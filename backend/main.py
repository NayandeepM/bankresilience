from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import docker
import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

APP_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("BANKRESILIENCE_DB_PATH", str(APP_DIR / "experiments.db")))
COMPOSE_PROJECT = os.getenv("BANKRESILIENCE_COMPOSE_PROJECT", "bank-of-anthos-local")
FRONTEND_URL = os.getenv("BANKRESILIENCE_FRONTEND_URL", "http://localhost:8082")
UPSTREAM_URL = os.getenv("UPSTREAM_URL", "http://localhost:8082").rstrip("/")
TIMEOUT_HOLD_SECONDS = 3


def bounded_fault_window(raw_value: str) -> int:
    try:
        return min(12, max(1, int(raw_value)))
    except ValueError:
        return 12


FAILURE_WINDOW_SECONDS = bounded_fault_window(os.getenv("BANKRESILIENCE_FAULT_WINDOW_SECONDS", "12"))
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
    "edge-timeout": {
        "name": "Request timeout injection",
        "target": "local proxy",
        "failure_type": "timeout",
        "summary": "Hold traffic through the local proxy, return 504, and verify recovery without Docker.",
    },
}

app = FastAPI(title="BankResilience Control Plane", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)

docker_client: Any = None

state: dict[str, Any] = {
    "active": False,
    "experiment": None,
    "events": [],
    "last_result": None,
}
fault_state: dict[str, Any] = {"mode": "none", "hold_seconds": 0, "expires_at": 0.0}
timeout_start_lock = asyncio.Lock()
HOP_HEADERS = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer", "transfer-encoding", "upgrade", "host"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def init_db() -> None:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        with conn:
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


def save_result(result: dict[str, Any]) -> None:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO experiments (id, started_at, finished_at, status, target, result_json) VALUES (?, ?, ?, ?, ?, ?)",
                (result["id"], result["started_at"], result["finished_at"], result["status"], result["target"], json.dumps(result)),
            )


def clear_timeout_fault() -> None:
    fault_state.update(mode="none", hold_seconds=0, expires_at=0.0)


def proxy_headers(headers: Any) -> dict[str, str]:
    connection_tokens = {token.strip().lower() for token in headers.get("connection", "").split(",")}
    return {key: value for key, value in headers.items() if key.lower() not in HOP_HEADERS | connection_tokens}


@app.api_route("/proxy/{path:path}", methods=["GET", "HEAD"])
@app.api_route("/proxy", methods=["GET", "HEAD"])
async def proxy(request: Request, path: str = "") -> Response:
    if fault_state["mode"] == "timeout" and time.monotonic() < fault_state["expires_at"]:
        await asyncio.sleep(fault_state["hold_seconds"])
        return JSONResponse({"error": "Injected upstream timeout"}, status_code=504)
    query = f"?{request.url.query}" if request.url.query else ""
    upstream = f"{UPSTREAM_URL}/{path.lstrip('/')}{query}"
    try:
        response = await asyncio.to_thread(
            requests.request, request.method, upstream,
            headers=proxy_headers(request.headers), timeout=5, allow_redirects=False,
        )
    except requests.RequestException:
        return JSONResponse({"error": "Upstream unavailable"}, status_code=502)
    return Response(
        content=b"" if request.method == "HEAD" else response.content,
        status_code=response.status_code,
        headers={key: value for key, value in proxy_headers(response.headers).items()
                 if key.lower() not in {"content-length", "content-encoding"}},
    )


def get_docker_client() -> Any:
    global docker_client
    if docker_client is not None:
        try:
            docker_client.ping()
            return docker_client
        except Exception:
            docker_client = None

    connection_attempts = [lambda: docker.from_env()]
    desktop_sockets = [
        Path.home() / ".docker" / "run" / "docker.sock",
        Path.home() / ".docker" / "desktop" / "docker.sock",
    ]
    connection_attempts.extend(
        lambda socket_path=socket_path: docker.DockerClient(base_url=f"unix://{socket_path}")
        for socket_path in desktop_sockets
        if socket_path.exists()
    )

    last_error: Exception | None = None
    for connect in connection_attempts:
        try:
            candidate = connect()
            candidate.ping()
            docker_client = candidate
            return docker_client
        except Exception as exc:
            last_error = exc

    raise RuntimeError(
        "Docker SDK cannot connect. Ensure Docker Desktop is running and install backend requirements."
    ) from last_error


def get_project_containers() -> list[Any]:
    client = get_docker_client()
    containers = client.containers.list(all=True, filters={"label": f"com.docker.compose.project={COMPOSE_PROJECT}"})
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


def expected_services_are_running(services: list[dict[str, Any]]) -> bool:
    service_map = {service.get("service"): service for service in services}
    return all(service_map.get(name, {}).get("status") == "running" for name in EXPECTED_SERVICES)


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
        get_docker_client()
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
    with closing(sqlite3.connect(DB_PATH)) as conn:
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


