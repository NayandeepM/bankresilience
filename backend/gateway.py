"""Trusted data-plane guard. One attempt per write; never replays a payment.

Runs in a separate Docker container so protection survives control-plane restart.
Only the authenticated loopback-published administration port accepts routing changes.
"""
from __future__ import annotations
import concurrent.futures
import hmac
import json
import os
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOP_HEADERS = {'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
               'te', 'trailer', 'transfer-encoding', 'upgrade', 'content-length', 'host'}


class RoutingGuard:
    def __init__(self, primary, standby, threshold_ms=300):
        self.hosts = {'primary': primary, 'standby': standby}
        self.threshold_ms = threshold_ms
        self.lock = threading.RLock()
        self.route = 'primary'
        self.health = {k: {'ok': False, 'healthy_streak': 0, 'bad_streak': 0} for k in self.hosts}
        self.events = []
        self.requests = {'primary': 0, 'standby': 0, 'successful': 0, 'failed': 0,
                         'writes': 0, 'ambiguous_writes': 0}
        self.last_failure = None

    def observe(self, name, ok, latency_ms):
        with self.lock:
            good = ok and latency_ms <= self.threshold_ms
            prior = self.health[name]
            healthy_streak=prior['healthy_streak'] + 1 if good else 0
            bad_streak=0 if good else prior['bad_streak'] + 1
            qualified=prior.get('qualified',False)
            if healthy_streak >= 2: qualified=True
            if bad_streak >= 2: qualified=False
            self.health[name] = {'ok': good, 'http_ready': ok, 'latency_ms': round(latency_ms, 1),
                                 'observed_at': time.time(),
                                 'healthy_streak': healthy_streak, 'bad_streak': bad_streak,
                                 'qualified':qualified}
            if self.route == 'primary' and self.health['primary']['bad_streak'] >= 2 and self.eligible('standby'):
                self.switch('standby', 'automatic-protection', 'Two primary health/latency breaches; healthy standby verified')

    def eligible(self, name):
        h = self.health[name]
        return h.get('qualified',False) and h['bad_streak'] < 2 and time.time() - h.get('observed_at', 0) < 2

    def switch(self, route, source, reason):
        with self.lock:
            if route not in self.hosts or not self.eligible(route):
                raise ValueError('Destination does not have two recent healthy observations')
            if route != self.route:
                self.events.append({'time': time.time(), 'from': self.route, 'to': route,
                                    'source': source, 'reason': reason})
                self.events = self.events[-100:]
                self.route = route

    def destination(self):
        with self.lock:
            if not self.eligible(self.route):
                if self.eligible('standby'):
                    self.switch('standby', 'automatic-protection', 'Primary unavailable before request dispatch')
                else:
                    return None, None
            return self.route, self.hosts[self.route]

    def snapshot(self):
        with self.lock:
            return json.loads(json.dumps({'route': self.route, 'available': self.eligible(self.route),
                'health': self.health, 'threshold_ms': self.threshold_ms, 'events': self.events,
                'requests': self.requests, 'last_failure': self.last_failure,
                'write_policy': 'Single dispatch; no automatic retries, including after timeout'}))


def probe(guard, name):
    tick = time.monotonic()
    try:
        with urllib.request.urlopen('http://' + guard.hosts[name] + ':8080/ready', timeout=.4) as response:
            ok = response.status == 200
    except Exception:
        ok = False
    guard.observe(name, ok, (time.monotonic() - tick) * 1000)


def monitor(guard):
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        while True:
            list(pool.map(lambda name: probe(guard, name), guard.hosts))
            time.sleep(.25)


def handlers(guard, token):
    class Proxy(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *args):
            pass  # Never log authentication headers, account identifiers or request bodies.

        def reply(self, status, body, headers=None):
            self.send_response(status)
            for key, value in (headers or {}).items():
                if key.lower() not in HOP_HEADERS:
                    self.send_header(key, value)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Connection', 'close')
            self.end_headers()
            self.close_connection = True
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def forward(self):
            if not self.path.startswith('/') or self.path.startswith('//'):
                self.reply(400, b'Invalid request path'); return
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if size < 0 or size > 1024 * 1024 or self.headers.get('Transfer-Encoding'):
                    self.reply(413, b'Unsupported request body'); return
                body = self.rfile.read(size) if size else None
            except ValueError:
                self.reply(400, b'Invalid content length'); return
            route, host = guard.destination()
            if not host:
                self.reply(503, b'No verified healthy instance is available'); return
            business = self.path.split('?', 1)[0] != '/ready'
            write = self.command not in ('GET', 'HEAD', 'OPTIONS')
            if business:
                with guard.lock:
                    guard.requests[route] += 1
                    guard.requests['writes'] += int(write)
            headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_HEADERS}
            request = urllib.request.Request('http://' + host + ':8080' + self.path,
                                            data=body, headers=headers, method=self.command)
            try:
                # Exactly ONE attempt. A timed-out write may already have committed.
                try:
                    response = urllib.request.urlopen(request, timeout=6)
                except urllib.error.HTTPError as error:
                    response = error
                with response:
                    status, payload, output_headers = response.code, response.read(), dict(response.headers)
                if business:
                    with guard.lock:
                        guard.requests['successful' if status < 400 else 'failed'] += 1
                self.reply(status, payload, output_headers)
            except Exception:
                if business:
                    with guard.lock:
                        guard.requests['failed'] += 1
                        guard.requests['ambiguous_writes'] += int(write)
                        guard.last_failure = {'time': time.time(), 'route': route, 'write_outcome_unknown': write}
                self.reply(502, b'Upstream request failed; request was not retried')

        do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = forward

    class Admin(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, status, data):
            payload = json.dumps(data).encode()
            self.send_response(status); self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(payload))); self.end_headers(); self.wfile.write(payload)

        def authorized(self):
            return hmac.compare_digest(self.headers.get('X-Resilience-Token', ''), token)

        def do_GET(self):
            if not self.authorized(): self.respond(403, {'error': 'Forbidden'}); return
            if self.path != '/status': self.respond(404, {'error': 'Unknown endpoint'}); return
            self.respond(200, guard.snapshot())

        def do_POST(self):
            if not self.authorized(): self.respond(403, {'error': 'Forbidden'}); return
            try:
                if self.path != '/route': raise ValueError('Unknown endpoint')
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size < 1024: raise ValueError('Invalid body size')
                data = json.loads(self.rfile.read(size))
                if set(data) != {'route'}: raise ValueError('Only an allowlisted route may be specified')
                guard.switch(data['route'], 'controller-approved', 'Verified routing request')
                self.respond(200, guard.snapshot())
            except (ValueError, KeyError):
                self.respond(409, {'error': 'Route rejected; healthy destination required'})

    return Proxy, Admin


def serve(guard, token):
    Proxy, Admin = handlers(guard, token)
    threading.Thread(target=monitor, args=(guard,), daemon=True).start()
    threading.Thread(target=ThreadingHTTPServer(('0.0.0.0', 9090), Admin).serve_forever, daemon=True).start()
    ThreadingHTTPServer(('0.0.0.0', 8080), Proxy).serve_forever()


if __name__ == '__main__':
    serve(RoutingGuard(os.environ['PRIMARY_HOST'], os.environ['STANDBY_HOST']), os.environ['ADMIN_TOKEN'])
