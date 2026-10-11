import importlib.util
import json
from pathlib import Path
import sys
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location(
    "market_session_probe", ROOT / "scripts" / "market_session_probe.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def private_streams(path=module.PATH, authorization="jwt aaa.bbb.cccccccccccccccccccc"):
    body = {
        "game_server_id": "2301",
        "game_item_keys": "516150001",
        "page_no": 1,
        "page_size": 100,
        "sort": {"key": "SalePrice", "order_by": "ASC"},
    }
    encoded = json.dumps(body, separators=(",", ":")).encode()
    request = (
        f"POST {path} HTTP/1.1\r\n"
        f"Host: {module.AUTHORITY}\r\n"
        f"Authorization: {authorization}\r\n"
        "Client-Context: private-client-context\r\n"
        "Client-Game-Context: private-game-context\r\n"
        "Device-Tag: private-device\r\n"
        "User-Agent: NcPlatformSdk/3.32.0\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(encoded)}\r\n\r\n"
    ).encode() + encoded
    return {"7": {"request": bytearray(request), "response": bytearray()}}


def private_policy_streams(authorization="jwt aaa.bbb.cccccccccccccccccccc"):
    request = (
        "GET /trade/v1.0/policies?game_server_id=12301 HTTP/1.1\r\n"
        f"Host: {module.AUTHORITY}\r\n"
        f"Authorization: {authorization}\r\n"
        "Client-Context: private-client-context\r\n"
        "Client-Game-Context: private-game-context\r\n"
        "Device-Tag: private-device\r\n"
        "User-Agent: NcPlatformSdk/3.32.0\r\n\r\n"
    ).encode()
    return {"3": {"request": bytearray(request), "response": bytearray()}}


def response_body(extra=None):
    listing = {
        "sale_id": "FE46D413-932D-4421-A914-AEF941252784",
        "game_item_key": "516150001",
        "game_item_quantity": "1",
        "sale_price": "164",
        "unit_price": "164.0000",
        "effective_from": "2026-10-04T00:02:47.95+03:00",
        "effective_to": "2026-10-07T00:02:47.95+03:00",
        "category_id": "48",
        "grade_id": "2",
        "is_set_item": "0",
        "purchase_currency_item_key": "247",
        "purchase_currency_item_key_type": "2",
        "game_item_conditions": [{"key": "ItemLevel", "type": "1", "value": "0"}],
        "display_data": "opaque-private-data",
    }
    if extra:
        listing.update(extra)
    return json.dumps({"has_next_page": "0", "loaded": "private", "sales": [listing]}).encode()


class MarketSessionProbeTests(unittest.TestCase):
    def test_mid_session_capture_skips_unusable_tshark_follow_sweep(self):
        with (mock.patch.object(
                module.tls_contract, "_validate_bundle",
                return_value=(Path("capture"), Path("keys")),
              ),
              mock.patch.object(
                module.tls_contract, "_keylog_match_counts",
                return_value={"matching_application_sessions": 0},
              ),
              mock.patch.object(module.tls_contract, "_tls_follow_plaintext") as follow,
              mock.patch.object(
                module.tls_contract, "_tls13_late_attach_plaintext",
                return_value=(private_streams(), {}),
              ) as late):
            template = module._extract_template(Path("bundle"))
        follow.assert_not_called()
        self.assertEqual(
            b"game_server_id",
            late.call_args.args[2],
        )
        self.assertEqual("516150001", template["body"]["game_item_keys"])

    def test_market_policy_request_bootstraps_scannable_listing_contract(self):
        template = module._select_scannable_template(private_policy_streams())
        self.assertEqual("12301", template["body"]["game_server_id"])
        self.assertEqual("516150001", template["body"]["game_item_keys"])
        self.assertEqual(100, template["body"]["page_size"])
        self.assertIn("authorization", template["headers"])

    def test_fresh_probe_uses_memory_only_session_and_exports_normalized_listing(self):
        template = module._select_template(private_streams())
        observed = {}

        def sender(headers, body):
            observed["authorization"] = headers["authorization"]
            observed["body"] = json.loads(body)
            return 200, {"content-type": "application/json"}, response_body({"seller": "private"})

        report = module.execute_probe(template, sender)
        self.assertTrue(observed["authorization"].startswith("jwt "))
        self.assertEqual(observed["body"]["game_item_keys"], "516150001")
        self.assertEqual(report["response"]["listings"][0]["sale_price"], 164)
        self.assertEqual(report["request"]["sort"], {"key": "SalePrice", "order_by": "ASC"})
        self.assertEqual(template["headers"], {})
        encoded = json.dumps(report).lower()
        self.assertNotIn("private", encoded)
        self.assertNotIn("authorization", encoded)
        self.assertNotIn("seller", encoded)

    def test_non_listing_path_is_not_a_session_template(self):
        with self.assertRaisesRegex(ValueError, "market_session_template_not_found"):
            module._select_template(private_streams("/trade/v1.0/sales/buy"))

    def test_missing_jwt_is_rejected_before_network(self):
        with self.assertRaisesRegex(ValueError, "market_session_auth_missing"):
            module._select_template(private_streams(authorization="Bearer private"))

    def test_uppercase_scheme_and_padded_compact_jwt_are_accepted(self):
        authorization = (
            "JWT eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9."
            "eyJhY2Nlc3NfdG9rZW4iOiJ4IiwiaWF0IjoiMSIsImlzcyI6IngiLCJ0eXAiOiJ4In0=."
            "YWJjZA=="
        )
        template = module._select_template(private_streams(authorization=authorization))
        self.assertEqual(authorization, template["headers"]["authorization"])

    def test_older_complete_request_wins_over_unauthenticated_tail(self):
        streams = private_streams()
        streams["8"] = private_streams(authorization="Bearer partial")["7"]
        template = module._select_template(streams)
        self.assertTrue(template["headers"]["authorization"].startswith("jwt "))
        self.assertEqual("516150001", template["body"]["game_item_keys"])

    def test_auth_failure_does_not_export_response_body(self):
        template = module._select_template(private_streams())
        with self.assertRaisesRegex(ValueError, "market_session_auth_rejected"):
            module.execute_probe(
                template,
                lambda _headers, _body: (
                    401, {"content-type": "application/json"}, b'{"token":"private"}'
                ),
            )
        self.assertEqual(template["headers"], {})

    def test_direct_vm_egress_is_rejected_without_fixed_relay(self):
        with (mock.patch.dict(module.os.environ, {}, clear=True),
              self.assertRaisesRegex(ValueError, "market_session_relay_missing")):
            module._https_post({}, b"{}")

    def test_mutating_or_unknown_request_field_is_rejected(self):
        value = {
            "game_server_id": "2301", "game_item_keys": "516150001",
            "page_no": 1, "page_size": 100,
            "sort": {"key": "SalePrice", "order_by": "ASC"},
            "purchase": True,
        }
        with self.assertRaisesRegex(ValueError, "market_session_request_invalid"):
            module._request_body(json.dumps(value).encode())


if __name__ == "__main__":
    unittest.main()
