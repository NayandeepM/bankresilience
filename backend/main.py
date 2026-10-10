from __future__ import annotations

import asyncio
import json
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import docker
import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

APP_DIR = Path(__file__).resolve().parent
DB_PATH = APP_DIR / "experiments.db"
COMPOSE_PROJECT = "bank-of-anthos-local"
ALLOWED_FAULT_TARGET = "ledgerwriter"
FRONTEND_URL = "http://localhost:8082"
FAILURE_WINDOW_SECONDS = 12
EXPECTED_SERVICES = ["frontend", "userservice", "contacts", "ledgerwriter", "balancereader", "transactionhistory", "accounts-db", "ledger-db"]
ALLOWED_SERVICES = set(EXPECTED_SERVICES)
EXPERIMENTS: dict[str, dict[str, str]] = {
    "ledgerwriter-outage": {
        "name": "LedgerWriter outage",
        "target": "ledgerwriter",
        "failure_type": "crash",
        "summary": "Stop ledgerwriter for 12 seconds, then restart it.",
    },
    "contacts-pause": {
        "name": "Contacts slowdown/hang",
        "target": "contacts",
        "failure_type": "slowdown/hang",
        "summary": "Pause contacts for 12 seconds, then unpause it.",
    },
    "transactionhistory-network-disconnect": {
        "name": "TransactionHistory network failure",
        "target": "transactionhistory",
        "failure_type": "network failure",
        "summary": "Disconnect transactionhistory from its Compose network for 12 seconds, then reconnect it.",
    },
}

app = FastAPI(title="BankResilience Control Plane", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)

try:
    docker_client = docker.from_env()
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
    if service not in ALLOWED_SERVICES:
        raise ValueError("Service is not allowlisted")
    for container in get_project_containers():
        if container.labels.get("com.docker.compose.service") == service:
            return container
    raise RuntimeError(f"Container for service '{service}' not found in Compose project '{COMPOSE_PROJECT}'.")


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
        return {"ok": response.status_code < 500, "status_code": response.status_code,
                "latency_ms": round((time.monotonic() - start) * 1000, 1)}
    except requests.RequestException as exc:
        return {"ok": False, "error": str(exc), "latency_ms": round((time.monotonic() - start) * 1000, 1)}


class StartExperimentRequest(BaseModel):
    confirm: bool = False


@app.get("/api/health")
def health() -> dict[str, Any]:
    docker_ok = False
    docker_error = None
    try:
        if docker_client:
            docker_client.ping()
            docker_ok = True
    except Exception as exc:
        docker_error = str(exc)
    return {"ok": docker_ok, "docker_connected": docker_ok, "docker_error": docker_error,
            "project": COMPOSE_PROJECT, "frontend_url": FRONTEND_URL}


@app.get("/api/experiments")
def list_experiments() -> list[dict[str, str]]:
    return [{"id": key, **value} for key, value in EXPERIMENTS.items()]


@app.get("/api/state")
def get_state() -> dict[str, Any]:
    return {**state, "services": snapshot_containers(), "frontend_check": frontend_check()}


@app.get("/api/history")
def history() -> list[dict[str, Any]]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM experiments ORDER BY started_at DESC LIMIT 20").fetchall()
    return [{"id": r["id"], "started_at": r["started_at"], "finished_at": r["finished_at"],
             "status": r["status"], "target": r["target"], "result": json.loads(r["result_json"] or "{}")}
            for r in rows]


@app.post("/api/experiments/ledgerwriter-outage")
async def start_ledgerwriter_experiment(payload: StartExperimentRequest) -> dict[str, Any]:
    return await start_experiment("ledgerwriter-outage", payload)


@app.post("/api/experiments/contacts-pause")
async def start_contacts_pause_experiment(payload: StartExperimentRequest) -> dict[str, Any]:
    return await start_experiment("contacts-pause", payload)


@app.post("/api/experiments/transactionhistory-network-disconnect")
async def start_transactionhistory_network_experiment(payload: StartExperimentRequest) -> dict[str, Any]:
    return await start_experiment("transactionhistory-network-disconnect", payload)


