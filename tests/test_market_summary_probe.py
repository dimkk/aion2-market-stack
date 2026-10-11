import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location(
    "market_summary_probe", ROOT / "scripts" / "market_summary_probe.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def private_streams(path=module.PATH, authorization="jwt aaa.bbb.cccccccccccccccccccc"):
    if path == module.listing_probe.PATH:
        encoded = json.dumps({
            "game_server_id": "2301",
            "game_item_keys": "516150001",
            "page_no": 1,
            "page_size": 20,
            "sort": {"key": "SalePrice", "order_by": "ASC"},
        }, separators=(",", ":")).encode()
    else:
        encoded = b'{"game_server_id":2301}'
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
        "GET /trade/v1.0/policies?game_server_id=2301 HTTP/1.1\r\n"
        f"Host: {module.AUTHORITY}\r\n"
        f"Authorization: {authorization}\r\n"
        "Client-Context: private-client-context\r\n"
        "Client-Game-Context: private-game-context\r\n"
        "Device-Tag: private-device\r\n"
        "User-Agent: NcPlatformSdk/3.32.0\r\n\r\n"
    ).encode()
    return {"3": {"request": bytearray(request), "response": bytearray()}}


def summary_response():
    return json.dumps({
        "loaded": "private",
        "list": [
            {
                "game_item_key": "310330019",
                "min_unit_price": "3.1999999e+07",
                "sale_count": "3",
                "game_item_conditions": [
                    {"key": "Enchant", "type": "1", "value": "0"},
                    {"key": "ItemLevel", "type": "1", "value": "62"},
                ],
            }
        ],
    }).encode()


