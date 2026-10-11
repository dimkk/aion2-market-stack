from __future__ import annotations

import base64
import gzip
import random
import unittest
from io import BytesIO
from unittest.mock import patch

from economy_api import yandex_handler


class YandexHandlerTests(unittest.TestCase):
    def tearDown(self) -> None:
        yandex_handler._application = None
        yandex_handler._spa_index = None

    def test_bootstrap_failure_uses_stable_log_code_without_exception_details(self) -> None:
        marker = "PRIVATE-BOOTSTRAP-MARKER-4b12"
        event = {
            "path": "/api/v1/health",
            "requestContext": {"requestId": "safe-request-id"},
        }
        with self.assertLogs("aion2_economy_api", level="ERROR") as captured:
            with patch.object(
                yandex_handler,
                "_build_application",
                side_effect=RuntimeError(marker),
            ):
                response = yandex_handler.handler(event, object())

        rendered = "\n".join(captured.output)
        self.assertEqual(response["statusCode"], 503)
        self.assertNotIn(marker, rendered)
        self.assertNotIn("Traceback", rendered)
        self.assertIn("dependency_unavailable", rendered)
        self.assertEqual(
            response["headers"]["Strict-Transport-Security"],
            "max-age=31536000; includeSubDomains",
        )

    def test_root_spa_is_served_with_csp_and_frame_denial(self) -> None:
        event = {
            "path": "/",
            "httpMethod": "GET",
            "requestContext": {"requestId": "safe-request-id"},
        }
        with patch.dict("os.environ", {"SPA_BUCKET": "private-spa"}, clear=False):
            with patch.object(yandex_handler, "IamObjectClient") as client:
                client.return_value.get_object.side_effect = lambda **_: {
                    "Body": BytesIO(b"<!doctype html><title>AION2 Economy</title>")
                }
                response = yandex_handler.handler(event, object())

        self.assertEqual(response["statusCode"], 200)
        self.assertEqual(response["headers"]["X-Frame-Options"], "DENY")
        self.assertIn("frame-ancestors 'none'", response["headers"]["Content-Security-Policy"])
        self.assertEqual(
            response["headers"]["Strict-Transport-Security"],
            "max-age=31536000; includeSubDomains",
        )
        client.return_value.get_object.assert_called_once_with(
            Bucket="private-spa", Key="index.html"
        )

    def test_canonical_spa_routes_serve_the_same_private_shell(self) -> None:
        paths = (
            "/overview",
            "/items",
            "/items/516150001",
            "/items/2301/516150001",
            "/movers",
            "/servers",
            "/craft",
            "/farm",
            "/crawler",
            "/admin",
        )
        with patch.dict("os.environ", {"SPA_BUCKET": "private-spa"}, clear=False):
            with patch.object(yandex_handler, "IamObjectClient") as client:
                client.return_value.get_object.side_effect = lambda **_: {
                    "Body": BytesIO(b"<!doctype html><title>AION2 Economy</title>")
                }
                for path in paths:
                    yandex_handler._spa_index = None
                    response = yandex_handler.handler(
                        {"path": path, "httpMethod": "GET", "requestContext": {}},
                        object(),
                    )
                    self.assertEqual(response["statusCode"], 200, path)
                    self.assertEqual(response["headers"]["Cache-Control"], "no-store")

        self.assertEqual(client.return_value.get_object.call_count, len(paths))

    def test_unknown_non_api_route_fails_without_bootstrapping_dependencies(self) -> None:
        with patch.object(yandex_handler, "_build_application") as build:
            response = yandex_handler.handler(
                {"path": "/private/unknown", "httpMethod": "GET"}, object()
            )

        self.assertEqual(response["statusCode"], 404)
        self.assertEqual(response["body"], '{"error":"not_found"}')
        build.assert_not_called()

    def test_hashed_spa_asset_is_bounded_and_served_with_hsts(self) -> None:
        event = {
            "path": "/assets/index-AbCd1234.js",
            "httpMethod": "GET",
            "requestContext": {"requestId": "safe-request-id"},
        }
        with patch.dict("os.environ", {"SPA_BUCKET": "private-spa"}, clear=False):
            with patch.object(yandex_handler, "IamObjectClient") as client:
                client.return_value.get_object.return_value = {
                    "Body": BytesIO(b"console.log('safe')")
                }
                response = yandex_handler.handler(event, object())

        self.assertEqual(response["statusCode"], 200)
        self.assertTrue(response["isBase64Encoded"])
        self.assertNotIn("Content-Encoding", response["headers"])
        self.assertEqual(response["headers"]["Content-Type"], "text/javascript; charset=utf-8")
        self.assertEqual(
            response["headers"]["Cache-Control"],
            "public, max-age=31536000, immutable",
        )
        self.assertEqual(
            response["headers"]["Strict-Transport-Security"],
            "max-age=31536000; includeSubDomains",
        )
        client.return_value.get_object.assert_called_once_with(
            Bucket="private-spa", Key="assets/index-AbCd1234.js"
        )

    def test_asset_path_extension_and_content_hash_are_allowlisted(self) -> None:
        with patch.object(yandex_handler, "IamObjectClient") as client:
            for path in (
                "/assets/../secret",
                "/assets/private.exe",
                "/assets/runtime.js",
                "/assets/logo.svg",
                "/assets/logo-AbCd1234.svg",
                "/assets/index-short.js",
            ):
                response = yandex_handler.handler(
                    {"path": path, "httpMethod": "GET", "requestContext": {}}, object()
                )
                self.assertNotEqual(response["statusCode"], 200)
            client.assert_not_called()

    def test_large_api_json_is_gzipped_with_gateway_headroom_and_round_trips(self) -> None:
        body = '{"items":"' + ("item-516150001," * 200_000) + '"}'
        self.assertGreater(
            len(body.encode("utf-8")),
            yandex_handler._YANDEX_API_GATEWAY_RESPONSE_LIMIT_BYTES,
        )

        class Application:
            @staticmethod
            def handle(event: object, context: object) -> dict[str, object]:
                return {
                    "statusCode": 200,
                    "headers": {
                        "Content-Type": "application/json; charset=utf-8",
                        "Cache-Control": "private, max-age=60, must-revalidate",
                        "X-Content-Type-Options": "nosniff",
                        "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
                    },
                    "body": body,
                }

        yandex_handler._application = Application()  # type: ignore[assignment]
        response = yandex_handler.handler(
            {"path": "/api/v1/market/snapshot", "httpMethod": "GET"}, object()
        )

        compressed = base64.b64decode(response["body"], validate=True)
        self.assertEqual(response["statusCode"], 200)
        self.assertTrue(response["isBase64Encoded"])
        self.assertEqual(response["headers"]["Content-Encoding"], "gzip")
        self.assertEqual(response["headers"]["Vary"], "Accept-Encoding")
        self.assertEqual(
            response["headers"]["Cache-Control"],
            "private, max-age=60, must-revalidate",
        )
        self.assertLess(
            len(response["body"]),
            yandex_handler._GATEWAY_BASE64_BODY_BUDGET_BYTES,
        )
        self.assertLess(len(compressed), len(response["body"]))
        self.assertGreaterEqual(
            yandex_handler._YANDEX_API_GATEWAY_RESPONSE_LIMIT_BYTES
            - yandex_handler._GATEWAY_BASE64_BODY_BUDGET_BYTES,
            500_000,
        )
        self.assertEqual(gzip.decompress(compressed).decode("utf-8"), body)

    def test_poorly_compressible_api_json_fails_closed_below_gateway_limit(self) -> None:
        random_bytes = random.Random(42).randbytes(2_100_000)
        body = '{"blob":"' + base64.b64encode(random_bytes).decode("ascii") + '"}'
        self.assertGreater(
            len(body.encode("utf-8")),
            yandex_handler._YANDEX_API_GATEWAY_RESPONSE_LIMIT_BYTES,
        )

        class Application:
            @staticmethod
            def handle(event: object, context: object) -> dict[str, object]:
                return {
                    "statusCode": 200,
                    "headers": {
                        "Content-Type": "application/json; charset=utf-8",
                        "Cache-Control": "private, max-age=60, must-revalidate",
                        "X-Content-Type-Options": "nosniff",
                        "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
                    },
                    "body": body,
                }

        yandex_handler._application = Application()  # type: ignore[assignment]
        response = yandex_handler.handler(
            {"path": "/api/v1/economy/items", "httpMethod": "GET"}, object()
        )

        self.assertEqual(response["statusCode"], 503)
        self.assertEqual(response["body"], '{"error":"response_too_large"}')
        self.assertEqual(response["headers"]["Cache-Control"], "no-store")
        self.assertNotIn("Content-Encoding", response["headers"])
        self.assertNotIn("isBase64Encoded", response)
        self.assertLess(
            len(response["body"]),
            yandex_handler._GATEWAY_BASE64_BODY_BUDGET_BYTES,
        )

if __name__ == "__main__":
    unittest.main()