async def start_experiment(experiment_key: str, payload: StartExperimentRequest) -> dict[str, Any]:
    spec = EXPERIMENTS[experiment_key]
    if not payload.confirm:
        raise HTTPException(status_code=400, detail=f"Explicit confirmation is required to run {spec['name']}.")
    if state["active"]:
        raise HTTPException(status_code=409, detail="An experiment is already running.")
    try:
        services = snapshot_containers()
        if any(s.get("status") == "unavailable" for s in services):
            raise RuntimeError(services[0].get("error", "Docker unavailable"))
        service_map = {s["service"]: s for s in services}
        missing = [name for name in EXPECTED_SERVICES if name not in service_map]
        if missing:
            raise RuntimeError("Expected Compose services not found: " + ", ".join(missing))
        not_running = [name for name in EXPECTED_SERVICES if service_map[name]["status"] != "running"]
        if not_running:
            raise RuntimeError("Healthy baseline required; these services are not running: " + ", ".join(not_running))
    except Exception as exc:
        raise HTTPException(status_code=409, detail=f"Preflight blocked the experiment: {exc}") from exc

    experiment_id = str(uuid.uuid4())[:8]
    started_at = utc_now()
    state["active"] = True
    state["experiment"] = {"id": experiment_id, "name": spec["name"], "target": spec["target"],
                           "failure_type": spec["failure_type"], "status": "running", "started_at": started_at,
                           "failure_window_seconds": FAILURE_WINDOW_SECONDS}
    state["events"] = []
    add_event("Healthy baseline passed: all eight expected services are running.", "Safety Controller")
    asyncio.create_task(run_experiment(experiment_id, started_at, spec))
    return {"accepted": True, "id": experiment_id, "message": "Experiment started; the fault window is bounded and recovery runs automatically."}


def get_compose_network_name(container: Any) -> str:
    container.reload()
    networks = container.attrs.get("NetworkSettings", {}).get("Networks", {}) or {}
    compose_networks = [
        name for name, details in networks.items()
        if details.get("NetworkID") and name not in {"bridge", "host", "none"}
    ]
    if not compose_networks:
        raise RuntimeError(f"No Compose network found for {container.name}.")
    return sorted(compose_networks)[0]


def get_network_aliases(container: Any, network_name: str) -> list[str]:
    container.reload()
    network_details = (container.attrs.get("NetworkSettings", {}).get("Networks", {}) or {}).get(network_name, {})
    return network_details.get("Aliases", []) or []


