import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[2]


def free_port():
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        return server.getsockname()[1]


def fetch(url, method="GET", payload=None, timeout=6):
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    request = Request(url, data=data, headers=headers, method=method)
    try:
        response = urlopen(request, timeout=timeout)
    except HTTPError as exc:
        response = exc
    with response:
        body = response.read()
        try:
            decoded = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            decoded = body.decode(errors="replace")
        return response.status, decoded


def wait_ready(url, process):
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError(f"Server exited early with code {process.returncode}")
        try:
            if fetch(url, timeout=1)[0] == 200:
                return
        except (URLError, TimeoutError):
            pass
        time.sleep(0.1)
    raise AssertionError(f"Server did not become ready at {url}")


class TimeoutIntegrationTests(unittest.TestCase):
    def test_real_http_fault_and_recovery_without_docker(self):
        stub_port = free_port()
        api_port = free_port()
        with tempfile.TemporaryDirectory() as directory:
            env = os.environ.copy()
            env.update(UPSTREAM_URL=f"http://127.0.0.1:{stub_port}",
                       BANKRESILIENCE_FRONTEND_URL=f"http://127.0.0.1:{stub_port}",
                       BANKRESILIENCE_DB_PATH=str(Path(directory) / "experiments.db"))
            stub = subprocess.Popen(
                [sys.executable, str(ROOT / "scripts" / "stub_bank.py"), "--port", str(stub_port)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            backend = None
            try:
                wait_ready(f"http://127.0.0.1:{stub_port}/", stub)
                backend = subprocess.Popen(
                    [sys.executable, "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", str(api_port)],
                    cwd=ROOT / "backend", env=env,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                api = f"http://127.0.0.1:{api_port}"
                wait_ready(api + "/api/experiments", backend)
                status, experiments = fetch(api + "/api/experiments")
                self.assertEqual(status, 200)
                self.assertIn("edge-timeout", [item["id"] for item in experiments])
                self.assertEqual(fetch(api + "/proxy/")[0], 200)
                self.assertEqual(fetch(api + "/proxy/", method="HEAD")[0], 200)
                self.assertEqual(fetch(api + "/api/experiments/edge-timeout", "POST", {"confirm": False})[0], 400)
                status, accepted = fetch(api + "/api/experiments/edge-timeout", "POST", {"confirm": True})
                self.assertEqual(status, 200)
                self.assertTrue(accepted["accepted"])
                self.assertEqual(fetch(api + "/api/experiments/edge-timeout", "POST", {"confirm": True})[0], 409)

                deadline = time.monotonic() + 40
                while time.monotonic() < deadline:
                    status, state = fetch(api + "/api/state")
                    if not state["active"]:
                        break
                    time.sleep(0.5)
                else:
                    self.fail("Timeout experiment did not finish within 40 seconds")

                result = state["last_result"]
                self.assertEqual(result["phases"]["before"]["requests"], 3)
                self.assertEqual(result["phases"]["during"]["requests"], 12)
                self.assertEqual(result["phases"]["after"]["requests"], 3)
                self.assertEqual(result["phases"]["during"]["status_counts"], {"504": 12})
                self.assertEqual(result["phases"]["after"]["errors"], 0)
                self.assertTrue(result["recovered"])
                self.assertIsNotNone(result["time_to_recover_seconds"])
                self.assertEqual(fetch(api + "/proxy/")[0], 200)
                status, history = fetch(api + "/api/history")
                self.assertEqual(status, 200)
                self.assertEqual(history[0]["result"]["id"], accepted["id"])
                print("timeout integration:", json.dumps({
                    "status": result["status"], "phases": result["phases"],
                    "requests": result["requests"], "errors": result["errors"],
                    "error_rate": result["error_rate"],
                    "time_to_recover_seconds": result["time_to_recover_seconds"],
                    "sqlite_saved": True,
                }), flush=True)
            finally:
                for process in (backend, stub):
                    if process is not None:
                        process.terminate()
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
