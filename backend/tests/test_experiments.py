import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import main


async def no_sleep(_seconds):
    return None


class FakeTarget:
    def __init__(self, service):
        self.service = service
        self.name = f"bank-{service}-1"
        self.status = "running"
        self.calls = []
        self.attrs = {
            "NetworkSettings": {
                "Networks": {
                    "bank_default": {
                        "NetworkID": "network-id",
                        "Aliases": [self.name, service],
                    }
                }
            }
        }

    def reload(self):
        return None

    def stop(self, timeout):
        self.calls.append(("stop", timeout))
        self.status = "exited"

    def start(self):
        self.calls.append(("start",))
        self.status = "running"

    def pause(self):
        self.calls.append(("pause",))
        self.status = "paused"

    def unpause(self):
        self.calls.append(("unpause",))
        self.status = "running"

    def logs(self, **_kwargs):
        return b"test log"


class FakeNetwork:
    def __init__(self):
        self.attrs = {"Labels": {"com.docker.compose.project": main.COMPOSE_PROJECT}}
        self.calls = []

    def disconnect(self, target):
        self.calls.append(("disconnect", target.service))
        target.attrs["NetworkSettings"]["Networks"].pop("bank_default")

    def connect(self, target, aliases):
        self.calls.append(("connect", target.service, aliases))
        target.attrs["NetworkSettings"]["Networks"]["bank_default"] = {
            "NetworkID": "network-id",
            "Aliases": aliases,
        }


class FakeNetworks:
    def __init__(self, network):
        self.network = network

    def get(self, name):
        if name != "bank_default":
            raise KeyError(name)
        return self.network


class FakeDockerClient:
    def __init__(self, network):
        self.networks = FakeNetworks(network)

    def ping(self):
        return True


class ExperimentRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = main.DB_PATH
        main.DB_PATH = Path(self.temp_dir.name) / "experiments.db"
        main.init_db()
        main.state.update({"active": True, "experiment": {}, "events": [], "last_result": None})

    def tearDown(self):
        main.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def services(self, target):
        return [
            {
                "service": service,
                "status": target.status if service == target.service else "running",
                "health": "n/a",
                "restarts": 0,
            }
            for service in main.EXPECTED_SERVICES
        ]

    async def run_case(self, experiment_id):
        spec = main.EXPERIMENTS[experiment_id]
        target = FakeTarget(spec["target"])
        network = FakeNetwork()
        client = FakeDockerClient(network)
        with (
            patch.object(main, "get_service_container", return_value=target),
            patch.object(main, "get_docker_client", return_value=client),
            patch.object(main, "snapshot_containers", side_effect=lambda: self.services(target)),
            patch.object(main, "frontend_check", return_value={"ok": True, "status_code": 200}),
            patch.object(main.asyncio, "sleep", new=no_sleep),
        ):
            await main.run_experiment("test-id", "2026-10-10T00:00:00Z", spec)
        return target, network, main.state["last_result"]

    async def test_crash_is_restarted_and_verified(self):
        target, _network, result = await self.run_case("ledgerwriter-outage")
        self.assertEqual(target.calls, [("stop", 3), ("start",)])
        self.assertTrue(result["recovery_verified"])
        self.assertTrue(result["all_services_running"])

    async def test_pause_is_unpaused_and_verified(self):
        target, _network, result = await self.run_case("contacts-pause")
        self.assertEqual(target.calls, [("pause",), ("unpause",)])
        self.assertTrue(result["recovery_verified"])

    async def test_network_is_reconnected_with_original_aliases(self):
        target, network, result = await self.run_case("transactionhistory-network-disconnect")
        self.assertEqual(network.calls[0], ("disconnect", "transactionhistory"))
        self.assertEqual(network.calls[1], ("connect", "transactionhistory", [target.name, "transactionhistory"]))
        self.assertTrue(result["network_recovered"])
        self.assertTrue(result["recovery_verified"])

    async def test_finally_recovers_when_evidence_collection_fails(self):
        spec = main.EXPERIMENTS["contacts-pause"]
        target = FakeTarget(spec["target"])
        checks = iter([
            {"ok": True, "status_code": 200},
            RuntimeError("probe failed"),
            {"ok": True, "status_code": 200},
        ])

        def frontend_check():
            result = next(checks)
            if isinstance(result, Exception):
                raise result
            return result

        with (
            patch.object(main, "get_service_container", return_value=target),
            patch.object(main, "snapshot_containers", side_effect=lambda: self.services(target)),
            patch.object(main, "frontend_check", side_effect=frontend_check),
            patch.object(main.asyncio, "sleep", new=no_sleep),
        ):
            await main.run_experiment("test-id", "2026-10-10T00:00:00Z", spec)

        self.assertEqual(target.calls, [("pause",), ("unpause",)])
        self.assertEqual(target.status, "running")
        self.assertFalse(main.state["active"])


if __name__ == "__main__":
    unittest.main()