async def run_experiment(experiment_id: str, started_at: str, spec: dict[str, str]) -> None:
    target_service = spec["target"]
    failure_type = spec["failure_type"]
    result: dict[str, Any] = {"id": experiment_id, "name": spec["name"], "target": target_service,
                              "failure_type": failure_type, "started_at": started_at,
                              "failure_window_seconds": FAILURE_WINDOW_SECONDS, "events": []}
    target = None
    injected = False
    network_name = None
    network_aliases = []
    try:
        before_services = snapshot_containers()
        result["baseline_services"] = before_services
        result["baseline_frontend"] = frontend_check()
        add_event("Captured container and frontend baseline.", "Observability Agent")

        target = get_service_container(target_service)
        # Recheck the target immediately before the destructive operation.
        target.reload()
        if target.status != "running":
            raise RuntimeError(f"Refusing injection: {target_service} is not running (status={target.status}).")

        if failure_type == "crash":
            add_event(f"Injecting approved fault: stopping {target_service}.", "Chaos Agent", "warning")
            target.stop(timeout=3)
        elif failure_type == "slowdown/hang":
            add_event(f"Injecting approved fault: pausing {target_service}.", "Chaos Agent", "warning")
            target.pause()
        elif failure_type == "network failure":
            network_name = get_compose_network_name(target)
            network_aliases = get_network_aliases(target, network_name)
            result["network_name"] = network_name
            result["network_aliases"] = network_aliases
            add_event(f"Injecting approved fault: disconnecting {target_service} from {network_name}.", "Chaos Agent", "warning")
            docker_client.networks.get(network_name).disconnect(target)
        else:
            raise RuntimeError(f"Unknown failure type: {failure_type}")

        injected = True
        state["experiment"]["phase"] = "fault-injected"
        add_event(f"{spec['name']} fault is active. Collecting downstream evidence for 12 seconds.", "Observability Agent", "warning")
        await asyncio.sleep(FAILURE_WINDOW_SECONDS)

        try:
            target.reload()
            fault_status = target.status
        except Exception as exc:
            fault_status = f"unavailable: {exc}"
        try:
            logs = target.logs(tail=100, timestamps=True).decode("utf-8", errors="replace")
        except Exception as exc:
            logs = f"Could not retrieve target logs: {exc}"
        result["fault_target_status"] = fault_status
        result["fault_frontend"] = frontend_check()
        result["fault_services"] = snapshot_containers()
        result["target_logs"] = logs[-12000:]
        add_event(f"Evidence captured. {target_service} status during fault: {fault_status}.", "Observability Agent")

        # Deterministic first-version diagnosis; an LLM is not permitted to run commands.
        diagnosis = {
            "root_cause": f"{target_service} experienced an injected {failure_type} fault.",
            "confidence": 0.99 if fault_status == "exited" else 0.75,
            "evidence": [f"Target container state during fault: {fault_status}",
                         f"The Chaos Agent recorded a successful {failure_type} action.",
                         f"Frontend probe during fault: {result['fault_frontend']}"],
            "recommended_action": f"Recover {target_service}, confirm all Compose services are running, and verify the banking UI.",
        }
        result["diagnosis"] = diagnosis
        state["experiment"]["diagnosis"] = diagnosis
        add_event(f"Diagnosis: {target_service} impact came from the injected {failure_type} fault.", "Diagnosis Agent", "warning")

    except Exception as exc:
        result["error"] = str(exc)
        add_event(f"Experiment error: {exc}", "Control Plane", "error")
    finally:
        recovery_start = time.monotonic()
        if injected and target is not None:
            try:
                add_event(f"Executing bounded recovery for {target_service}.", "Recovery Agent")
                if failure_type == "crash":
                    target.start()
                elif failure_type == "slowdown/hang":
                    target.unpause()
                elif failure_type == "network failure":
                    if not network_name:
                        network_name = get_compose_network_name(target)
                    docker_client.networks.get(network_name).connect(target, aliases=network_aliases)
                deadline = time.monotonic() + 35
                last_status = "unknown"
                while time.monotonic() < deadline:
                    try:
                        target.reload()
                        last_status = target.status
                        target_networks = target.attrs.get("NetworkSettings", {}).get("Networks", {}) or {}
                        network_recovered = failure_type != "network failure" or network_name in target_networks
                        if last_status == "running" and network_recovered:
                            break
                    except Exception as exc:
                        last_status = str(exc)
                    await asyncio.sleep(1)
                result["recovery_container_status"] = last_status
                result["recovery_duration_seconds"] = round(time.monotonic() - recovery_start, 2)
                result["recovery_frontend"] = frontend_check()
                result["recovery_services"] = snapshot_containers()
                verified = last_status == "running" and result["recovery_frontend"].get("ok", False)
                result["recovery_verified"] = verified
                if verified:
                    add_event(f"Recovery verification passed: {target_service} is running and frontend responds.", "Recovery Agent", "success")
                else:
                    add_event("Recovery is not fully verified. Manual transaction check is required.", "Recovery Agent", "error")
            except Exception as exc:
                result["recovery_verified"] = False
                result["recovery_error"] = str(exc)
                add_event(f"Recovery failed: {exc}. Manual intervention required.", "Recovery Agent", "error")
        else:
            result.setdefault("recovery_verified", False)
            result.setdefault("recovery_duration_seconds", 0)
        result["finished_at"] = utc_now()
        result["status"] = "recovered-needs-transaction-check" if result.get("recovery_verified") else "failed-or-unverified"
        state["experiment"]["status"] = result["status"]
        state["experiment"]["finished_at"] = result["finished_at"]
        state["experiment"]["result"] = result
        state["last_result"] = result
        state["active"] = False
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("INSERT OR REPLACE INTO experiments (id, started_at, finished_at, status, target, result_json) VALUES (?, ?, ?, ?, ?, ?)",
                         (experiment_id, started_at, result["finished_at"], result["status"], target_service, json.dumps(result)))
        add_event("Experiment finished. Open the banking UI and verify a real transaction before marking end-to-end recovery complete.", "Control Plane",
                  "success" if result.get("recovery_verified") else "warning")
