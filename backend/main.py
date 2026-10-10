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
SAFETY_THRESHOLD_SECONDS = 5
NETWORK_DELAY_MS = 800
NETWORK_JITTER_MS = 100
NETWORK_SAFETY_THRESHOLD_MS = 1500
NETWORK_EXPERIMENT_SECONDS = 10
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
    if service not in {"frontend", "userservice", "contacts", "ledgerwriter", "balancereader", "transactionhistory", "accounts-db", "ledger-db"}:
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
def service_latency_check(service: str = "ledgerwriter") -> dict[str, Any]:
    """
    Measure latency to a backend service from the frontend container.

    This intentionally measures traffic from another Compose container so
    that network impairment applied to the target's network namespace is
    observable.
    """
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
def apply_network_delay(target: Any) -> Any:
    """
    Start a temporary helper container sharing the target's network
    namespace and apply Linux netem latency to eth0.
    """
    helper = docker_client.containers.run(
        "nicolaka/netshoot:latest",
        command="sleep 60",
        detach=True,
        remove=False,
        network_mode=f"container:{target.id}",
        cap_add=["NET_ADMIN"],
        labels={
            "bankresilience.role": "network-safety-helper",
            "bankresilience.target": target.name,
        },
    )

    try:
        result = helper.exec_run(
            [
                "tc",
                "qdisc",
                "replace",
                "dev",
                "eth0",
                "root",
                "netem",
                "delay",
                f"{NETWORK_DELAY_MS}ms",
                f"{NETWORK_JITTER_MS}ms",
            ],
            stdout=True,
            stderr=True,
        )

        if result.exit_code != 0:
            output = result.output.decode("utf-8", errors="replace")
            raise RuntimeError(
                f"Failed to apply network delay: {output}"
            )

        return helper

    except Exception:
        try:
            helper.remove(force=True)
        except Exception:
            pass
        raise
def remove_network_delay(helper: Any) -> None:
    """Remove netem impairment and delete the temporary helper."""
    try:
        helper.exec_run(
            [
                "tc",
                "qdisc",
                "del",
                "dev",
                "eth0",
                "root",
            ],
            stdout=True,
            stderr=True,
        )
    finally:
        try:
            helper.remove(force=True)
        except Exception:
            pass

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
    if not payload.confirm:
        raise HTTPException(status_code=400, detail="Explicit confirmation is required to stop LedgerWriter.")
    if state["active"]:
        raise HTTPException(status_code=409, detail="An experiment is already running.")
    try:
        services = snapshot_containers()
        if any(s.get("status") == "unavailable" for s in services):
            raise RuntimeError(services[0].get("error", "Docker unavailable"))
        service_map = {s["service"]: s for s in services}
        required = ["frontend", "userservice", "contacts", "ledgerwriter", "balancereader", "transactionhistory", "accounts-db", "ledger-db"]
        missing = [name for name in required if name not in service_map]
        if missing:
            raise RuntimeError("Expected Compose services not found: " + ", ".join(missing))
        not_running = [name for name in required if service_map[name]["status"] != "running"]
        if not_running:
            raise RuntimeError("Healthy baseline required; these services are not running: " + ", ".join(not_running))
    except Exception as exc:
        raise HTTPException(status_code=409, detail=f"Preflight blocked the experiment: {exc}") from exc

    experiment_id = str(uuid.uuid4())[:8]
    started_at = utc_now()
    state["active"] = True
    state["experiment"] = {"id": experiment_id, "name": "LedgerWriter outage", "target": ALLOWED_FAULT_TARGET,
                           "status": "running", "started_at": started_at, "failure_window_seconds": FAILURE_WINDOW_SECONDS}
    state["events"] = []
    add_event("Healthy baseline passed: all eight expected services are running.", "Safety Controller")
    asyncio.create_task(run_experiment(experiment_id, started_at))
    return {"accepted": True, "id": experiment_id, "message": "Experiment started; the fault window is bounded and recovery runs automatically."}