@app.post("/api/experiments/edge-timeout")
async def start_edge_timeout_experiment(payload: StartExperimentRequest, request: Request) -> dict[str, Any]:
    spec = EXPERIMENTS["edge-timeout"]
    if not payload.confirm:
        raise HTTPException(status_code=400, detail=f"Explicit confirmation is required to run {spec['name']}.")
    async with timeout_start_lock:
        if state["active"]:
            raise HTTPException(status_code=409, detail="An experiment is already running.")
        if fault_state["mode"] == "timeout" and time.monotonic() < fault_state["expires_at"]:
            raise HTTPException(status_code=409, detail="A proxy timeout fault is already active.")
        try:
            upstream = await asyncio.to_thread(requests.get, UPSTREAM_URL + "/", timeout=3)
            if upstream.status_code >= 500:
                raise RuntimeError(f"HTTP {upstream.status_code}")
        except (requests.RequestException, RuntimeError) as exc:
            raise HTTPException(status_code=409, detail=f"Upstream {UPSTREAM_URL} is unreachable or unhealthy: {exc}") from exc
        if state["active"]:
            raise HTTPException(status_code=409, detail="An experiment is already running.")
        server = request.scope.get("server")
        if not server:
            raise HTTPException(status_code=409, detail="Cannot determine the local control plane port.")
        proxy_url = f"http://127.0.0.1:{server[1]}/proxy/"
        experiment_id = str(uuid.uuid4())[:8]
        started_at = utc_now()
        state.update(active=True, events=[], experiment={
            "id": experiment_id, "name": spec["name"], "target": spec["target"],
            "failure_type": spec["failure_type"], "status": "running", "started_at": started_at,
            "failure_window_seconds": 12,
        })
        add_event(f"Upstream preflight passed at {UPSTREAM_URL}.", "Safety Controller")
        asyncio.create_task(run_timeout_experiment(experiment_id, started_at, proxy_url))
    return {"accepted": True, "id": experiment_id, "message": "Timeout experiment started."}


async def probe_proxy(proxy_url: str, phase: str) -> dict[str, Any]:
    started = time.monotonic()
    try:
        response = await asyncio.to_thread(requests.get, proxy_url, timeout=5)
        status_code = response.status_code
        error = status_code >= 500
    except requests.RequestException as exc:
        status_code = None
        error = True
        detail = str(exc)
    observation: dict[str, Any] = {
        "phase": phase, "status_code": status_code, "error": error,
        "latency_ms": round((time.monotonic() - started) * 1000, 1),
    }
    if status_code is None:
        observation["detail"] = detail
    return observation


def phase_metrics(probes: list[dict[str, Any]]) -> dict[str, Any]:
    latencies = [probe["latency_ms"] for probe in probes]
    status_counts: dict[str, int] = {}
    for probe in probes:
        code = str(probe["status_code"]) if probe["status_code"] is not None else "exception"
        status_counts[code] = status_counts.get(code, 0) + 1
    return {
        "requests": len(probes),
        "errors": sum(probe["error"] for probe in probes),
        "status_counts": status_counts,
        "latency_min_ms": min(latencies, default=None),
        "latency_avg_ms": round(sum(latencies) / len(latencies), 1) if latencies else None,
        "latency_max_ms": max(latencies, default=None),
    }


