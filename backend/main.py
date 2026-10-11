from __future__ import annotations

import asyncio
import json
import sqlite3
import time
import uuid
import os
import threading
import copy
from contextlib import asynccontextmanager
from control import (SERVICES, TOPOLOGY, CATALOGUE, Action, Diagnosis, Plan, Analysis, healthy, approve, latency_summary, summarize, score, network_breached, fallback_diagnosis, fallback_plan, fallback_analysis)
from reasoning import reason, ai_configuration
from advice import RecoveryAdvice, ImmediateAdvice, diagnostic_context, fallback_advice, validate_advice, advisory_decision, advice_contract
from resilience import ResilienceManager, ResilienceDecision, tolerance_verified
from suite import DemoProbe, PaymentBudget, SuiteSummary, experiment_summary, summary_fallback
from policies import graph_features, safety_policy, evaluate, observed_affected, derived_impact
from planner import assess_system
from findings import export_report, read_report
from dotenv import load_dotenv
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import docker
import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

APP_DIR = Path(__file__).resolve().parent
load_dotenv(APP_DIR / ".env")
DB_PATH = Path(os.getenv("BANKRESILIENCE_DB_PATH", str(APP_DIR / "experiments.db")))
COMPOSE_PROJECT = "bank-of-anthos-local"
ALLOWED_FAULT_TARGET = "ledgerwriter"
FRONTEND_URL = "http://localhost:8082"
FAILURE_WINDOW_SECONDS = 12
SAFETY_THRESHOLD_SECONDS = 5
NETWORK_DELAY_MS = 800
NETWORK_JITTER_MS = 100
PACKET_LOSS_PERCENT = 60
PACKET_LOSS_EXPERIMENT_SECONDS = 12
CPU_STRESS_WORKERS = 2
CPU_STRESS_SECONDS = 12
CPU_STRESS_MARKER = "/tmp/bankresilience-cpu-stress.pid"
NETWORK_SAFETY_THRESHOLD_MS = 1500
NETWORK_EXPERIMENT_SECONDS = 10
RECOVERY_VERIFICATION_SECONDS = 75
app = FastAPI(title="BankResilience Control Plane", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)

try:
    docker_client = docker.from_env(timeout=4)
except Exception:
    docker_client = None