async def run_network_slowdown_experiment(
    experiment_id: str,
    started_at: str,
) -> None:

    result: dict[str, Any] = {
        "id": experiment_id,
        "target": "ledgerwriter",
        "started_at": started_at,
        "experiment": "network-slowdown",
        "network_delay_ms": NETWORK_DELAY_MS,
        "network_jitter_ms": NETWORK_JITTER_MS,
        "events": [],
    }

    target = None
    helper = None

    try:
        # Baseline
        result["baseline_services"] = snapshot_containers()
        result["baseline_frontend"] = frontend_check()
        result["baseline_latency"] = service_latency_check("ledgerwriter")

        add_event(
            "Captured normal-operation baseline.",
            "Observability Agent",
        )

        baseline_latency = result["baseline_latency"].get("latency_ms")

        add_event(
            f"Baseline LedgerWriter latency: {baseline_latency} ms.",
            "Observability Agent",
        )

        # Get approved target.
        target = get_service_container("ledgerwriter")
        target.reload()

        if target.status != "running":
            raise RuntimeError(
                f"Refusing injection: ledgerwriter is not running "
                f"(status={target.status})."
            )

        # Apply network impairment.
        add_event(
            f"Injecting network slowdown: +{NETWORK_DELAY_MS}ms "
            f"latency with {NETWORK_JITTER_MS}ms jitter.",
            "Chaos Agent",
            "warning",
        )

        helper = apply_network_delay(target)

        state["experiment"]["phase"] = "network-fault-injected"

        add_event(
            "Network impairment active. Safety Monitor is measuring "
            "LedgerWriter response latency.",
            "Safety Controller",
            "warning",
        )

        fault_start = time.monotonic()
        max_latency = 0.0

        while True:
            elapsed = time.monotonic() - fault_start

            latency_check = service_latency_check("ledgerwriter")
            current_latency = latency_check.get("latency_ms")

            result.setdefault("latency_samples", []).append(
                {
                    "elapsed_seconds": round(elapsed, 2),
                    "latency": current_latency,
                    "ok": latency_check.get("ok", False),
                }
            )

            if current_latency is not None:
                max_latency = max(max_latency, current_latency)

            # Safety threshold.
            if (
                current_latency is not None
                and current_latency >= NETWORK_SAFETY_THRESHOLD_MS
            ):
                add_event(
                    f"Safety threshold breached: LedgerWriter latency "
                    f"reached {current_latency:.1f} ms.",
                    "Safety Controller",
                    "error",
                )

                add_event(
                    "KILL SWITCH ACTIVATED: removing network impairment.",
                    "Safety Controller",
                    "error",
                )

                state["experiment"]["phase"] = "safety-triggered"
                break

            if elapsed >= NETWORK_EXPERIMENT_SECONDS:
                add_event(
                    f"Network fault window completed after "
                    f"{elapsed:.1f}s.",
                    "Observability Agent",
                )
                break

            await asyncio.sleep(0.5)

        result["max_latency_ms"] = max_latency
        result["fault_latency"] = service_latency_check("ledgerwriter")
        result["fault_frontend"] = frontend_check()
        result["fault_services"] = snapshot_containers()

    except Exception as exc:
        result["error"] = str(exc)

        add_event(
            f"Experiment error: {exc}",
            "Control Plane",
            "error",
        )

    finally:
        recovery_start = time.monotonic()

        # ALWAYS remove network impairment.
        if helper is not None:
            try:
                add_event(
                    "Removing network impairment.",
                    "Recovery Agent",
                )

                remove_network_delay(helper)

                add_event(
                    "Network impairment removed.",
                    "Recovery Agent",
                    "success",
                )

            except Exception as exc:
                result["network_cleanup_error"] = str(exc)

                add_event(
                    f"Network cleanup failed: {exc}",
                    "Recovery Agent",
                    "error",
                )

        # Verify target and application after cleanup.
        await asyncio.sleep(1)

        result["recovery_latency"] = service_latency_check(
            "ledgerwriter"
        )
        result["recovery_frontend"] = frontend_check()
        result["recovery_services"] = snapshot_containers()

        recovery_latency = result["recovery_latency"].get(
            "latency_ms"
        )

        result["recovery_duration_seconds"] = round(
            time.monotonic() - recovery_start,
            2,
        )

        baseline_latency = (
            result.get("baseline_latency", {})
            .get("latency_ms")
        )

        latency_recovered = (
            recovery_latency is not None
            and (
                baseline_latency is None
                or recovery_latency < baseline_latency + 100
            )
        )

        result["recovery_verified"] = (
            result["recovery_frontend"].get("ok", False)
            and result["recovery_latency"].get("ok", False)
            and latency_recovered
        )

        if result["recovery_verified"]:
            add_event(
                f"Recovery verification passed: LedgerWriter latency "
                f"returned to {recovery_latency:.1f} ms.",
                "Recovery Agent",
                "success",
            )
        else:
            add_event(
                "Network recovery could not be fully verified.",
                "Recovery Agent",
                "error",
            )

        result["finished_at"] = utc_now()

        result["status"] = (
            "recovered"
            if result["recovery_verified"]
            else "failed-or-unverified"
        )

        state["experiment"]["status"] = result["status"]
        state["experiment"]["finished_at"] = result["finished_at"]
        state["experiment"]["result"] = result
        state["last_result"] = result
        state["active"] = False

        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO experiments
                (id, started_at, finished_at, status, target, result_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    experiment_id,
                    started_at,
                    result["finished_at"],
                    result["status"],
                    "ledgerwriter-network",
                    json.dumps(result),
                ),
            )

        add_event(
            "Network slowdown experiment finished.",
            "Control Plane",
            "success" if result["recovery_verified"] else "warning",
        )
