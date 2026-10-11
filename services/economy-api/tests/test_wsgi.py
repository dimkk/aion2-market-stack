from __future__ import annotations

import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from economy_api import wsgi


class WsgiAdapterTest(unittest.TestCase):
    def _call(self, path: str) -> tuple[str, dict[str, str], bytes]:
        captured: dict[str, object] = {}

        def start_response(status, headers):
            captured["status"] = status
            captured["headers"] = dict(headers)

        body = b"".join(
            wsgi.application(
                {
                    "REQUEST_METHOD": "GET",
                    "PATH_INFO": path,
                    "QUERY_STRING": "",
                    "CONTENT_LENGTH": "0",
                    "REMOTE_ADDR": "127.0.0.1",
                    "wsgi.input": io.BytesIO(b""),
                },
                start_response,
            )
        )
        return str(captured["status"]), captured["headers"], body

    def test_spa_history_fallback_and_security_headers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "index.html").write_text("<html>ok</html>", encoding="utf-8")
            with patch.object(wsgi, "_STATIC_ROOT", root):
                status, headers, body = self._call("/items/516150001")
            self.assertEqual(status, "200 OK")
            self.assertEqual(body, b"<html>ok</html>")
            self.assertEqual(headers["Cache-Control"], "no-store")
            self.assertEqual(headers["X-Frame-Options"], "DENY")

    def test_missing_hashed_asset_is_not_spa(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "index.html").write_text("ok", encoding="utf-8")
            with patch.object(wsgi, "_STATIC_ROOT", root):
                status, _headers, body = self._call("/assets/missing-deadbeef.js")
            self.assertEqual(status, "404 Not Found")
            self.assertEqual(body, b"not found")

    def test_item_asset_is_served_from_separate_client_derived_root(self) -> None:
        with (
            tempfile.TemporaryDirectory() as static_directory,
            tempfile.TemporaryDirectory() as asset_directory,
        ):
            static_root = Path(static_directory)
            asset_root = Path(asset_directory)
            (static_root / "index.html").write_text("spa", encoding="utf-8")
            file_name = "item-42-0123456789abcdef.png"
            (asset_root / file_name).write_bytes(b"synthetic-png")
            with (
                patch.object(wsgi, "_STATIC_ROOT", static_root),
                patch.object(wsgi, "_ITEM_ASSET_ROOT", asset_root),
            ):
                status, headers, body = self._call(f"/assets/{file_name}")
            self.assertEqual(status, "200 OK")
            self.assertEqual(headers["Content-Type"], "image/png")
            self.assertEqual(body, b"synthetic-png")

    def test_loopback_proxy_uses_only_last_valid_forwarded_address(self) -> None:
        event = wsgi._event(
            {
                "REQUEST_METHOD": "POST",
                "PATH_INFO": "/api/v1/auth/login",
                "REMOTE_ADDR": "127.0.0.1",
                "HTTP_X_FORWARDED_FOR": "198.51.100.77, 203.0.113.19",
            },
            b"",
        )
        self.assertEqual(
            event["requestContext"]["identity"]["sourceIp"], "203.0.113.19"
        )

    def test_direct_request_does_not_trust_forwarded_address(self) -> None:
        event = wsgi._event(
            {
                "REQUEST_METHOD": "GET",
                "PATH_INFO": "/api/v1/health",
                "REMOTE_ADDR": "203.0.113.20",
                "HTTP_X_FORWARDED_FOR": "198.51.100.9",
            },
            b"",
        )
        self.assertEqual(
            event["requestContext"]["identity"]["sourceIp"], "203.0.113.20"
        )


if __name__ == "__main__":
    unittest.main()
