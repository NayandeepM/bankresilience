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


if __name__ == "__main__":
    unittest.main()