@app.post("/api/experiments/network-slowdown")
async def start_network_slowdown_experiment(
    payload: StartExperimentRequest,
) -> dict[str, Any]:

    if not payload.confirm:
        raise HTTPException(
            status_code=400,
            detail="Explicit confirmation is required to inject network latency.",
        )

    if state["active"]:
        raise HTTPException(
            status_code=409,
            detail="An experiment is already running.",
        )

    try:
        services = snapshot_containers()

        service_map = {
            s["service"]: s
            for s in services
        }

        required = [
            "frontend",
            "userservice",
            "contacts",
            "ledgerwriter",
            "balancereader",
            "transactionhistory",
            "accounts-db",
            "ledger-db",
        ]

        missing = [
            name for name in required
            if name not in service_map
        ]

        if missing:
            raise RuntimeError(
                "Expected Compose services not found: "
                + ", ".join(missing)
            )

        not_running = [
            name for name in required
            if service_map[name]["status"] != "running"
        ]

        if not_running:
            raise RuntimeError(
                "Healthy baseline required; these services are not running: "
                + ", ".join(not_running)
            )

    except Exception as exc:
        raise HTTPException(
            status_code=409,
            detail=f"Preflight blocked the experiment: {exc}",
        ) from exc

    experiment_id = str(uuid.uuid4())[:8]
    started_at = utc_now()

    state["active"] = True
    state["experiment"] = {
        "id": experiment_id,
        "name": "Network slowdown",
        "target": "ledgerwriter",
        "status": "running",
        "started_at": started_at,
        "network_delay_ms": NETWORK_DELAY_MS,
        "network_jitter_ms": NETWORK_JITTER_MS,
    }
    state["events"] = []

    add_event(
        "Healthy baseline passed: all eight expected services are running.",
        "Safety Controller",
    )

    asyncio.create_task(
        run_network_slowdown_experiment(
            experiment_id,
            started_at,
        )
    )

    return {
        "accepted": True,
        "id": experiment_id,
        "message": (
            "Network slowdown experiment started; "
            "latency injection is bounded and automatically removed."
        ),
    }