class MarketSummaryProbeTests(unittest.TestCase):
    def test_multi_server_probe_reuses_session_without_exporting_it(self):
        template = module._select_template(private_streams())
        observed = []

        def sender(headers, body):
            self.assertIn("authorization", headers)
            observed.append(json.loads(body)["game_server_id"])
            return 200, {"content-type": "application/json"}, summary_response()

        sleeps = []
        report = module.execute_multi_probe(
            template, [2301, 1301, 1101], "a" * 64, "b" * 64,
            sender=sender, sleeper=sleeps.append,
        )
        self.assertEqual([2301, 1301, 1101], observed)
        self.assertEqual([module.MIN_INTERVAL_SECONDS] * 2, sleeps)
        self.assertEqual(3, len(report["responses"]))
        self.assertEqual(9, report["privacy"]["max_requests"])
        self.assertEqual(3, report["transport_attempts"])
        self.assertEqual({}, template["headers"])
        encoded = json.dumps(report).lower()
        self.assertNotIn("private", encoded)
        self.assertNotIn("authorization", encoded)

    def test_multi_server_report_binds_actual_transport_attempt_count(self):
        template = module._select_template(private_streams())

        def sender(_headers, _body):
            return 200, {"content-type": "application/json"}, summary_response()

        report = module.execute_multi_probe(
            template,
            [2301, 1301, 1101],
            "a" * 64,
            "b" * 64,
            sender=sender,
            sleeper=lambda _seconds: None,
            transport_attempt_counter=lambda: 5,
        )
        self.assertEqual(5, report["transport_attempts"])
        self.assertEqual(9, report["privacy"]["max_requests"])

    def test_multi_server_target_batch_is_strict_and_digest_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "targets.json"
            target.write_text(json.dumps({
                "format": module.TARGET_BATCH_FORMAT,
                "generated_at": "2026-10-07T00:00:00Z",
                "registry_sha256": "b" * 64,
                "game_server_ids": [2301, 1301, 1101],
            }, separators=(",", ":")), encoding="utf-8")
            server_ids, digest, registry = module.load_target_batch(target)
        self.assertEqual([2301, 1301, 1101], server_ids)
        self.assertRegex(digest, r"^[0-9a-f]{64}$")
        self.assertEqual("b" * 64, registry)

    def test_mid_session_capture_stops_on_first_server_scoped_market_request(self):
        with (mock.patch.object(
                module.listing_probe.tls_contract, "_validate_bundle",
                return_value=(Path("capture"), Path("keys"))),
              mock.patch.object(
                module.listing_probe.tls_contract, "_keylog_match_counts",
                return_value={"matching_application_sessions": 0}),
              mock.patch.object(
                module.listing_probe.tls_contract, "_tls13_late_attach_plaintext",
                return_value=(private_policy_streams(), {})) as late):
            template = module._extract_template(Path("bundle"))
        self.assertEqual(
            b"game_server_id",
            late.call_args.args[2],
        )
        self.assertEqual(2301, template["body"]["game_server_id"])

    def test_fresh_probe_exports_only_normalized_summary(self):
        template = module._select_template(private_streams())
        observed = {}

        def sender(headers, body):
            observed["authorization"] = headers["authorization"]
            observed["body"] = json.loads(body)
            return 200, {"content-type": "application/json"}, summary_response()

        report = module.execute_probe(template, sender)
        self.assertEqual({"game_server_id": 2301}, observed["body"])
        self.assertEqual("31999999", report["response"]["summaries"][0]["min_unit_price"])
        self.assertEqual(3, report["response"]["summaries"][0]["sale_count"])
        self.assertEqual({}, template["headers"])
        encoded = json.dumps(report).lower()
        self.assertNotIn("private", encoded)
        self.assertNotIn("authorization", encoded)

    def test_listing_request_supplies_current_session_and_server_scope(self):
        template = module._select_template(private_streams(module.listing_probe.PATH))
        self.assertEqual({"game_server_id": 2301}, template["body"])
        self.assertIn("authorization", template["headers"])

    def test_policy_get_supplies_current_session_and_query_server_scope(self):
        template = module._select_template(private_policy_streams())
        self.assertEqual({"game_server_id": 2301}, template["body"])
        self.assertIn("authorization", template["headers"])

    def test_unknown_trade_path_is_not_a_template_source(self):
        streams = private_policy_streams()
        request = bytes(streams["3"]["request"]).replace(
            b"/trade/v1.0/policies?", b"/trade/v1.0/sales/delete?"
        )
        streams["3"]["request"] = bytearray(request)
        with self.assertRaisesRegex(ValueError, "market_session_template_not_found"):
            module._select_template(streams)

    def test_single_late_attach_accepts_listing_template(self):
        with (mock.patch.object(
                module.listing_probe.tls_contract, "_validate_bundle",
                return_value=(Path("capture"), Path("keys"))),
              mock.patch.object(
                module.listing_probe.tls_contract, "_keylog_match_counts",
                return_value={"matching_application_sessions": 0}),
              mock.patch.object(
                module.listing_probe.tls_contract, "_tls13_late_attach_plaintext",
                return_value=(private_streams(module.listing_probe.PATH), {})
              ) as late):
            template = module._extract_template(Path("bundle"))
        self.assertEqual(1, late.call_count)
        self.assertEqual(
            b"game_server_id", late.call_args.args[2]
        )
        self.assertEqual({"game_server_id": 2301}, template["body"])

    def test_extra_request_field_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "market_session_request_invalid"):
            module._request_body(b'{"game_server_id":2301,"page":1}')

    def test_auth_failure_clears_private_headers(self):
        template = module._select_template(private_streams())
        with self.assertRaisesRegex(ValueError, "market_session_auth_rejected"):
            module.execute_probe(
                template,
                lambda _headers, _body: (
                    401, {"content-type": "application/json"}, b'{"private":true}'
                ),
            )
        self.assertEqual({}, template["headers"])

    def test_rate_limit_is_distinct_and_clears_private_headers(self):
        template = module._select_template(private_streams())
        with self.assertRaisesRegex(ValueError, "market_session_rate_limited"):
            module.execute_probe(
                template,
                lambda _headers, _body: (
                    429, {"content-type": "application/json"}, b'{"private":true}'
                ),
            )
        self.assertEqual({}, template["headers"])

    def test_upstream_unavailable_is_distinct_and_clears_private_headers(self):
        template = module._select_template(private_streams())
        with self.assertRaisesRegex(
            ValueError, "market_session_upstream_unavailable"
        ):
            module.execute_probe(
                template,
                lambda _headers, _body: (
                    503, {"content-type": "application/json"}, b'{"private":true}'
                ),
            )
        self.assertEqual({}, template["headers"])

    def test_unknown_response_shape_fails_closed(self):
        template = module._select_template(private_streams())
        report = module.execute_probe(
            template,
            lambda _headers, _body: (
                200, {"content-type": "application/json"}, b'{"rows":[]}'
            ),
        )
        self.assertEqual(module.SHAPE_FORMAT, report["format"])
        self.assertEqual(["rows"], report["response_shape"]["top_level_fields"])
        self.assertEqual([], report["response_shape"]["arrays"][0]["sample_fields"])
        self.assertNotIn("authorization", json.dumps(report).lower())

    def test_cohort_specific_wrapper_name_is_accepted_by_row_contract(self):
        template = module._select_template(private_streams())
        wrapped = json.loads(summary_response())
        wrapped["minimum_prices"] = wrapped.pop("list")
        report = module.execute_probe(
            template,
            lambda _headers, _body: (
                200,
                {"content-type": "application/json"},
                json.dumps(wrapped).encode(),
            ),
        )
        self.assertEqual(1, report["response"]["summary_count"])

    def test_nested_summary_wrapper_is_accepted_by_row_contract(self):
        template = module._select_template(private_streams())
        wrapped = json.loads(summary_response())
        wrapped["data"] = {"rows": wrapped.pop("list")}
        report = module.execute_probe(
            template,
            lambda _headers, _body: (
                200,
                {"content-type": "application/json"},
                json.dumps(wrapped).encode(),
            ),
        )
        self.assertEqual(1, report["response"]["summary_count"])

    def test_single_root_summary_object_is_accepted(self):
        template = module._select_template(private_streams())
        root = json.loads(summary_response())["list"][0]
        report = module.execute_probe(
            template,
            lambda _headers, _body: (
                200,
                {"content-type": "application/json"},
                json.dumps(root).encode(),
            ),
        )
        self.assertEqual(1, report["response"]["summary_count"])

    def test_whole_market_rows_without_conditions_are_normalized(self):
        template = module._select_template(private_streams())
        root = json.loads(summary_response())
        del root["list"][0]["game_item_conditions"]
        report = module.execute_probe(
            template,
            lambda _headers, _body: (
                200,
                {"content-type": "application/json"},
                json.dumps(root).encode(),
            ),
        )
        self.assertEqual([], report["response"]["summaries"][0]["conditions"])

    def test_ambiguous_empty_lists_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "market_session_response_invalid"):
            module._normalize_response(
                200,
                {"content-type": "application/json"},
                b'{"first":[],"second":[]}',
            )


if __name__ == "__main__":
    unittest.main()
