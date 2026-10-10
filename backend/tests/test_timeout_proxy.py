import time
import unittest
from unittest.mock import patch

from fastapi import Request

import main


class ProxyExpiryTests(unittest.IsolatedAsyncioTestCase):
    async def test_expired_fault_forwards_without_recovery_call(self):
        request = Request({
            "type": "http", "method": "GET", "scheme": "http", "server": ("127.0.0.1", 8000),
            "path": "/proxy/", "raw_path": b"/proxy/", "query_string": b"", "headers": [],
        })
        response = type("UpstreamResponse", (), {
            "status_code": 200, "content": b"Stub bank", "headers": {"Content-Type": "text/plain"},
        })()
        original = main.fault_state.copy()
        try:
            main.fault_state.update(mode="timeout", hold_seconds=3, expires_at=time.monotonic() - 1)
            with patch.object(main.requests, "request", return_value=response) as upstream:
                proxied = await main.proxy(request)
            self.assertEqual(proxied.status_code, 200)
            self.assertEqual(proxied.body, b"Stub bank")
            upstream.assert_called_once()
        finally:
            main.fault_state.update(original)


class TimeoutDiagnosisTests(unittest.TestCase):
    def test_mixed_responses_are_reported_as_partial_and_unverified(self):
        def probe(status):
            return {"status_code": status, "error": status is None or status >= 500, "latency_ms": 10.0}

        result = {
            "phases": {
                "before": main.phase_metrics([probe(200)]),
                "during": main.phase_metrics([probe(504), probe(200), probe(None)]),
                "after": main.phase_metrics([probe(200), probe(502), probe(200)]),
            },
            "time_to_recover_seconds": 0.1,
            "recovered": False,
        }
        diagnosis = main.timeout_diagnosis(result, fault_activated=True)
        self.assertIn("1 of 3 fault-window probes", diagnosis["root_cause"])
        self.assertIn("total errors: 2", diagnosis["evidence"][1])
        self.assertIn("After: 3 probes, errors: 1", diagnosis["evidence"][2])
        self.assertIn("not verified", diagnosis["recommended_action"])
        self.assertNotIn("confidence", diagnosis)


if __name__ == "__main__":
    unittest.main()