async def run_experiment(experiment_id: str, started_at: str) -> None:
    result: dict[str, Any] = {
        "id": experiment_id,
        "target": ALLOWED_FAULT_TARGET,
        "started_at": started_at,
        "failure_window_seconds": FAILURE_WINDOW_SECONDS,
        "events": [],
    }

    target = None
    injected = False

    try:
        # Capture healthy baseline.
        before_services = snapshot_containers()
        result["baseline_services"] = before_services
        result["baseline_frontend"] = frontend_check()

        add_event(
            "Captured container and frontend baseline.",
            "Observability Agent",
        )

        # Get the approved target.
        target = get_service_container(ALLOWED_FAULT_TARGET)

        # Recheck the target immediately before the destructive operation.
        target.reload()

        if target.status != "running":
            raise RuntimeError(
                f"Refusing injection: {ALLOWED_FAULT_TARGET} is not running "
                f"(status={target.status})."
            )

        # Inject the controlled failure.
        add_event(
            "Injecting approved fault: stopping ledgerwriter.",
            "Chaos Agent",
            "warning",
        )

        target.stop(timeout=3)
        injected = True

        state["experiment"]["phase"] = "fault-injected"

        # Start the safety monitor.
        add_event(
            f"LedgerWriter is stopped. Safety Monitor armed with a "
            f"{SAFETY_THRESHOLD_SECONDS}s threshold.",
            "Safety Controller",
            "warning",
        )

        fault_start = time.monotonic()

        while True:
            elapsed = time.monotonic() - fault_start

            try:
                target.reload()
                current_status = target.status
            except Exception:
                current_status = "unavailable"

            # Service came back before the safety threshold.
            if current_status == "running":
                add_event(
                    f"LedgerWriter recovered after {elapsed:.1f}s.",
                    "Safety Controller",
                    "success",
                )
                break

            # Safety threshold exceeded.
            if elapsed >= SAFETY_THRESHOLD_SECONDS:
                add_event(
                    f"Safety threshold breached: LedgerWriter unavailable "
                    f"for {elapsed:.1f}s.",
                    "Safety Controller",
                    "error",
                )

                add_event(
                    "KILL SWITCH ACTIVATED: terminating fault window and "
                    "starting recovery.",
                    "Safety Controller",
                    "error",
                )

                state["experiment"]["phase"] = "safety-triggered"
                break

            await asyncio.sleep(0.5)

        # Capture evidence after the fault window.
        try:
            target.reload()
            fault_status = target.status
        except Exception as exc:
            fault_status = f"unavailable: {exc}"

        try:
            logs = target.logs(
                tail=100,
                timestamps=True,
            ).decode("utf-8", errors="replace")
        except Exception as exc:
            logs = f"Could not retrieve target logs: {exc}"

        result["fault_target_status"] = fault_status
        result["fault_frontend"] = frontend_check()
        result["fault_services"] = snapshot_containers()
        result["target_logs"] = logs[-12000:]

        add_event(
            f"Evidence captured. LedgerWriter status during fault: "
            f"{fault_status}.",
            "Observability Agent",
        )

        # Deterministic first-version diagnosis.
        diagnosis = {
            "root_cause": (
                "LedgerWriter service was deliberately stopped by "
                "the experiment."
            ),
            "confidence": (
                0.99 if fault_status == "exited" else 0.75
            ),
            "evidence": [
                f"Target container state during fault: {fault_status}",
                "The Chaos Agent recorded a successful stop action.",
                f"Frontend probe during fault: "
                f"{result['fault_frontend']}",
            ],
            "recommended_action": (
                "Start the allowlisted ledgerwriter container and "
                "verify service availability."
            ),
        }

        result["diagnosis"] = diagnosis
        state["experiment"]["diagnosis"] = diagnosis

        add_event(
            "Diagnosis: LedgerWriter is unavailable because the "
            "injected stop action succeeded.",
            "Diagnosis Agent",
            "warning",
        )

    except Exception as exc:
        result["error"] = str(exc)

        add_event(
            f"Experiment error: {exc}",
            "Control Plane",
            "error",
        )

    finally:
        recovery_start = time.monotonic()

        if injected and target is not None:
            try:
                add_event(
                    "Executing bounded recovery: starting ledgerwriter.",
                    "Recovery Agent",
                )

                target.start()

                deadline = time.monotonic() + 35
                last_status = "unknown"

                while time.monotonic() < deadline:
                    try:
                        target.reload()
                        last_status = target.status

                        if last_status == "running":
                            break

                    except Exception as exc:
                        last_status = str(exc)

                    await asyncio.sleep(1)

                result["recovery_container_status"] = last_status

                result["recovery_duration_seconds"] = round(
                    time.monotonic() - recovery_start,
                    2,
                )

                result["recovery_frontend"] = frontend_check()
                result["recovery_services"] = snapshot_containers()

                verified = (
                    last_status == "running"
                    and result["recovery_frontend"].get("ok", False)
                )

                result["recovery_verified"] = verified

                if verified:
                    add_event(
                        "Recovery verification passed: LedgerWriter "
                        "is running and frontend responds.",
                        "Recovery Agent",
                        "success",
                    )
                else:
                    add_event(
                        "Recovery is not fully verified. Manual "
                        "transaction check is required.",
                        "Recovery Agent",
                        "error",
                    )

            except Exception as exc:
                result["recovery_verified"] = False
                result["recovery_error"] = str(exc)

                add_event(
                    f"Recovery failed: {exc}. Manual intervention required.",
                    "Recovery Agent",
                    "error",
                )

        else:
            result.setdefault("recovery_verified", False)
            result.setdefault("recovery_duration_seconds", 0)

        result["finished_at"] = utc_now()

        if result.get("recovery_verified"):
            result["status"] = "recovered-needs-transaction-check"
        else:
            result["status"] = "failed-or-unverified"

        state["experiment"]["status"] = result["status"]
        state["experiment"]["finished_at"] = result["finished_at"]
        state["experiment"]["result"] = result
        state["last_result"] = result
        state["active"] = False

        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO experiments
                (id, started_at, finished_at, status, target, result_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    experiment_id,
                    started_at,
                    result["finished_at"],
                    result["status"],
                    ALLOWED_FAULT_TARGET,
                    json.dumps(result),
                ),
            )

        add_event(
            "Experiment finished. Open the banking UI and verify a "
            "real transaction before marking end-to-end recovery complete.",
            "Control Plane",
            "success" if result.get("recovery_verified") else "warning",
        )
