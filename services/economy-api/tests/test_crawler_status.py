from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime

from economy_api.crawler_status import CrawlerStatusError, decode_crawler_status


REVISION = "b25650019-20261009T120000Z"
RELEASE = "a" * 40
NOW = datetime(2026, 10, 9, 12, 30, tzinfo=UTC)


def published_status(**overrides: object) -> bytes:
    last_success = {
        "run_id": "market-" + "1" * 32,
        "observed_at_utc": "2026-10-09T12:00:00Z",
        "revision": REVISION,
        "release": RELEASE,
        "server_count": 100,
        "request_count": 100,
        "transport_attempt_count": 117,
        "summary_rows": 26_590,
        "snapshot_changed": True,
    }
    value: dict[str, object] = {
        "schema_version": "aion2.market-autorefresh.status/v1",
        "observed_at_utc": "2026-10-09T12:00:00Z",
        "state": "published",
        "trigger": "scheduled",
        "run_id": "market-" + "2" * 32,
        "revision": REVISION,
        "release": RELEASE,
        "server_count": 100,
        "request_count": 100,
        "transport_attempt_count": 117,
        "summary_rows": 26_590,
        "snapshot_changed": True,
        "packet_bus_segments": 3,
        "next_run_utc": "2026-10-09T13:00:00Z",
        "last_success": last_success,
    }
    value.update(overrides)
    return json.dumps(value, separators=(",", ":")).encode()


