"""Fixed infrastructure and routing adapters. Gemini never supplies Docker arguments."""
from __future__ import annotations
import json
import secrets
import threading
import time
from pathlib import Path
from typing import Literal
import requests
from pydantic import Field
from control import Contract

PROJECT = 'bank-of-anthos-local'
TARGETS = {'ledgerwriter': (18083, 19083), 'contacts': (18084, 19084)}
OWNER = 'bankresilience-managed'


class ResilienceDecision(Contract):
    experiment_id: str
    target: Literal['ledgerwriter', 'contacts']
    action: Literal['route-to-standby', 'escalate']
    confidence: float = Field(ge=0, le=1)
    rationale: str
    evidence_refs: list[Literal['primary-observation', 'routing-observation']]


class ResilienceManager:
    def __init__(self, client, app_dir, get_primary):
        self.client = client
        self.app_dir = Path(app_dir)
        self.get_primary = get_primary
        self.journal = self.app_dir / '.resilience-state.json'
        self.lock = threading.RLock()

    def installed(self):
        return self.journal.exists() and json.loads(self.journal.read_text()).get('enabled', False)

    def owned(self, name, role):
        try:
            container = self.client.containers.get(name)
        except Exception as error:
            if getattr(error, 'status_code', None) == 404: return None
            raise
        if container.labels.get('bankresilience.owner') != OWNER or container.labels.get('bankresilience.role') != role:
            raise RuntimeError('Managed container name is occupied by an unrelated container')
        return container

    def gateway(self, target):
        if target not in TARGETS: raise ValueError('Target not permitted')
        gateway = self.owned('bankresilience-' + target + '-gateway', 'routing-gateway')
        if gateway is None: raise RuntimeError('Routing gateway is not installed')
        return gateway

    def control(self, target, route=None):
        gateway = self.gateway(target)
        gateway.reload()
        if gateway.status != 'running': raise RuntimeError('Routing gateway is not running')
        env = dict(value.split('=', 1) for value in gateway.attrs['Config']['Env'])
        url = 'http://127.0.0.1:' + str(TARGETS[target][1])
        headers = {'X-Resilience-Token': env['ADMIN_TOKEN']}
        if route is None: response = requests.get(url + '/status', headers=headers, timeout=1)
        else: response = requests.post(url + '/route', headers=headers, json={'route': route}, timeout=1)
        if response.status_code != 200: raise RuntimeError('Routing guard rejected the request')
        return response.json()

    def status(self):
        if not self.installed(): return {'enabled': False, 'targets': {}}
        targets = {}
        for target in TARGETS:
            try: targets[target] = self.control(target)
            except Exception: targets[target] = {'available': False, 'error': 'Routing control unavailable'}
        return {'enabled': True, 'targets': targets, 'scope': 'Single primary fault in LedgerWriter or Contacts; shared databases remain dependencies'}

    def sample(self, target):
        if not self.installed(): return {'enabled': False}
        tick = time.monotonic()
        try:
            routing = self.control(target)
            response = requests.get('http://127.0.0.1:' + str(TARGETS[target][0]) + '/ready', timeout=1)
            return {'enabled': True, 'routing': routing, 'protected_probe':
                    {'ok': response.status_code == 200, 'latency_ms': round((time.monotonic() - tick) * 1000, 1)}}
        except Exception:
            return {'enabled': True, 'routing': {'available': False}, 'protected_probe': {'ok': False, 'latency_ms': None}}

    def ensure_enabled(self):
        with self.lock:
            if self.installed():
                status = self.status()
                if not all(s.get('available') for s in status['targets'].values()):
                    raise RuntimeError('Existing resilience infrastructure is unhealthy; restore it before injecting faults')
                return status
            if self.journal.exists():
                # Resume rollback of a partially completed installation before retrying.
                self.disable()
            record = {'enabled': False, 'targets': {}}
            self.journal.write_text(json.dumps(record))
            try:
                frontend = self.get_primary('frontend')
                for target, ports in TARGETS.items():
                    primary = self.get_primary(target); primary.reload()
                    if primary.status != 'running': raise RuntimeError('Healthy primaries are required before setup')
                    networks = primary.attrs['NetworkSettings']['Networks']
                    network_name = next(n for n in networks if n.startswith(PROJECT + '_'))
                    network = self.client.networks.get(network_name)
                    record['targets'][target] = {'network': network_name, 'primary': primary.name,
                        'aliases': networks[network_name].get('Aliases') or [target, primary.name]}
                    self.journal.write_text(json.dumps(record))  # Journal before any network mutation.
                    standby_name = 'bankresilience-' + target + '-standby'
                    standby = self.owned(standby_name, 'standby')
                    if standby is None:
                        config = primary.attrs['Config']
                        volumes = {m['Source']: {'bind': m['Destination'], 'mode': 'ro' if not m['RW'] else 'rw'}
                                   for m in primary.attrs.get('Mounts', []) if m['Type'] == 'bind'}
                        standby = self.client.containers.run(primary.image.id, config.get('Cmd'),
                            entrypoint=config.get('Entrypoint'), environment=config['Env'], volumes=volumes,
                            working_dir=config.get('WorkingDir') or None, user=config.get('User') or None,
                            name=standby_name, network=network_name, detach=True,
                            restart_policy={'Name': 'unless-stopped'},
                            labels={'bankresilience.owner': OWNER, 'bankresilience.role': 'standby', 'bankresilience.target': target})
                    elif standby.status != 'running': standby.start()
                    gateway_name = 'bankresilience-' + target + '-gateway'
                    gateway = self.owned(gateway_name, 'routing-gateway')
                    if gateway is None:
                        gateway = self.client.containers.run(frontend.image.id, ['/opt/bankresilience/gateway.py'],
                            entrypoint=['python'], name=gateway_name, network=network_name, detach=True,
                            environment={'PRIMARY_HOST': primary.name, 'STANDBY_HOST': standby_name,
                                         'ADMIN_TOKEN': secrets.token_urlsafe(32)},
                            volumes={str(self.app_dir / 'gateway.py'): {'bind': '/opt/bankresilience/gateway.py', 'mode': 'ro'}},
                            ports={'8080/tcp': ('127.0.0.1', ports[0]), '9090/tcp': ('127.0.0.1', ports[1])},
                            restart_policy={'Name': 'unless-stopped'}, cap_drop=['ALL'],
                            security_opt=['no-new-privileges:true'],
                            labels={'bankresilience.owner': OWNER, 'bankresilience.role': 'routing-gateway', 'bankresilience.target': target})
                    elif gateway.status != 'running': gateway.start()
                    deadline = time.monotonic() + 40
                    while time.monotonic() < deadline:
                        try:
                            observation = self.control(target)
                            if all(h['healthy_streak'] >= 2 for h in observation['health'].values()): break
                        except Exception: pass
                        time.sleep(.4)
                    else: raise RuntimeError('Standby readiness timed out; application routing was not changed')
                    # The logical service name now resolves solely to the guard. The original
                    # container's full name remains available for independent fault measurements.
                    network.disconnect(primary)
                    network.connect(primary, aliases=[target + '-primary'])
                    network.disconnect(gateway)
                    network.connect(gateway, aliases=[target])
                record['enabled'] = True
                self.journal.write_text(json.dumps(record))
                # Reconnecting the gateway briefly interrupts its health requests.
                # Do not report setup complete until both protected routes recover.
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    status = self.status()
                    if all(s.get('available') for s in status['targets'].values()): return status
                    time.sleep(.3)
                raise RuntimeError('Protected routes failed the post-install readiness check')
            except Exception:
                self.disable()
                raise

    def restore_primary_routes(self):
        if not self.installed(): return
        deadline = time.monotonic() + 5
        for target in TARGETS:
            while True:
                try: self.control(target, 'primary'); break
                except Exception:
                    if time.monotonic() >= deadline: raise RuntimeError('Primary route could not be restored safely')
                    time.sleep(.3)

    def disable(self):
        with self.lock:
            if not self.journal.exists(): return {'enabled': False}
            record = json.loads(self.journal.read_text())
            for target, info in record.get('targets', {}).items():
                primary = self.get_primary(target)
                network = self.client.networks.get(info['network'])
                try: network.disconnect(primary)
                except Exception:
                    primary.reload()
                    if info['network'] in primary.attrs['NetworkSettings']['Networks']: raise
                network.connect(primary, aliases=info['aliases'])
                for role, suffix in [('routing-gateway', 'gateway'), ('standby', 'standby')]:
                    container = self.owned('bankresilience-' + target + '-' + suffix, role)
                    if container is not None: container.remove(force=True)
            self.journal.unlink()
            return {'enabled': False, 'targets': {}}


def tolerance_verified(sample):
    routing = sample.get('routing', {})
    return (sample.get('enabled') and sample.get('protected_probe', {}).get('ok') is True
            and routing.get('route') == 'standby' and routing.get('available') is True
            and (routing.get('health', {}).get('standby', {}).get('qualified') is True
                 or routing.get('health', {}).get('standby', {}).get('healthy_streak', 0) >= 2))