async def run_timeout_experiment(experiment_id: str, started_at: str, proxy_url: str) -> None:
    spec = EXPERIMENTS["edge-timeout"]
    result: dict[str, Any] = {
        "id": experiment_id, "started_at": started_at, "name": spec["name"],
        "target": spec["target"], "failure_type": spec["failure_type"],
        "hold_seconds": TIMEOUT_HOLD_SECONDS, "failure_window_seconds": 12,
    }
    observations: dict[str, list[dict[str, Any]]] = {"before": [], "during": [], "after": []}
    outstanding: list[asyncio.Task] = []
    fault_activated = False
    try:
        for _ in range(3):
            observations["before"].append(await probe_proxy(proxy_url, "before"))
            await asyncio.sleep(1)
        if any(probe["error"] for probe in observations["before"]):
            raise RuntimeError("Proxy baseline failed before injection.")
        add_event("Three healthy proxy baseline probes recorded.", "Observability Agent")
        fault_state.update(mode="timeout", hold_seconds=TIMEOUT_HOLD_SECONDS,
                           expires_at=time.monotonic() + 12)
        fault_activated = True
        state["experiment"]["phase"] = "fault-injected"
        add_event("Injecting traffic-level timeout for 12 seconds; requests return 504 after a 3-second hold.",
                  "Chaos Agent", "warning")
        for _ in range(12):
            outstanding.append(asyncio.create_task(probe_proxy(proxy_url, "during")))
            await asyncio.sleep(1)
    except Exception as exc:
        result["error"] = str(exc)
        add_event(f"Timeout experiment error: {exc}", "Control Plane", "error")
    finally:
        cleared_at = time.monotonic()
        clear_timeout_fault()
        if fault_activated:
            add_event("Timeout fault cleared; verifying proxy recovery.", "Recovery Agent")
        for _ in range(3):
            observation = await probe_proxy(proxy_url, "after")
            observation["completed_at"] = time.monotonic()
            observations["after"].append(observation)
            await asyncio.sleep(1)
        if outstanding:
            done, pending = await asyncio.wait(outstanding, timeout=5)
            for task in done:
                if task.cancelled():
                    continue
                try:
                    observations["during"].append(task.result())
                except Exception as exc:
                    observations["during"].append({
                        "phase": "during", "status_code": None, "error": True,
                        "latency_ms": 0, "detail": str(exc),
                    })
            for task in pending:
                task.cancel()
            if pending:
                result["error"] = "Some fault-window probes did not complete within their timeout."
        result["phases"] = {phase: phase_metrics(probes) for phase, probes in observations.items()}
        result["requests"] = sum(phase["requests"] for phase in result["phases"].values())
        result["errors"] = sum(phase["errors"] for phase in result["phases"].values())
        result["error_rate"] = round(result["errors"] / result["requests"], 3) if result["requests"] else 0
        result["baseline_latency_ms"] = result["phases"]["before"]["latency_avg_ms"]
        result["latency_during_ms"] = {
            "avg": result["phases"]["during"]["latency_avg_ms"],
            "max": result["phases"]["during"]["latency_max_ms"],
        }
        first_healthy = next((probe for probe in observations["after"] if not probe["error"]), None)
        result["time_to_recover_seconds"] = (
            round(first_healthy["completed_at"] - cleared_at, 2) if first_healthy else None
        )
        result["recovered"] = bool(fault_activated and first_healthy and not result.get("error"))
        result["recovery_verified"] = result["recovered"]
        result["diagnosis"] = {
            "root_cause": "The local proxy deliberately delayed requests and returned HTTP 504.",
            "confidence": 1.0 if result["phases"]["during"]["errors"] else 0.0,
            "evidence": [
                f"Fault-window requests: {result['phases']['during']['requests']}",
                f"Fault-window errors: {result['phases']['during']['errors']}",
                f"Post-fault successful requests: {result['phases']['after']['requests'] - result['phases']['after']['errors']}",
            ],
            "recommended_action": "The proxy fault clears automatically; verify the upstream remains healthy.",
        }
        state["experiment"]["diagnosis"] = result["diagnosis"]
        result["status"] = "recovered-needs-transaction-check" if result["recovered"] else "failed-or-unverified"
        result["finished_at"] = utc_now()
        if result["recovered"]:
            add_event("Proxy recovered: a post-fault request succeeded.", "Recovery Agent", "success")
        else:
            add_event("Proxy recovery could not be verified.", "Recovery Agent", "error")
        state["experiment"].update(status=result["status"], finished_at=result["finished_at"], result=result)
        state["last_result"] = result
        try:
            save_result(result)
        finally:
            state["active"] = False


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
    candidate_names = [
        name for name, details in networks.items()
        if details.get("NetworkID") and name not in {"bridge", "host", "none"}
    ]
    if not candidate_names:
        raise RuntimeError(f"No Compose network found for {container.name}.")
    client = get_docker_client()
    for name in sorted(candidate_names):
        network = client.networks.get(name)
        labels = network.attrs.get("Labels", {}) or {}
        if labels.get("com.docker.compose.project") == COMPOSE_PROJECT:
            return name
    if len(candidate_names) == 1:
        return candidate_names[0]
    raise RuntimeError(
        f"Could not safely choose a Compose network for {container.name}: {', '.join(sorted(candidate_names))}."
    )


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
            get_docker_client().networks.get(network_name).disconnect(target)
        else:
            raise RuntimeError(f"Unknown failure type: {failure_type}")

        injected = True
        state["experiment"]["phase"] = "fault-injected"
        add_event(
            f"{spec['name']} fault is active. Collecting downstream evidence for "
            f"{FAILURE_WINDOW_SECONDS} seconds.",
            "Observability Agent",
            "warning",
        )
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
                    get_docker_client().networks.get(network_name).connect(target, aliases=network_aliases)
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
                result["all_services_running"] = expected_services_are_running(result["recovery_services"])
                result["network_recovered"] = network_recovered
                verified = (
                    last_status == "running"
                    and network_recovered
                    and result["all_services_running"]
                    and result["recovery_frontend"].get("ok", False)
                )
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
        with closing(sqlite3.connect(DB_PATH)) as conn:
            with conn:
                conn.execute("INSERT OR REPLACE INTO experiments (id, started_at, finished_at, status, target, result_json) VALUES (?, ?, ?, ?, ?, ?)",
                             (experiment_id, started_at, result["finished_at"], result["status"], target_service, json.dumps(result)))
        add_event("Experiment finished. Open the banking UI and verify a real transaction before marking end-to-end recovery complete.", "Control Plane",
                  "success" if result.get("recovery_verified") else "warning")