class CrawlerStatusTests(unittest.TestCase):
    def test_published_status_maps_exact_revision_and_read_only_controls(self) -> None:
        value = decode_crawler_status(published_status(), REVISION, now=NOW)

        self.assertEqual("standalone_object", value["source"])
        self.assertFalse(value["controlAvailable"])
        self.assertEqual(REVISION, value["economyRevision"])
        self.assertEqual("completed", value["status"])
        self.assertEqual(100, value["processedItems"])
        self.assertEqual(100, value["successfulRequests"])
        self.assertEqual(117, value["transportAttempts"])
        self.assertEqual(17, value["retriedRequests"])
        self.assertEqual(REVISION, value["lastSuccess"]["economyRevision"])
        self.assertEqual(100, value["lastSuccess"]["marketScopeCount"])
        self.assertEqual(117, value["lastSuccess"]["transportAttemptCount"])
        self.assertNotIn("serverCount", value["lastSuccess"])
        self.assertEqual("market-" + "2" * 32, value["runId"])

    def test_v2_completed_requires_bound_readback_and_api_release(self) -> None:
        with self.assertRaisesRegex(
            CrawlerStatusError, "crawler_status_readback_evidence_invalid"
        ):
            decode_crawler_status(
                published_status(
                    schema_version="aion2.market-autorefresh.status/v2"
                ),
                REVISION,
                now=NOW,
            )
        value = decode_crawler_status(
            published_status(
                schema_version="aion2.market-autorefresh.status/v2",
                scan_observed_at_utc="2026-10-09T12:00:00Z",
                readback_verified=True,
                api_release_sha256="d" * 64,
                last_success={
                    "run_id": "market-" + "2" * 32,
                    "observed_at_utc": "2026-10-09T12:00:00Z",
                    "revision": REVISION,
                    "release": RELEASE,
                    "server_count": 100,
                    "request_count": 100,
                    "summary_rows": 26_590,
                    "snapshot_changed": True,
                },
            ),
            REVISION,
            now=NOW,
        )
        self.assertEqual("completed", value["status"])

    def test_pointer_revision_mismatch_is_sanitized_pending_degradation(self) -> None:
        document = published_status(
            revision="b25650019-other",
            last_success={
                "run_id": "market-" + "1" * 32,
                "observed_at_utc": "2026-10-09T12:00:00Z",
                "revision": "b25650019-other",
                "release": RELEASE,
                "server_count": 100,
                "request_count": 100,
                "summary_rows": 26_590,
                "snapshot_changed": True,
            },
        )

        value = decode_crawler_status(document, REVISION, now=NOW)

        self.assertEqual("failed", value["status"])
        self.assertEqual(REVISION, value["economyRevision"])
        self.assertEqual(
            "crawler_status_revision_pending", value["errors"][0]["code"]
        )

    def test_rejects_unknown_fields_and_unbounded_error_codes(self) -> None:
        with self.assertRaisesRegex(CrawlerStatusError, "crawler_status_shape_invalid"):
            decode_crawler_status(published_status(raw_headers={}), REVISION, now=NOW)
        with self.assertRaisesRegex(
            CrawlerStatusError, "crawler_status_error_code_invalid"
        ):
            decode_crawler_status(
                published_status(error_code="Bearer private-value"),
                REVISION,
                now=NOW,
            )

    def test_failed_status_can_report_last_good_revision_without_raw_details(self) -> None:
        document = published_status(
            state="failed_waiting_next_cycle",
            trigger="scheduled",
            error_code="scan_timeout",
            revision=None,
            release=None,
            server_count=None,
            request_count=None,
            summary_rows=None,
            snapshot_changed=None,
            packet_bus_segments=None,
        )

        value = decode_crawler_status(document, REVISION, now=NOW)

        self.assertEqual("failed", value["status"])
        self.assertEqual("scan_timeout", value["errors"][0]["code"])
        self.assertNotIn("raw", json.dumps(value))

    def test_failed_publication_reports_publish_phase_truthfully(self) -> None:
        document = published_status(
            state="failed_waiting_next_cycle",
            error_code="api_readback_failed",
            failure_phase="publish",
            revision=None,
            release=None,
            server_count=None,
            request_count=None,
            summary_rows=None,
            snapshot_changed=None,
            packet_bus_segments=None,
        )

        value = decode_crawler_status(document, REVISION, now=NOW)

        self.assertEqual("failed", value["status"])
        self.assertEqual("publish", value["phase"])
        self.assertEqual("api_readback_failed", value["errors"][0]["code"])

    def test_failure_phase_is_rejected_outside_failed_state(self) -> None:
        with self.assertRaisesRegex(
            CrawlerStatusError, "crawler_status_failure_phase_invalid"
        ):
            decode_crawler_status(
                published_status(failure_phase="publish"), REVISION, now=NOW
            )

    def test_rejects_observed_at_beyond_explicit_future_skew(self) -> None:
        with self.assertRaisesRegex(
            CrawlerStatusError, "crawler_status_observed_at_future"
        ):
            decode_crawler_status(
                published_status(observed_at_utc="2026-10-09T12:33:00Z"),
                REVISION,
                now=NOW,
            )

    def test_hourly_status_is_degraded_after_next_run_grace(self) -> None:
        value = decode_crawler_status(
            published_status(),
            REVISION,
            now=datetime(2026, 10, 9, 13, 10, 1, tzinfo=UTC),
        )

        self.assertEqual("failed", value["status"])
        self.assertEqual("crawler_status_stale", value["errors"][0]["code"])
        self.assertEqual(REVISION, value["lastSuccess"]["economyRevision"])

    def test_rejects_unbounded_next_run_that_would_mask_staleness(self) -> None:
        with self.assertRaisesRegex(
            CrawlerStatusError, "crawler_status_next_run_invalid"
        ):
            decode_crawler_status(
                published_status(next_run_utc="2026-10-10T12:00:00Z"),
                REVISION,
                now=NOW,
            )

    def test_maps_real_run_timing_and_server_progress(self) -> None:
        document = published_status(
            state="scanning",
            observed_at_utc="2026-10-09T12:20:00Z",
            started_at_utc="2026-10-09T12:05:00Z",
            elapsed_seconds=900,
            revision=None,
            release=None,
            server_count=42,
            request_count=40,
            summary_rows=None,
            snapshot_changed=None,
            packet_bus_segments=2,
        )

        value = decode_crawler_status(document, REVISION, now=NOW)

        self.assertEqual("running", value["status"])
        self.assertEqual("2026-10-09T12:05:00Z", value["startedAt"])
        self.assertEqual(900, value["elapsedSeconds"])
        self.assertEqual(42, value["processedItems"])
        self.assertEqual(58, value["queueDepth"])

    def test_publishing_preserves_attempt_identity_and_phase(self) -> None:
        value = decode_crawler_status(
            published_status(
                state="publishing",
                revision=REVISION,
                snapshot_changed=None,
            ),
            REVISION,
            now=NOW,
        )

        self.assertEqual("running", value["status"])
        self.assertEqual("publish", value["phase"])
        self.assertEqual("market-" + "2" * 32, value["runId"])

    def test_timing_pair_is_strict_and_stopped_maps_to_paused(self) -> None:
        with self.assertRaisesRegex(
            CrawlerStatusError, "crawler_status_timing_invalid"
        ):
            decode_crawler_status(
                published_status(started_at_utc="2026-10-09T11:59:00Z"),
                REVISION,
                now=NOW,
            )

        stopped = decode_crawler_status(
            published_status(state="stopped"), REVISION, now=NOW
        )
        self.assertEqual("paused", stopped["status"])


if __name__ == "__main__":
    unittest.main()