state: dict[str, Any] = {
    "active": False,
    "experiment": None,
    "events": [],
    "last_result": None,
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS experiments (
            id TEXT PRIMARY KEY, started_at TEXT, finished_at TEXT,
            status TEXT, target TEXT, result_json TEXT
        )""")
        conn.execute('CREATE TABLE IF NOT EXISTS suites (id TEXT PRIMARY KEY, started_at TEXT, report_json TEXT)')


init_db()


def add_event(message: str, agent: str = "Control Plane", level: str = "info") -> dict[str, str]:
    event = {"time": utc_now(), "agent": agent, "level": level, "message": message}
    state["events"].append(event)
    state["events"] = state["events"][-100:]
    return event


def get_project_containers() -> list[Any]:
    if docker_client is None:
        raise RuntimeError("Docker SDK cannot connect. Ensure Docker Desktop is running and install backend requirements.")
    containers = docker_client.containers.list(all=True, filters={"label": f"com.docker.compose.project={COMPOSE_PROJECT}"})
    return containers


def get_service_container(service: str) -> Any:
    if service not in {"frontend", "userservice", "contacts", "ledgerwriter", "balancereader", "transactionhistory", "accounts-db", "ledger-db"}:
        raise ValueError("Service is not allowlisted")
    for container in get_project_containers():
        if container.labels.get("com.docker.compose.service") == service:
            return container
    raise RuntimeError(f"Container for service '{service}' not found in Compose project '{COMPOSE_PROJECT}'.")


def validate_bind_mounts(container):
    for mount in container.attrs.get('Mounts', []):
        if mount.get('Type') != 'bind': continue
        source = Path(mount['Source'])
        if not source.exists():
            raise RuntimeError(f'Missing bind mount source: {source}; restart would fail')
        if mount.get('Destination') == '/tmp/.ssh/publickey' and not source.is_file():
            raise RuntimeError(f'Public key bind mount must be a file: {source}')

def snapshot_containers() -> list[dict[str, Any]]:
    try:
        containers = get_project_containers()
    except Exception as exc:
        return [{"service": "Docker", "status": "unavailable", "error": str(exc)}]
    result = []
    for c in containers:
        result.append({
            "service": c.labels.get("com.docker.compose.service", c.name),
            "container": c.name,
            "status": c.status,
            "health": (c.attrs.get("State", {}).get("Health", {}) or {}).get("Status", "n/a"),
            "restarts": c.attrs.get("RestartCount", 0),
        })
    return sorted(result, key=lambda x: x["service"])


def frontend_check() -> dict[str, Any]:
    start = time.monotonic()
    try:
        response = requests.get(FRONTEND_URL, timeout=3)
        return {"ok": 200 <= response.status_code < 400, "status_code": response.status_code,
                "latency_ms": round((time.monotonic() - start) * 1000, 1)}
    except requests.RequestException as exc:
        return {"ok": False, "error": str(exc), "latency_ms": round((time.monotonic() - start) * 1000, 1)}
def service_latency_check(service: str = "ledgerwriter") -> dict[str, Any]:
    """
    Measure latency to a backend service from the frontend container.

    This intentionally measures traffic from another Compose container so
    that network impairment applied to the target's network namespace is
    observable.
    """
    if service not in {"ledgerwriter", "contacts"}:
        raise ValueError("Probe target is not allowlisted")
    frontend = get_service_container("frontend")

    script = f"""
import time
import urllib.request

url = "http://{service}:8080/ready"
start = time.monotonic()

try:
    with urllib.request.urlopen(url, timeout=3) as response:
        status = response.status
except Exception as exc:
    elapsed = (time.monotonic() - start) * 1000
    print(f"ERROR|{{elapsed:.1f}}|{{exc}}")
else:
    elapsed = (time.monotonic() - start) * 1000
    print(f"OK|{{elapsed:.1f}}|{{status}}")
"""

    try:
        result = frontend.exec_run(
            ["python3", "-c", script],
            stdout=True,
            stderr=True,
        )

        output = result.output.decode("utf-8", errors="replace").strip()

        if output.startswith("OK|"):
            _, latency, status = output.split("|", 2)
            return {
                "ok": True,
                "latency_ms": float(latency),
                "status_code": int(status),
            }

        if output.startswith("ERROR|"):
            _, latency, error = output.split("|", 2)
            return {
                "ok": False,
                "latency_ms": float(latency),
                "error": error,
            }

        return {
            "ok": False,
            "latency_ms": None,
            "error": output or "No probe output",
        }

    except Exception as exc:
        return {
            "ok": False,
            "latency_ms": None,
            "error": str(exc),
        }
def apply_network_impairment(target: Any, netem_arguments: list[str], fault: str, duration_seconds: int = NETWORK_EXPERIMENT_SECONDS) -> Any:
    """Apply one owned netem policy with an independent ten-second cleanup."""
    helper = docker_client.containers.run(
        "nicolaka/netshoot:latest",
        command=["sh", "-c", f"sleep {duration_seconds}; tc qdisc del dev eth0 root; sleep 35"],
        detach=True,
        remove=False,
        network_mode=f"container:{target.id}",
        cap_add=["NET_ADMIN"],
        labels={
            "bankresilience.role": "network-safety-helper",
            "bankresilience.target": target.name,
            "bankresilience.fault": fault,
        },
    )

    try:
        result = helper.exec_run(["tc","qdisc","add","dev","eth0","root","netem",*netem_arguments],stdout=True,stderr=True)

        if result.exit_code != 0:
            output = result.output.decode("utf-8", errors="replace")
            raise RuntimeError(
                f"Failed to apply {fault}: {output}"
            )

        return helper

    except Exception:
        try:
            remove_network_delay(helper)
        except Exception:
            # Keep independent 10s namespace cleanup alive when the SDK is unavailable.
            pass
        raise

def apply_network_delay(target: Any) -> Any:
    return apply_network_impairment(target,["delay",f"{NETWORK_DELAY_MS}ms",f"{NETWORK_JITTER_MS}ms"],"network-slowdown")

def apply_packet_loss(target: Any) -> Any:
    return apply_network_impairment(target,["loss",f"{PACKET_LOSS_PERCENT}%"],"packet-loss",PACKET_LOSS_EXPERIMENT_SECONDS)

def remove_network_delay(helper: Any) -> None:
    """Delete only our qdisc; keep helper available if cleanup fails."""
    result = helper.exec_run(['tc','qdisc','del','dev','eth0','root'])
    if result.exit_code != 0:
        check = helper.exec_run(['tc','qdisc','show','dev','eth0'])
        if check.exit_code != 0 or b'netem' in check.output:
            raise RuntimeError('Network cleanup could not be verified; helper retained for escalation')
    helper.remove(force=True)

def apply_cpu_stress(target: Any) -> dict[str,str]:
    """Run bounded CPU workers inside the target cgroup and retain a kill handle."""
    script=(f"set -eu; test ! -e {CPU_STRESS_MARKER}; echo $$ > {CPU_STRESS_MARKER}; "
            "cleanup(){ kill $workers $timer 2>/dev/null || true; "
            f"rm -f {CPU_STRESS_MARKER}; }}; trap cleanup EXIT TERM INT; "
            "burn(){ while :; do :; done; }; workers=''; "
            f"i=0; while [ $i -lt {CPU_STRESS_WORKERS} ]; do burn & workers=\"$workers $!\"; i=$((i+1)); done; "
            f"(sleep {CPU_STRESS_SECONDS}; kill -TERM $$) & timer=$!; wait")
    created=docker_client.api.exec_create(target.id,["sh","-c",script])
    exec_id=created['Id']; docker_client.api.exec_start(exec_id,detach=True)
    for _ in range(10):
        check=target.exec_run(["sh","-c",f"test -s {CPU_STRESS_MARKER}"])
        if check.exit_code==0:return {'exec_id':exec_id,'target_id':target.id,'marker':CPU_STRESS_MARKER}
        time.sleep(.05)
    raise RuntimeError('CPU stress worker did not start')

def remove_cpu_stress(target: Any, handle: Any = None) -> None:
    """Terminate only the controller-owned stress process and verify its marker is gone."""
    script=(f"if [ -s {CPU_STRESS_MARKER} ]; then pid=$(cat {CPU_STRESS_MARKER}); kill -TERM \"$pid\" 2>/dev/null || true; "
            f"i=0; while [ -e {CPU_STRESS_MARKER} ] && [ $i -lt 30 ]; do sleep .1; i=$((i+1)); done; fi; "
            f"test ! -e {CPU_STRESS_MARKER}")
    result=target.exec_run(["sh","-c",script],stdout=True,stderr=True)
    if result.exit_code!=0:raise RuntimeError('CPU stress cleanup could not be verified')

def cleanup_compound(target, helper=None):
    """Attempt BOTH inverses even when one fails; used for restart and rollback."""
    failures=[]; actions=[]
    try:
        remove_cpu_stress(target)
        actions.append({'action':'stop-resource-pressure','verified':True})
    except Exception as exc:
        failures.append(str(exc));actions.append({'action':'stop-resource-pressure','verified':False})
    try:
        if helper is not None:
            helpers=[helper]
        else:
            helpers=docker_client.containers.list(all=True,filters={'label':['bankresilience.role=network-safety-helper',f'bankresilience.target={target.name}']})
        for item in helpers:remove_network_delay(item)
        actions.append({'action':'remove-network-impairment','verified':True})
    except Exception as exc:
        failures.append(str(exc));actions.append({'action':'remove-network-impairment','verified':False})
    if failures:raise RuntimeError('Combined cleanup incomplete: '+'; '.join(failures))
    return actions

# Shared runtime: only these deterministic adapters can mutate Docker.
lock = asyncio.Lock()
state.update(baseline=None, recent_telemetry=[], baseline_collecting=False, safety_state='idle', planner=None)
runner_task = None
sampler_task = None
suite_task = None
suite_cancel = threading.Event()
manual_kill = threading.Event()
resilience = ResilienceManager(docker_client, APP_DIR, get_service_container)
state.update(suite=None, suite_running=False, resilience=resilience.status())

def persist_suite(report):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('INSERT OR REPLACE INTO suites (id,started_at,report_json) VALUES (?,?,?)',
                     (report['id'], report['started_at'], json.dumps(report)))

def suite_history():
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute('SELECT report_json FROM suites ORDER BY started_at DESC LIMIT 15').fetchall()
    return [json.loads(r[0]) for r in rows]

def persist(result):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('INSERT OR REPLACE INTO experiments (id,started_at,finished_at,status,target,result_json) VALUES (?,?,?,?,?,?)',
            (result['id'],result['started_at'],result.get('finished_at'),result['status'],result['target'],json.dumps(result)))

def history_rows(limit=30):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory=sqlite3.Row
        rows=conn.execute('SELECT * FROM experiments ORDER BY started_at DESC'+(' LIMIT ?' if limit is not None else ''), (limit,) if limit is not None else ()).fetchall()
    return [dict(r, result=json.loads(r['result_json'] or '{}')) for r in rows]

def probe_services():
    # Fixed readiness endpoints only. HTTP errors are measured even for paused/stopped targets.
    hosts = {name: get_service_container(name).name for name in ('contacts', 'ledgerwriter')}
    script = "hosts=" + repr(hosts) + "\n" + """
import json, time, urllib.request
from concurrent.futures import ThreadPoolExecutor
names=['userservice','contacts','balancereader','ledgerwriter','transactionhistory']
def probe(name):
    start=time.monotonic()
    try:
        with urllib.request.urlopen('http://'+hosts.get(name,name)+':8080/ready',timeout=2.5) as response:
            return name,dict(ok=200<=response.status<400,status_code=response.status,latency_ms=round((time.monotonic()-start)*1000,1),source='http-readiness')
    except Exception as exc:
        return name,dict(ok=False,error=type(exc).__name__,latency_ms=round((time.monotonic()-start)*1000,1),source='http-readiness')
with ThreadPoolExecutor(max_workers=5) as pool:
    print(json.dumps(dict(pool.map(probe,names))))
"""
    response=get_service_container('frontend').exec_run(['python3','-c',script])
    if response.exit_code!=0: raise RuntimeError('Readiness instrumentation failed')
    return json.loads(response.output.decode())

def point(target='ledgerwriter', phase='idle', experiment_id=None):
    started=time.monotonic()
    services=snapshot_containers()
    front=frontend_check()
    try:
        service_probes=probe_services()
        probe=service_probes[target]
    except Exception as exc:
        service_probes={}
        probe={'ok':None,'latency_ms':None,'error':str(exc),'source':'instrumentation-unavailable'}
    resources={}
    try:
        container=get_service_container(target)
        if container.status!='running': raise RuntimeError('Resources unavailable for non-running container')
        # A regular non-streaming snapshot includes a real previous CPU sample.
        # Docker's one_shot form can expose lifetime counters and flatten a
        # short stress event into a misleading near-constant percentage.
        stats=container.stats(stream=False)
        cpu=stats.get('cpu_stats',{}); prev=stats.get('precpu_stats',{})
        delta=cpu.get('cpu_usage',{}).get('total_usage',0)-prev.get('cpu_usage',{}).get('total_usage',0)
        system=cpu.get('system_cpu_usage',0)-prev.get('system_cpu_usage',0)
        resources={'memory_bytes':stats.get('memory_stats',{}).get('usage'),
            'cpu_percent':round(delta/system*cpu.get('online_cpus',1)*100,2) if system>0 else None}
    except Exception:
        resources={'memory_bytes':None,'cpu_percent':None}
    measurement={'time':utc_now(),'experiment_id':experiment_id,'phase':phase,'target':target,'services':services,'service_probes':service_probes,
            'frontend':front,'target_probe':probe,'resources':resources,
            'restarts':{s['service']:s.get('restarts',0) for s in services},
            'collection_seconds':round(time.monotonic()-started,3)}
    measurement['resilience'] = resilience.sample(target)
    measurement['affected_services']=observed_affected(measurement)
    return measurement

def collect_baseline(samples=30, interval_seconds=.5, target='ledgerwriter'):
    started=utc_now(); tick=time.monotonic()
    # Warm-up excluded from distribution, but not from health preflight.
    for _ in range(2):
        point(target,'warmup')
    points=[]
    for _ in range(samples):
        points.append(point(target,'baseline'))
        if interval_seconds:
            time.sleep(interval_seconds)
    summary=summarize(points,target)
    summary['service_metrics']={name:latency_summary([p['service_probes'][name] for p in points if name in p.get('service_probes',{})]) for name in ('userservice','contacts','balancereader','ledgerwriter','transactionhistory')}
    return {**summary,'id':str(uuid.uuid4()),'target':target,'started_at':started,'finished_at':utc_now(),
            'observation_duration_seconds':round(time.monotonic()-tick,2),'sample_count':samples,'samples':samples,
            'warmup_samples':2,'outlier_policy':'Warm-up excluded; all observed errors retained; successful latency median and nearest-rank p95; no hidden outlier deletion.',
            'service_health':{'expected':8,'min_running':min(sum(s['status']=='running' for s in p['services']) for p in points),
                'final_running':sum(s['status']=='running' for s in points[-1]['services'])},
            'services':points[-1]['services'],'restarts':points[-1]['restarts'],
            'resource_measurements':[p['resources'] for p in points],'telemetry':points}

def save_baseline(report):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('INSERT INTO baselines (id,report_json) VALUES (?,?)',(report['id'],json.dumps(report)))
    state['baseline']=report

async def sample_idle():
    while True:
        try:
            if not state['active'] and not state['suite_running'] and not state['baseline_collecting']:
                p=await asyncio.to_thread(point)
                # An experiment can start while the idle sample is in flight.
                if not state['active']:
                    state['recent_telemetry']=(state['recent_telemetry']+[p])[-120:]
        except Exception as exc:
            state['telemetry_error']=str(exc)
        await asyncio.sleep(1)

@asynccontextmanager
async def lifespan(app):
    global sampler_task
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS baselines (id TEXT PRIMARY KEY, report_json TEXT)')
        row=conn.execute('SELECT report_json FROM baselines ORDER BY rowid DESC LIMIT 1').fetchone()
    if row:
        state['baseline']=json.loads(row[0])
    suites = suite_history()
    for report in suites:
        if report.get('status') == 'running':
            report.update(status='interrupted', finished_at=utc_now(), stop_reason='Control plane restarted; suite will not resume fault injection')
            persist_suite(report)
    if suites: state['suite'] = suites[0]
    # Journal is written before mutation. Restart resumes cleanup only for known fault inverses.
    for item in history_rows():
        r=item['result']
        if r.get('status')=='running' and r.get('experiment_type') in CATALOGUE:
            try:
                spec=CATALOGUE[r['experiment_type']]; target=get_service_container(spec['target'])
                if spec['action']=='start-service':
                    target.start()
                elif spec['action']=='unpause-service':
                    target.reload()
                    if target.status=='paused': target.unpause()
                elif spec['action']=='remove-network-impairment':
                    helpers=docker_client.containers.list(all=True,filters={'label':['bankresilience.role=network-safety-helper',f'bankresilience.target={target.name}']})
                    for helper in helpers: remove_network_delay(helper)
                elif spec['action']=='stop-resource-pressure':
                    remove_cpu_stress(target)
                elif spec['action']=='remove-compound-impairment':
                    cleanup_compound(target)
                r.update(status='interrupted-cleaned-needs-verification',finished_at=utc_now(),recovery_verified=False)
            except Exception as exc:
                r.update(status='manual-escalation',error=str(exc),finished_at=utc_now(),recovery_verified=False)
                state['safety_state']='manual-escalation'
            persist(r)
    rows=history_rows()
    if rows:
        state['last_result']=rows[0]['result']
        state['experiment']=rows[0]['result']
        state['events']=rows[0]['result'].get('events',[])
    sampler_task=asyncio.create_task(sample_idle())
    yield
    suite_cancel.set()
    manual_kill.set()
    if suite_task and not suite_task.done(): await suite_task
    if runner_task and not runner_task.done():
        await runner_task
    sampler_task.cancel()
    try: await sampler_task
    except asyncio.CancelledError: pass

app.router.lifespan_context=lifespan

class StartExperimentRequest(BaseModel):
    confirm: bool = False
    resilience_enabled: bool = True
class BaselineRequest(BaseModel):
    samples: int = 30
class SuiteRequest(BaseModel):
    confirm: bool = False
    resilience_enabled: bool = True
    verify_payments: bool = False
    payment_budget: int = Field(default=15,ge=0,le=15)
    combinations_only: bool = False

@app.get('/api/health')
def health():
    try:
        if docker_client is None: raise RuntimeError('Docker unavailable')
        docker_client.ping()
        return {'ok':True,'docker_connected':True,'project':COMPOSE_PROJECT,'frontend_url':FRONTEND_URL}
    except Exception as exc:
        return {'ok':False,'docker_connected':False,'docker_error':str(exc),'project':COMPOSE_PROJECT,'frontend_url':FRONTEND_URL}

@app.get('/api/state')
def get_state():
    return {**copy.deepcopy(state),'resilience':resilience.status(),'ai':ai_configuration(),'services':snapshot_containers(),'frontend_check':frontend_check()}

@app.get('/api/history')
def history():
    return [{k:v for k,v in r.items() if k!='result_json'} for r in history_rows()]

FINDINGS_PATH = APP_DIR.parent / 'FINDINGS.md'

@app.get('/api/findings')
def findings_report():
    return read_report(FINDINGS_PATH)

@app.post('/api/findings')
def generate_findings():
    if state['active'] or state['suite_running'] or state['baseline_collecting']:
        raise HTTPException(409,'Wait for the active operation before refreshing findings')
    return export_report(history_rows(limit=None),FINDINGS_PATH)

@app.get('/api/findings/download')
def download_findings():
    if not FINDINGS_PATH.is_file():raise HTTPException(404,'Generate the findings report first')
    return FileResponse(FINDINGS_PATH,media_type='text/markdown; charset=utf-8',filename='FINDINGS.md',headers={'Cache-Control':'no-store'})

@app.get('/api/baseline')
def baseline():
    return state['baseline'] or {'status':'not-collected'}

@app.post('/api/baseline')
async def refresh_baseline(payload: BaselineRequest):
    if not 10<=payload.samples<=60:
        raise HTTPException(400,'Baseline sample count must be 10–60')
    async with lock:
        if state['active'] or state['suite_running'] or state['baseline_collecting']: raise HTTPException(409,'Observation or experiment suite already active')
        state['baseline_collecting']=True
    try:
        report=await asyncio.to_thread(collect_baseline,payload.samples)
        save_baseline(report)
        return report
    finally:
        state['baseline_collecting']=False

@app.get('/api/experiments')
def catalogue():
    return [{'experiment_type':k,**v} for k,v in CATALOGUE.items()]

@app.post('/api/planner')
async def planner():
    if state['active'] or state['suite_running']: raise HTTPException(409,'Wait for the current experiment or suite')
    context={'graph_features':graph_features(),'baseline':state['baseline'],'topology':TOPOLOGY,'catalogue':CATALOGUE,'history':[{'result':{k:v for k,v in h['result'].items() if k in ('experiment_type','resilience_score','analysis','recovery_verified')}} for h in history_rows()]}
    if not context['baseline']: raise HTTPException(409,'Collect a baseline before planning')
    output=await asyncio.to_thread(reason,'Rank the available experiments using baseline, topology, scores and history. Return every available experiment exactly once.',Plan,context,fallback_plan(context))
    kinds=[p['experiment_type'] for p in output['recommendations']]
    valid_entries=all(p['target']==CATALOGUE[p['experiment_type']]['target'] and p['rank']==i+1 for i,p in enumerate(output['recommendations']))
    if set(kinds)!=set(CATALOGUE) or len(kinds)!=len(CATALOGUE) or not valid_entries:
        output={**fallback_plan(context).model_dump(),'mode':'deterministic-fallback','fallback_reason':'Planner returned incomplete or duplicate catalogue'}
    output['ranked_experiments']=output['recommendations']
    output['graph_features']=graph_features()
    state['planner']=output
    return output

@app.post('/api/experiments/{kind}')
async def start_experiment(kind: str,payload:StartExperimentRequest):
    global runner_task
    if kind not in CATALOGUE: raise HTTPException(404,'Unknown experiment')
    if not payload.confirm: raise HTTPException(400,'Explicit fault confirmation required')
    async with lock:
        if state['active'] or state['suite_running'] or state['baseline_collecting']: raise HTTPException(409,'Observation or experiment suite already active')
        if state['safety_state']=='manual-escalation': raise HTTPException(409,'Resolve failed cleanup and restart control plane before further injection')
        services=await asyncio.to_thread(snapshot_containers)
        if not healthy(services): raise HTTPException(409,'Healthy baseline required: all eight expected services must be running and ready')
        result=new_result(kind, payload.resilience_enabled)
        stage(result,'planner','Gemini plan selected' if state['planner'] and state['planner'].get('mode')=='gemini' else 'Manual selection; Gemini ranking not available','Planner')
        manual_kill.clear()
        state.update(active=True,experiment=result,events=result['events'],safety_state='preflight')
        persist(result)
        runner_task=asyncio.create_task(run_experiment(result))
    return {'accepted':True,'id':result['id']}

def new_result(kind, protection=True, suite_id=None, payments=False):
    spec=CATALOGUE[kind]
    result={'id':str(uuid.uuid4())[:8],'experiment_type':kind,'name':spec['name'],'target':spec['target'],
        'started_at':utc_now(),'status':'running','phase':'baseline','telemetry':[],
        'fault_metrics':[],'recovery_metrics':[],'events':[],'safety_events':[],'diagnosis':None,
        'recovery_verified':False,'recovery_due':False,'injected':False,'max_fault_seconds':spec['max_seconds'],
        'lifecycle':[], 'schema_version':3,'planner_snapshot':copy.deepcopy(state['planner']),
        'resilience_enabled':protection,'suite_id':suite_id,'verify_payments':payments,
        'components':spec.get('components',[]),'component_actions':[]}
    result['experiment_id']=result['id']; result['start_time']=result['started_at']
    return result

@app.get('/api/suites')
def list_suites(): return suite_history()

@app.post('/api/suites')
async def start_suite(payload: SuiteRequest):
    global suite_task
    if not payload.confirm: raise HTTPException(400, 'Confirm the sequential fault suite before starting')
    async with lock:
        if state['active'] or state['suite_running'] or state['baseline_collecting']: raise HTTPException(409, 'Another operation is already active')
        if state['safety_state']=='manual-escalation': raise HTTPException(409, 'Resolve failed cleanup before starting a suite')
        if not healthy(await asyncio.to_thread(snapshot_containers)): raise HTTPException(409, 'All eight primary services must be healthy')
        suite={'id':str(uuid.uuid4())[:8], 'started_at':utc_now(), 'status':'running', 'phase':'preparing',
               'order':[k for k,s in CATALOGUE.items() if bool(s.get('components'))==payload.combinations_only], 'current_index':0, 'results':[], 'experiment_ids':[],
               'combinations_only':payload.combinations_only,
               'resilience_enabled':payload.resilience_enabled, 'verify_payments':payload.verify_payments,
               'payment_budget':payload.payment_budget,
               'summary':None}
        suite_cancel.clear(); manual_kill.clear()
        state.update(suite=suite, suite_running=True)
        persist_suite(suite)
        suite_task=asyncio.create_task(run_suite(suite))
    return {'accepted':True, 'id':suite['id']}

@app.post('/api/suites/cancel')
async def cancel_suite():
    if not state['suite_running']: raise HTTPException(409,'No suite is running')
    suite_cancel.set(); manual_kill.set()
    return {'accepted':True,'id':state['suite']['id']}

@app.post('/api/resilience/enable')
async def enable_resilience():
    async with lock:
        if state['active'] or state['suite_running'] or state['baseline_collecting']: raise HTTPException(409,'Wait for the active operation')
        state['baseline_collecting']=True
    try: return await asyncio.to_thread(resilience.ensure_enabled)
    except Exception: raise HTTPException(503,'Resilience setup failed; inspect backend logs and infrastructure health')
    finally: state['baseline_collecting']=False

@app.post('/api/resilience/disable')
async def disable_resilience():
    async with lock:
        if state['active'] or state['suite_running'] or state['baseline_collecting']: raise HTTPException(409,'Wait for the active operation')
        state['baseline_collecting']=True
    try: return await asyncio.to_thread(resilience.disable)
    finally: state['baseline_collecting']=False

@app.post('/api/recovery/verify')
async def verify_restoration():
    async with lock:
        if state['active'] or state['suite_running'] or state['baseline_collecting']:raise HTTPException(409,'Wait for the active operation')
        state['baseline_collecting']=True
    try:
        report=await asyncio.to_thread(collect_baseline,10,.15,'ledgerwriter')
        if not all(healthy(p['services']) and p['frontend']['ok'] and
                   all(probe.get('ok') is True for probe in p['service_probes'].values()) and len(p['service_probes'])==5
                   for p in report['telemetry']):raise HTTPException(409,'Restoration not verified; keep faults disabled')
        if resilience.installed():await asyncio.to_thread(resilience.restore_primary_routes)
        save_baseline(report);state['safety_state']='idle';manual_kill.clear()
        return {'verified':True,'baseline_id':report['id'],'scope':'Ten fresh samples of all primary readiness endpoints and frontend; prior failed reports remain unchanged'}
    finally:state['baseline_collecting']=False

async def run_suite(suite):
    payment_budget=PaymentBudget(suite.get('payment_budget',15))
    try:
        for index, kind in enumerate(suite['order']):
            if suite_cancel.is_set(): suite['status']='cancelled'; break
            if not healthy(await asyncio.to_thread(snapshot_containers)):
                raise RuntimeError('Suite stopped: primary services are not healthy before the next fault')
            suite.update(current_index=index+1, phase=CATALOGUE[kind]['name'])
            result=new_result(kind,suite['resilience_enabled'],suite['id'],suite['verify_payments'])
            result['payment_allowance']=min(5,(payment_budget.limit-payment_budget.attempted)//(len(suite['order'])-index))
            suite['experiment_ids'].append(result['id'])
            manual_kill.clear()
            state.update(active=True,experiment=result,events=result['events'],safety_state='preflight')
            persist(result); persist_suite(suite)
            await run_experiment(result,payment_budget)
            suite['results'].append(experiment_summary(result)); persist_suite(suite)
            if not result['recovery_verified']:
                if not result.get('injection_attempted'):
                    raise RuntimeError('Suite stopped: preflight failed before injection; '+result.get('error','baseline unverified'))
                raise RuntimeError('Suite stopped: cleanup/recovery was not verified; no further faults will be injected')
        if suite['status']=='running': suite['status']='cancelled' if suite_cancel.is_set() else 'completed'
    except Exception as exc:
        suite.update(status='failed', stop_reason=str(exc))
    finally:
        suite['phase']='summarizing'
        suite['measured_summary']=summary_fallback(suite).model_dump()
        try:
            suite['summary']=await asyncio.to_thread(reason,
                'Summarize the measured sequential suite. Distinguish primary degradation, protected-route tolerance, recovery, '
                'sampled authenticated home/payment results and unknown accounting correctness. Do not attribute automatic gateway '
                'failover to Gemini; distinguish AI advice from executed control. Use only supplied metrics; keep under 180 words.',
                SuiteSummary,{'results':suite['results'],'status':suite['status']},summary_fallback(suite))
        except Exception:
            suite['summary']={**suite['measured_summary'],'mode':'deterministic-fallback'}
        suite.update(phase='finished', finished_at=utc_now())
        suite['payments_attempted']=payment_budget.attempted
        persist_suite(suite); state['suite_running']=False

@app.post('/api/kill-switch')
async def kill_switch():
    if not state['active']: raise HTTPException(409,'No active experiment')
    manual_kill.set()
    if state['suite_running']: suite_cancel.set()
    return {'accepted':True,'experiment_id':state['experiment']['id']}

def event(result,message,agent='Control Plane',level='info',kind=None):
    e={'time':utc_now(),'experiment_id':result['id'],'agent':agent,'level':level,'message':message,'kind':kind}
    result['events'].append(e)
    if agent=='Safety Controller': result['safety_events'].append(e)
    return e

def stage(result,name,detail,agent):
    result['stage']=name
    result.setdefault('lifecycle',[]).append({'stage':name,'time':utc_now(),'agent':agent,'detail':detail})

def inspect_network_policy(helper):
    """Trusted read-only adapter; model never supplies command text."""
    if helper is None:return {'available':False,'reason':'Trusted network inspector unavailable'}
    response=helper.exec_run(['tc','qdisc','show','dev','eth0'],stdout=True,stderr=True)
    if response.exit_code!=0:raise RuntimeError('Traffic-policy inspection failed')
    output=response.output.decode('utf-8',errors='replace')[:1200]
    return {'available':True,'controller_owned_netem':'netem' in output,'policy_output':output,'observed_at':utc_now()}

async def diagnose(result,helper=None):
    stage(result,'diagnose','Inferring cause from observations; fault identity withheld','Diagnosis Agent')
    if helper is not None and result['experiment_type']!='cpu-stress' and not result.get('network_observation'):
        try:
            result['network_observation']=await asyncio.to_thread(inspect_network_policy,helper)
            event(result,'Read-only traffic-policy observation captured','Observability Agent',kind='network-inspected')
        except Exception:result['network_observation']={'available':False,'reason':'Inspection failed; no network-policy claim is verified'}
    context=diagnostic_context(result)
    fallback=fallback_advice(result)
    brief_fallback=ImmediateAdvice.model_validate({k:v for k,v in fallback.model_dump().items() if k in ImmediateAdvice.model_fields})
    output=await asyncio.to_thread(reason,
        'Make a brief immediate diagnosis and action choice from observations without an injection label. Keep the whole response under 150 words. Explain why two alternatives are NOT chosen. Cite evidence_ids exactly. Expected effect concerns readiness/latency only; never assume failed business flows or absent configuration.',
        advice_contract(context,brief=True),context,brief_fallback)
    try:
        if output.get('mode')=='gemini':validate_advice(output,context,result,brief=True)
    except Exception as exc:
        output={**brief_fallback.model_dump(),'mode':'deterministic-fallback','fallback_reason':str(exc)}
    # Controller scope is attached only AFTER reasoning; it is never a model input/guess.
    output.setdefault('evidence',[f"Cited observation: {ref}" for ref in output['evidence_refs']])
    output.setdefault('blast_radius',output['impacted_services'])
    output.setdefault('current_system_state','See cited process state and readiness observations')
    answer={**output,'experiment_type':result['experiment_type'],'diagnostic_scope':'Fault label, injection parameters and selected inverse withheld from Gemini',
        'resilience_improvements':[p['title'] for p in output.get('improvement_plan',[])]}
    result['diagnosis']=answer
    result['diagnosis_time']=utc_now()
    result['diagnosis_evidence']=context['evidence']
    result.setdefault('diagnosis_history',[]).append({'time':result['diagnosis_time'],'answer':copy.deepcopy(answer),'evidence':context['evidence']})
    event(result,f'Gemini inferred {answer.get("inferred_condition")}; proposed {answer["recommended_action"]["action"]}' if answer['mode']=='gemini' else 'AI unavailable: deterministic containment recommendation only','Diagnosis Agent',kind='recommendation-generated')

async def review_actions(result):
    if not result.get('fault_metrics'):
        result['action_review']={'mode':'unavailable','fallback_reason':'No incident observations to review'}
        return
    context=diagnostic_context(result)
    context['recovery_observed']={'verified':result.get('recovery_verified'),
        'executed_action':result.get('recovery_action'),'source':result.get('recovery_mode'),
        'readiness_recovery_seconds':result.get('recovery_time')}
    # Fault identity/parameters remain withheld. This is after containment, not an executed decision.
    output=await asyncio.to_thread(reason,
        'Review this incident after containment. Infer the original cause from incident observations, evaluate the safe action and two rejected alternatives, and propose one or two engineering improvements. Keep under 350 words. Titles should say Evaluate; hypotheses should start If/Whether. Only supplied routing configuration and sampled business responses are inspected; other controls and accounting correctness are unverified. No caller failure/business-flow/absence-of-configuration claim is established by readiness alone. Cite evidence_ids exactly. This review does not execute any action.',
        advice_contract(context),context,fallback_advice(result))
    try:
        if output.get('mode')=='gemini':validate_advice(output,context,result)
    except Exception as exc:output={**fallback_advice(result).model_dump(),'mode':'deterministic-fallback','fallback_reason':str(exc)}
    for proposal in output.get('improvement_plan',[]):proposal['configuration_status']='not-inspected'
    result['action_review']={**output,'created_at':utc_now(),'scope':'Post-recovery review; not the recommendation used to execute containment'}
    result['action_review_evidence']=context['evidence']
    event(result,'Gemini engineering review generated after recovery' if output['mode']=='gemini' else 'AI engineering review unavailable','Analysis Agent',kind='action-review-generated')

async def resilience_advice(result, observation):
    sample=copy.deepcopy(observation['resilience'])
    # Gateways outlive experiments. Prior planned failovers must never be mistaken
    # for repeated instability in the current incident.
    started=datetime.fromisoformat(result['fault_start_time']).timestamp() if result.get('fault_start_time') else time.time()
    sample.setdefault('routing',{})['events']=[e for e in sample.get('routing',{}).get('events',[]) if e.get('time',0)>=started]
    sample.get('routing',{}).pop('requests',None)
    sample.get('routing',{}).pop('last_failure',None)
    fallback=ResilienceDecision(experiment_id=result['id'],target=result['target'],action='route-to-standby',
        confidence=1, rationale='Independent routing guard verified primary degradation and a healthy standby; keep protected routing.',
        evidence_refs=['primary-observation','routing-observation'])
    context={'experiment_id':result['id'],'target':result['target'],'evidence':{
        'primary-observation':{'probe':observation['target_probe'],'services':observation['services']},
        'routing-observation':sample},
        'capabilities':['route-to-standby: only a verified healthy standby for this target',
                        'escalate: end the bounded fault if protected traffic is unsafe'],
        'scope':'Guard may already have failed over independently. Fault identity withheld. No retries of submitted writes.'}
    answer=await asyncio.to_thread(reason,'Maintain the verified healthy standby route while the primary is degraded. '
        'Primary degradation alone is NOT a reason to escalate when protected traffic is healthy. Escalate only for concrete '
        'protected-route failure. No write retries is an intentional duplicate-prevention rule, not a defect. '
        'Use only current-incident observations. Keep under 90 words. Cite both evidence IDs. You do not remove the fault.',
        ResilienceDecision,context,fallback)
    try:
        choice=ResilienceDecision.model_validate({k:v for k,v in answer.items() if k not in ('mode','model','fallback_reason')})
        if choice.experiment_id!=result['id'] or choice.target!=result['target']: raise ValueError('Wrong scope')
        if set(choice.evidence_refs)!={'primary-observation','routing-observation'}: raise ValueError('Incomplete evidence')
        if choice.action=='route-to-standby' and choice.confidence < .7: raise ValueError('Insufficient confidence')
        if result['recovery_due']:
            answer['execution']='Advice arrived after cleanup started; no routing action executed'
        elif choice.action=='escalate':
            current=await asyncio.to_thread(resilience.sample,result['target'])
            result['review_requested']=True
            if tolerance_verified(current):
                answer['execution']='Review requested; healthy protected routing remains active. Primary failure alone cannot end the tolerance test.'
            else:
                result['resilience_escalate']=True
                answer['execution']='Protected-route failure confirmed; requested independent containment'
        else:
            await asyncio.to_thread(resilience.control,result['target'],'standby')
            answer['execution']='Verified standby route maintained; automatic guard originally performs failover'
    except Exception:
        answer={**fallback.model_dump(),'mode':'deterministic-fallback','fallback_reason':'AI routing advice rejected or destination not healthy',
                'execution':'Automatic guard remains authoritative; cleanup if protected traffic fails'}
    result['resilience_decision']=answer
    event(result,'Gemini resilience advice recorded' if answer['mode']=='gemini' else 'Deterministic resilience guard active; AI advice unavailable',
          'Resilience Agent',kind='resilience-advice')

async def monitor_protected_traffic(result, done, fault_tick, recover, business_probe=None):
    """Independent fast sampling: a slow raw-primary probe must not hide tolerance.

    Fault and business evidence are timestamped again at completion. Sampling never
    extends the watchdog, and requests spanning containment are not fault successes.
    """
    advice_task=None
    try:
        while not done.is_set() and not result['recovery_due']:
            sample=await asyncio.to_thread(resilience.sample,result['target'])
            if done.is_set() or result['recovery_due']: break
            observation={'time':utc_now(),'phase':'fault','fault_elapsed_seconds':round(time.monotonic()-fault_tick,3),
                         'resilience':sample,'primary_fault_active':sample.get('routing',{}).get('health',{}).get('primary',{}).get('ok') is False}
            result.setdefault('tolerance_observations',[]).append(observation)
            protected=tolerance_verified(sample)
            if protected and observation['primary_fault_active']:
                if not result.get('tolerance_start_time'):
                    result['tolerance_start_time']=utc_now(); result['time_to_tolerance']=observation['fault_elapsed_seconds']
                    event(result,'Verified standby serving while primary degradation remains observed; independent traffic monitoring active',
                          'Resilience Agent',kind='tolerance-started')
                if advice_task is None:
                    primary=sample['routing']['health']['primary']
                    advice_task=asyncio.create_task(resilience_advice(result,{
                        'resilience':copy.deepcopy(sample),'services':[],
                        'target_probe':{'ok':primary.get('http_ready'), 'latency_ms':primary.get('latency_ms'),
                                        'source':'Independent guard readiness check; timeouts are lower bounds'}}))
            if result.get('tolerance_start_time') and not protected:
                await asyncio.to_thread(recover,'Protected route lost health; immediate containment','resilience-failed'); break
            if result.get('resilience_escalate'):
                await asyncio.to_thread(recover,'Resilience Agent requested containment','resilience-escalation'); break
            await asyncio.sleep(.35)
    except Exception:
        await asyncio.to_thread(recover,'Protected-traffic monitoring unavailable; contain fault safely','instrumentation')
    finally:
        if advice_task:
            try:await asyncio.wait_for(advice_task,9)
            except Exception:pass

async def monitor_business_traffic(result,done,fault_tick,recover,probe):
    try:
        while not done.is_set() and not result['recovery_due']:
            sample=await asyncio.to_thread(resilience.sample,result['target'])
            if not tolerance_verified(sample):
                await asyncio.sleep(.3);continue
            observed=await asyncio.to_thread(probe.check,'fault')
            if done.is_set() or result['recovery_due']:observed['phase']='intervention'
            else:observed.update(fault_elapsed_seconds=round(time.monotonic()-fault_tick,3),primary_fault_active=True,
                                 protected_route=sample['routing']['route'])
            result['business_observations'].append(observed)
            if not observed['home']['ok'] or (observed['payment']['attempted'] and not observed['payment']['ok']):
                await asyncio.to_thread(recover,'Sampled banking operation failed; end fault safely','business-flow');break
            await asyncio.sleep(.6)
    except Exception:
        await asyncio.to_thread(recover,'Banking-flow measurement unavailable; contain fault safely','instrumentation')

async def run_experiment(result, payment_budget=None):
    spec=CATALOGUE[result['experiment_type']]
    target=None; helper=None; watchdog=None; diagnosis_task=None; resilience_task=None; business_probe=None; traffic_task=None; business_task=None
    done=threading.Event(); recovery_lock=threading.Lock()
    fault_tick=None

    def recover(reason_text,trigger='emergency'):
        # This runs in an independent OS thread, without waiting for probes or AI.
        with recovery_lock:
            if done.is_set(): return
            result['recovery_due']=True
            result['intervention_reason']=reason_text
            state['safety_state']='kill-switch'
            result['phase']='recovery'
            result['recovery_started_monotonic']=time.monotonic()
            if fault_tick is not None:
                result.setdefault('detection_time',round(time.monotonic()-fault_tick,3))
                result['time_to_mitigation']=round(time.monotonic()-fault_tick,3)
            result['intervention_trigger']=trigger
            stage(result,'safety-approval',reason_text,'Safety Controller')
            event(result,reason_text,'Safety Controller','error','emergency-timeout' if trigger=='emergency' else 'manual-kill' if trigger=='manual' else 'mitigation-trigger')
            event(result,'KILL SWITCH ACTIVATED','Safety Controller','error','kill-switch')
            diagnosis=result.get('diagnosis')
            if not diagnosis:
                diagnosis={**fallback_diagnosis(result).model_dump(),'mode':'deterministic-fallback','fallback_reason':'AI unavailable before safety deadline'}
                result['diagnosis']=diagnosis
                event(result,'Bounded fallback recovery recommendation generated before deadline','Diagnosis Agent',kind='recommendation-generated')
            if diagnosis.get('recommended_action',{}).get('action')=='escalate':result['review_requested']=True
            try:
                action=Action.model_validate(diagnosis['recommended_action'])
                if diagnosis.get('mode')=='gemini' and advisory_decision(diagnosis,result)!='mitigate':
                    raise ValueError('Advisory choice requires controller containment; it is not an executed recovery mutation')
                approval=approve(action,result,active=True,recovery_due=True)
            except Exception as exc:
                advisory=diagnosis.get('recommended_action',{}).get('action') in ('observe','inspect-network','escalate')
                event(result,f'Controller containment overrides advisory: {exc}' if advisory else f'AI recovery action rejected: {exc}','Safety Controller','warning' if advisory else 'error','advisory-overridden' if advisory else 'action-rejected')
                action=fallback_diagnosis(result).recommended_action
                approval=approve(action,result,active=True,recovery_due=True)
                result['emergency_recommendation']={'mode':'deterministic-fallback',**action.model_dump()}
                result['safety_override_reason']=str(exc)
            result['recovery_diagnosis']=copy.deepcopy(diagnosis)
            result['recommendation']=diagnosis['recommended_action']
            result['recovery_mode']='gemini-recommended' if diagnosis.get('mode')=='gemini' and not result.get('emergency_recommendation') else 'deterministic-emergency-fallback'
            result['safety_decision']={'decision':'APPROVED','reason':result.get('safety_override_reason','Exact inverse approved; '+('observed state supports Gemini recommendation' if diagnosis.get('mode')=='gemini' else 'independent controller containment')),'requested_action':diagnosis['recommended_action'],'trigger':trigger,'time':utc_now()}
            result['approved_action']=approval
            event(result,f'Approved allowlisted action: {action.action} on {action.target}','Safety Controller',kind='action-approved')
            result['recovery_start_time']=utc_now()
            stage(result,'recover',f'Executing {action.action}; {result["recovery_mode"]}','Recovery Agent')
            event(result,'Recovery execution started','Recovery Agent',kind='recovery-started')
            try:
                if action.action=='start-service': target.start()
                elif action.action=='unpause-service':
                    target.reload()
                    if target.status=='paused': target.unpause()
                elif action.action=='remove-network-impairment':
                    if helper is None: raise RuntimeError('Network helper unavailable')
                    remove_network_delay(helper)
                elif action.action=='stop-resource-pressure':
                    remove_cpu_stress(target,helper)
                elif action.action=='remove-compound-impairment':
                    result['component_actions']=cleanup_compound(target,helper)
                result['recovery_action']=approval
                result['cleanup_succeeded']=True
                result['recovery_action_end_time']=utc_now()
                event(result,'Recovery action completed; verifying readiness and latency','Recovery Agent',kind='recovery-completed')
            except Exception as exc:
                result['cleanup_succeeded']=False; result['recovery_error']=str(exc)
                event(result,f'Recovery failed: {exc}; manual intervention required','Recovery Agent','error','recovery-failed')
                state['safety_state']='manual-escalation'
            done.set()

    try:
        if result.get('resilience_enabled'):
            stage(result,'resilience-setup','Verifying standby replicas and protected routes','Resilience Agent')
            await asyncio.to_thread(resilience.ensure_enabled)
            await asyncio.to_thread(resilience.restore_primary_routes)
            event(result,'Verified standby capacity; automatic routing guard armed. Faults will remain active while protected traffic is healthy.',
                  'Resilience Agent',kind='resilience-armed')
        elif result.get('resilience_enabled') is False and resilience.installed():
            await asyncio.to_thread(resilience.disable)
        stage(result,'safety-check','Fresh baseline and mount preflight','Safety Controller')
        result['baseline']=await asyncio.to_thread(collect_baseline,10,.15,result['target'])
        result['telemetry'].extend([{**p,'experiment_id':result['id']} for p in result['baseline']['telemetry']])
        report=result['baseline']
        if result.get('suite_id'): save_baseline(report)
        if report[result['target']]['availability_percent']!=100 or report['frontend']['availability_percent']!=100 or not all(healthy(p['services']) for p in report['telemetry']):
            raise RuntimeError('Injection blocked: fresh baseline probes or services are not healthy')
        if result['target']=='ledgerwriter' and not state['baseline']: save_baseline(report)
        result['safety_policy']=safety_policy(result['experiment_type'],report)
        parameters={
            'network-slowdown':{'delay_ms':NETWORK_DELAY_MS,'jitter_ms':NETWORK_JITTER_MS},
            'packet-loss':{'loss_percent':PACKET_LOSS_PERCENT},
            'cpu-stress':{'workers':CPU_STRESS_WORKERS,'duration_limit_seconds':CPU_STRESS_SECONDS},
            'ledgerwriter-outage':{'operation':'stop'},
            'contacts-pause':{'operation':'pause'},
            'latency-cpu':{'delay_ms':NETWORK_DELAY_MS,'jitter_ms':NETWORK_JITTER_MS,'workers':CPU_STRESS_WORKERS},
            'loss-cpu':{'loss_percent':PACKET_LOSS_PERCENT,'workers':CPU_STRESS_WORKERS},
        }
        result['fault_parameters']=parameters[result['experiment_type']]
        event(result,'Warm-up and repeated baseline probes passed','Observability Agent',kind='baseline')
        if result.get('suite_id'):
            # Exercise the backup path BEFORE fault injection, including its cold-start write path.
            if result.get('resilience_enabled'):
                for service in ('ledgerwriter','contacts'):await asyncio.to_thread(resilience.control,service,'standby')
            business_probe=await asyncio.to_thread(DemoProbe,get_service_container('frontend'),result.get('verify_payments',False),payment_budget,result.get('payment_allowance',5))
            initial=await asyncio.to_thread(business_probe.check,'baseline')
            initial['route_scope']='Verified standby path' if result.get('resilience_enabled') else 'Original service path'
            result['business_observations']=[initial]
            if result.get('resilience_enabled'):await asyncio.to_thread(resilience.restore_primary_routes)
            if not initial['home']['ok'] or (initial['payment']['attempted'] and not initial['payment']['ok']):
                raise RuntimeError('Business-flow baseline failed; injection blocked')
        if manual_kill.is_set(): raise RuntimeError('Cancelled before fault injection')
        target=await asyncio.to_thread(get_service_container,result['target'])
        await asyncio.to_thread(target.reload)
        if target.status!='running': raise RuntimeError('Target state changed before injection')
        validate_bind_mounts(target)
        # Journal already persisted before attempting mutation; cleanup also runs for ambiguous errors.
        result['injection_attempted']=True
        event(result,f'Injecting {result["experiment_type"]} on {result["target"]}','Chaos Agent','warning','fault-start')
        stage(result,'chaos','Injecting bounded fault','Chaos Agent')
        result['fault_start_time']=utc_now()
        fault_tick=time.monotonic()
        if result['experiment_type']=='ledgerwriter-outage': await asyncio.to_thread(target.stop,timeout=1)
        elif result['experiment_type']=='contacts-pause': await asyncio.to_thread(target.pause)
        elif result['experiment_type']=='network-slowdown': helper=await asyncio.to_thread(apply_network_delay,target)
        elif result['experiment_type']=='packet-loss': helper=await asyncio.to_thread(apply_packet_loss,target)
        elif result['experiment_type']=='cpu-stress': helper=await asyncio.to_thread(apply_cpu_stress,target)
        elif spec.get('components'):
            # Both adapters self-expire; record each component before attempting it.
            # If the second mutation fails, finally invokes both exact inverses.
            result['component_injections']=[{'kind':spec['components'][0],'attempted_at':utc_now()}]
            helper=await asyncio.to_thread(apply_packet_loss if result['experiment_type']=='loss-cpu' else apply_network_delay,target)
            result['component_injections'][0]['completed_at']=utc_now()
            result['component_injections'].append({'kind':'cpu-stress','attempted_at':utc_now()})
            await asyncio.to_thread(apply_cpu_stress,target)
            result['component_injections'][-1]['completed_at']=utc_now()
            result['combined_active_start_time']=utc_now()
        result['injected']=True; result['phase']='fault'; state['safety_state']='armed'
        stage(result,'observe','Collecting HTTP readiness, states and resources','Observability Agent')
        watchdog=threading.Timer(max(.01,spec['max_seconds']-(time.monotonic()-fault_tick)),recover,args=(
            'Bounded tolerance window complete; restore the primary' if result.get('resilience_enabled') else 'Absolute emergency fault duration reached',
            'tolerance-window-complete' if result.get('resilience_enabled') else 'emergency'))
        watchdog.daemon=True; watchdog.start()
        if result.get('resilience_enabled'):
            traffic_task=asyncio.create_task(monitor_protected_traffic(result,done,fault_tick,recover,business_probe))
            if business_probe:business_task=asyncio.create_task(monitor_business_traffic(result,done,fault_tick,recover,business_probe))
        while not done.is_set():
            if manual_kill.is_set():
                await asyncio.to_thread(recover,'Manual kill switch requested','manual'); break
            p=await asyncio.to_thread(point,result['target'],'fault',result['id'])
            p['fault_elapsed_seconds']=round(time.monotonic()-fault_tick,3)
            protected=tolerance_verified(p.get('resilience',{})) and result.get('resilience_enabled')
            # A probe straddling intervention is explicitly labeled, not silently attributed to fault.
            if done.is_set() or result['recovery_due']:
                p['phase']='intervention'; result['telemetry'].append(p); break
            p['affected_services']=observed_affected(p,result['safety_policy'])
            result['fault_metrics'].append(p); result['telemetry'].append(p)
            assessment=evaluate(result['fault_metrics'],result['safety_policy'])
            result['policy_evaluation']=assessment
            if protected:
                if not result.get('tolerance_start_time'):
                    result['tolerance_start_time']=utc_now()
                    result['time_to_tolerance']=round(time.monotonic()-fault_tick,3)
                    event(result,'Healthy standby serving while the primary fault remains active. Writes are never automatically replayed.',
                          'Resilience Agent',kind='tolerance-started')
                if resilience_task is None and traffic_task is None:
                    resilience_task=asyncio.create_task(resilience_advice(result,copy.deepcopy(p)))
            if business_probe and traffic_task is None:
                observed=await asyncio.to_thread(business_probe.check,'fault')
                # Do not call a request completed after cleanup a during-fault success.
                if result['recovery_due'] or done.is_set(): observed['phase']='intervention'
                else:
                    observed['fault_elapsed_seconds']=round(time.monotonic()-fault_tick,3)
                    observed['primary_fault_active']=True
                    observed['protected_route']=p.get('resilience',{}).get('routing',{}).get('route')
                result['business_observations'].append(observed)
                if not observed['home']['ok'] or (observed['payment']['attempted'] and not observed['payment']['ok']):
                    await asyncio.to_thread(recover,'Sampled banking operation failed; end the fault safely','business-flow')
                    break
            if done.is_set(): break
            unsafe_blast=len(assessment.get('affected_services',[]))>result['safety_policy']['blast_radius']['max_affected_services']
            unsafe_resources=any(b['metric'] in ('cpu_percent','memory_bytes') and b['level']=='critical' for b in assessment['breaches'])
            if result.get('resilience_escalate'):
                await asyncio.to_thread(recover,'Resilience Agent requested containment','resilience-escalation'); break
            if result.get('tolerance_start_time') and not protected:
                await asyncio.to_thread(recover,'Protected route lost health; end fault immediately','resilience-failed'); break
            if assessment['level']!='normal':
                if 'first_degradation_monotonic' not in result:
                    result['first_degradation_monotonic']=time.monotonic()
                    result['detection_time']=round(time.monotonic()-fault_tick,3)
                    event(result,'Measured degradation detected','Observability Agent','warning','degradation')
                if diagnosis_task is None:
                    try:
                        result['target_logs']=(await asyncio.to_thread(target.logs,tail=20,timestamps=True)).decode('utf-8',errors='replace')[-2000:]
                    except Exception:result['target_logs']='Logs unavailable'
                    diagnosis_task=asyncio.create_task(diagnose(result,helper))
                    result['diagnosis_rounds']=1
            if not protected and assessment['level']=='warning' and diagnosis_task and diagnosis_task.done() and result.get('diagnosis',{}).get('mode')=='gemini' and result.get('advice_consumed_at')!=result.get('diagnosis_time'):
                result['advice_consumed_at']=result['diagnosis_time']
                try:
                    choice=advisory_decision(result['diagnosis'],result)
                    if choice=='mitigate':
                        await asyncio.to_thread(recover,'Gemini mitigation supported by observed degradation and allowlisted inverse','ai-recommendation')
                    elif choice=='escalate':
                        result['review_requested']=True
                        await asyncio.to_thread(recover,'Gemini requested review; controller containment ends the fault safely','ai-escalation')
                    else:
                        decision={'time':utc_now(),'action':choice,'decision':'APPROVED','reason':'Read-only/bounded observation below critical limits'}
                        result.setdefault('advisory_decisions',[]).append(decision)
                        event(result,'Approved Gemini advisory choice: '+choice,'Safety Controller',kind='advisory-approved')
                        if choice=='inspect-network':
                            try:result['network_observation']=await asyncio.to_thread(inspect_network_policy,helper)
                            except Exception:result['network_observation']={'available':False,'reason':'Inspection failed'}
                        if result.get('diagnosis_rounds',1)<3:
                            # Re-query on the NEXT measured point, never extend the watchdog.
                            result['diagnosis_rounds']=result.get('diagnosis_rounds',1)+1
                            result['rediagnose_after_samples']=len(result['fault_metrics'])+1
                except Exception as exc:
                    event(result,'Gemini advisory rejected: '+str(exc),'Safety Controller','warning','advisory-rejected')
            if result.get('rediagnose_after_samples') and len(result['fault_metrics'])>=result['rediagnose_after_samples'] and not done.is_set():
                result.pop('rediagnose_after_samples')
                diagnosis_task=asyncio.create_task(diagnose(result,helper))
            if assessment['level']=='critical' and (not protected or unsafe_blast or unsafe_resources):
                result['threshold_breaches']=assessment['breaches']
                result['threshold_breach_time']=utc_now()
                event(result,'Metric policy breached: '+', '.join(f"{b['metric']}={b['observed']} (limit {b['limit']})" for b in assessment['breaches']),'Safety Controller','error','threshold-breach')
                # AI is already running from the first warning. Bound its remaining decision time.
                if diagnosis_task and not diagnosis_task.done():
                    try:await asyncio.wait_for(asyncio.shield(diagnosis_task),result['safety_policy']['ai_decision_budget_seconds'])
                    except asyncio.TimeoutError:pass
                await asyncio.to_thread(recover,'Critical measured metric policy requires mitigation','metric')
            if p['target_probe'].get('ok') is None:
                await asyncio.to_thread(recover,'Telemetry unavailable; emergency containment','instrumentation')
            await asyncio.sleep(.2)
    except Exception as exc:
        result['error']=str(exc)
        event(result,f'Experiment error: {exc}','Control Plane','error')
    finally:
        if watchdog: watchdog.cancel()
        if result.get('injection_attempted') and target is not None and not done.is_set():
            await asyncio.to_thread(recover,'Emergency cleanup / bounded experiment completion')
        if result.get('injection_attempted') and target is not None:
            stage(result,'verify','Three consecutive readiness and baseline-restoration checks','Verification')
            deadline=time.monotonic()+RECOVERY_VERIFICATION_SECONDS; consecutive=0
            while time.monotonic()<deadline:
                p=await asyncio.to_thread(point,result['target'],'recovery',result['id'])
                result['telemetry'].append(p); result['recovery_metrics'].append(p)
                base=result['baseline'][result['target']]['latency_ms']['p95'] or 100
                valid=healthy(p['services']) and p['frontend']['ok'] and p['target_probe']['ok'] and (p['target_probe'].get('latency_ms') or float('inf')) <= max(base*2,base+100)
                consecutive=consecutive+1 if valid else 0
                if consecutive>=3 and result.get('cleanup_succeeded'):
                    result['recovery_verified']=True; break
                await asyncio.sleep(.5)
            result['recovery_end_time']=utc_now()
            result['baseline_return_time']=utc_now() if result['recovery_verified'] else None
            if result.get('first_degradation_monotonic') is not None:
                result['total_degraded_duration']=round(time.monotonic()-result.pop('first_degradation_monotonic'),3)
            result['recovery_time']=round(time.monotonic()-result.get('recovery_started_monotonic',time.monotonic()),2)
            result['recovery_duration_seconds']=result['recovery_time']
            event(result,'Recovery verified: three consecutive readiness, service-state, frontend and latency checks passed' if result['recovery_verified'] else 'Recovery verification timed out: manual escalation required','Recovery Agent','success' if result['recovery_verified'] else 'error','recovered' if result['recovery_verified'] else 'recovery-failed')
        if diagnosis_task:
            try: await asyncio.wait_for(diagnosis_task,3)
            except Exception: pass
        if resilience_task:
            try: await asyncio.wait_for(resilience_task,9)
            except Exception: pass
        if traffic_task:
            try:await asyncio.wait_for(traffic_task,15)
            except Exception:pass
        if business_task:
            try:await asyncio.wait_for(business_task,12)
            except Exception:pass
        if business_probe and result.get('injection_attempted'):
            try:
                result['business_observations'].append(await asyncio.to_thread(business_probe.check,'recovery'))
            except Exception:
                result['business_verification_error']='Recovery business-flow measurement failed'
            finally: business_probe.close()
        elif business_probe: business_probe.close()
        if result.get('resilience_enabled') and result.get('recovery_verified'):
            try:
                await asyncio.to_thread(resilience.restore_primary_routes)
                event(result,'Recovered primary verified and returned to traffic','Resilience Agent',kind='primary-route-restored')
            except Exception:
                result['recovery_verified']=False
                result['error']='Primary recovered but route restoration failed; suite must stop'
                state['safety_state']='manual-escalation'
        result.pop('recovery_started_monotonic',None)
        result['impact']=derived_impact(result)
        stage(result,'analyze','Measured score and Gemini post-analysis','Analysis Agent')
        result['resilience_score']=score(result)
        await review_actions(result)
        result['analysis']=await asyncio.to_thread(reason,'Analyze measured baseline/fault/recovery impact and explain the deterministic score. Rank only observed weaknesses. Use measurement_scope: configuration is uninspected, caller impact is potential unless observed, and banking flows are unverified. Recommend evaluation/testing of existing controls; do not assert absence of circuit breakers, retries, alerts or redundancy.',Analysis,
            {'measurement_scope':diagnostic_context(result)['evidence']['measurement-scope'] if result.get('baseline') else {'business_operations':'unverified','configuration':'uninspected'},'engineering_review':result.get('action_review'),'impact':result['impact'],'score':result['resilience_score'],'diagnosis':result['diagnosis'],'recovery_verified':result['recovery_verified'],'recovery_time':result.get('recovery_time'),'previous_results':[{'experiment_type':h['result'].get('experiment_type'),'score':h['result'].get('resilience_score'),'recovery_verified':h['result'].get('recovery_verified')} for h in history_rows()[:10]]},fallback_analysis(result))
        result['severity']=result['analysis']['severity']
        result['status']='recovered' if result['recovery_verified'] else 'manual-escalation' if result.get('injection_attempted') else 'blocked-before-injection'
        result['tolerance_report']=experiment_summary(result)
        result['phase']='completed'; result['finished_at']=utc_now(); result['end_time']=result['finished_at']
        event(result,'Experiment saved; verify a demo banking transfer before claiming end-to-end recovery',level='success' if result['recovery_verified'] else 'error',kind='completed')
        try:
            persist(result)
        finally:
            state['last_result']=result; state['active']=False
            state['safety_state']='recovered' if result['recovery_verified'] else 'manual-escalation' if result.get('injection_attempted') else 'idle'

@app.get('/api/analysis')
def system_analysis():
    return assess_system(history_rows(limit=None))
