from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any


SCHEMAS = frozenset(
    {"aion2.market-autorefresh.status/v1", "aion2.market-autorefresh.status/v2"}
)
MAX_DOCUMENT_BYTES = 65_536
MAX_FUTURE_SKEW_SECONDS = 120
HOURLY_CADENCE_SECONDS = 3_600
STATUS_LATE_GRACE_SECONDS = 600
MAX_NEXT_RUN_AHEAD_SECONDS = 7_200

_STATES = frozenset(
    {
        "disabled",
        "stopped",
        "preflight",
        "scanning",
        "publishing",
        "verifying",
        "published",
        "failed_waiting_next_cycle",
        "manual_refresh_ignored_disabled",
        "manual_refresh_ignored_cooldown",
        "supervisor_restarting",
        "supervisor_fault",
    }
)
_TRIGGERS = frozenset({"startup", "scheduled", "manual", "control", "supervisor"})
_ERROR_CODES = frozenset(
    {
        "aion2_process_missing",
        "aion2_process_not_unique",
        "aion2_process_path_invalid",
        "atomic_promotion_verification_failed",
        "execution_closure_hash_mismatch",
        "external_executable_invalid",
        "installed_runtime_invalid",
        "installed_runtime_incompatible_active",
        "installed_runtime_required",
        "integrity_level_not_medium",
        "internal_error",
        "internal_worker_auth_invalid",
        "job_assignment_failed",
        "live_input_acl_invalid",
        "market_packet_bus_for_listener_missing",
        "market_packet_bus_not_growing",
        "market_packet_bus_session_invalid",
        "market_packet_listener_not_unique",
        "market_packet_listener_path_invalid",
        "market_session_cleanup_failed",
        "market_session_snapshot_invalid",
        "market_tls_keylog_empty",
        "market_tls_keylog_not_unique",
        "owner_only_state_invalid",
        "path_scope_invalid",
        "pending_publication_invalid",
        "process_elevated",
        "project_root_mismatch",
        "scan_process_failed",
        "scan_stderr_not_empty",
        "scan_timeout",
        "status_promotion_failed",
        "status_publication_failed",
        "api_readback_failed",
        "target_batch_format_invalid",
        "target_batch_hash_mismatch",
        "target_batch_missing",
        "target_batch_scope_invalid",
        "terminal_receipt_count_invalid",
        "terminal_receipt_invalid",
        "terminal_receipt_not_final",
        "terminal_receipt_parse_failed",
        "terminal_receipt_shape_invalid",
        "terminal_receipt_values_invalid",
        "worker_not_in_job",
        "worker_singleton_unavailable",
    }
)
_RELEASE_RE = re.compile(r"^[0-9a-f]{40}$")
_REVISION_RE = re.compile(r"^b[0-9]{1,12}-[A-Za-z0-9._-]{1,96}$")
_ROOT_KEYS = frozenset(
    {
        "schema_version",
        "observed_at_utc",
        "scan_observed_at_utc",
        "started_at_utc",
        "elapsed_seconds",
        "state",
        "trigger",
        "run_id",
        "error_code",
        "failure_phase",
        "revision",
        "release",
        "server_count",
        "request_count",
        "transport_attempt_count",
        "summary_rows",
        "snapshot_changed",
        "packet_bus_segments",
        "next_run_utc",
        "minimum_next_attempt_utc",
        "manual_cooldown_until_utc",
        "last_success",
        "readback_verified",
        "api_release_sha256",
    }
)
_LAST_SUCCESS_KEYS = frozenset(
    {
        "observed_at_utc",
        "run_id",
        "revision",
        "release",
        "server_count",
        "request_count",
        "transport_attempt_count",
        "summary_rows",
        "snapshot_changed",
    }
)


class CrawlerStatusError(ValueError):
    pass


def _timestamp_value(value: object) -> tuple[str, datetime]:
    if not isinstance(value, str) or not 1 <= len(value) <= 40:
        raise CrawlerStatusError("crawler_status_timestamp_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CrawlerStatusError("crawler_status_timestamp_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CrawlerStatusError("crawler_status_timestamp_invalid")
    parsed = parsed.astimezone(UTC)
    return parsed.isoformat().replace("+00:00", "Z"), parsed


def _timestamp(value: object) -> str:
    return _timestamp_value(value)[0]


def _optional_timestamp(value: object) -> str | None:
    return None if value is None else _timestamp(value)


def _optional_int(value: object, maximum: int) -> int | None:
    if value is None:
        return None
    if type(value) is not int or not 0 <= value <= maximum:
        raise CrawlerStatusError("crawler_status_counter_invalid")
    return value


def _optional_bool(value: object) -> bool | None:
    if value is not None and type(value) is not bool:
        raise CrawlerStatusError("crawler_status_boolean_invalid")
    return value


def _revision(value: object) -> str:
    if not isinstance(value, str) or _REVISION_RE.fullmatch(value) is None:
        raise CrawlerStatusError("crawler_status_revision_invalid")
    return value


def _release(value: object) -> str:
    if not isinstance(value, str) or _RELEASE_RE.fullmatch(value) is None:
        raise CrawlerStatusError("crawler_status_release_invalid")
    return value


def _last_success(value: object) -> dict[str, Any] | None:
    if value is None:
        return None
    if (
        not isinstance(value, dict)
        or set(value) not in {
            _LAST_SUCCESS_KEYS,
            _LAST_SUCCESS_KEYS - {"run_id"},
            _LAST_SUCCESS_KEYS - {"transport_attempt_count"},
            _LAST_SUCCESS_KEYS - {"run_id", "transport_attempt_count"},
        }
    ):
        raise CrawlerStatusError("crawler_status_last_success_invalid")
    success_run_id = value.get("run_id")
    if success_run_id is not None and (
        not isinstance(success_run_id, str)
        or re.fullmatch(r"market-[0-9a-f]{32}", success_run_id) is None
    ):
        raise CrawlerStatusError("crawler_status_last_success_invalid")
    revision = _revision(value["revision"])
    server_count = _optional_int(value["server_count"], 100)
    request_count = _optional_int(value["request_count"], 100)
    transport_attempt_count = _optional_int(
        value.get("transport_attempt_count"), 300
    )
    if transport_attempt_count is None:
        transport_attempt_count = request_count
    summary_rows = _optional_int(value["summary_rows"], 10_000_000)
    snapshot_changed = _optional_bool(value["snapshot_changed"])
    if (
        server_count != 100
        or request_count != 100
        or transport_attempt_count is None
        or transport_attempt_count < request_count
        or summary_rows is None
        or summary_rows <= 0
        or snapshot_changed is None
    ):
        raise CrawlerStatusError("crawler_status_last_success_invalid")
    return {
        "runId": success_run_id,
        "observedAt": _timestamp(value["observed_at_utc"]),
        "economyRevision": revision,
        "release": _release(value["release"]),
        # These are registry-defined market/faction scopes.  They must not be
        # presented as a count of physical game servers.
        "marketScopeCount": server_count,
        "requestCount": request_count,
        "transportAttemptCount": transport_attempt_count,
        "summaryRows": summary_rows,
        "snapshotChanged": snapshot_changed,
    }


def decode_crawler_status(
    document: bytes, current_revision: str, *, now: datetime
) -> dict[str, Any]:
    if not document or len(document) > MAX_DOCUMENT_BYTES:
        raise CrawlerStatusError("crawler_status_document_invalid")
    try:
        value = json.loads(document)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CrawlerStatusError("crawler_status_document_invalid") from exc
    if not isinstance(value, dict) or not set(value).issubset(_ROOT_KEYS):
        raise CrawlerStatusError("crawler_status_shape_invalid")
    if not {"schema_version", "observed_at_utc", "state", "trigger"}.issubset(value):
        raise CrawlerStatusError("crawler_status_shape_invalid")
    if value["schema_version"] not in SCHEMAS or value["state"] not in _STATES or value["trigger"] not in _TRIGGERS:
        raise CrawlerStatusError("crawler_status_contract_invalid")

    if now.tzinfo is None or now.utcoffset() is None:
        raise CrawlerStatusError("crawler_status_server_time_invalid")
    server_now = now.astimezone(UTC)
    observed_at, observed_at_value = _timestamp_value(value["observed_at_utc"])
    if (observed_at_value - server_now).total_seconds() > MAX_FUTURE_SKEW_SECONDS:
        raise CrawlerStatusError("crawler_status_observed_at_future")
    scan_observed_at = _optional_timestamp(value.get("scan_observed_at_utc"))
    scan_observed_at_value = (
        None
        if scan_observed_at is None
        else _timestamp_value(scan_observed_at)[1]
    )
    if (
        scan_observed_at_value is not None
        and (scan_observed_at_value - observed_at_value).total_seconds()
        > MAX_FUTURE_SKEW_SECONDS
    ):
        raise CrawlerStatusError("crawler_status_scan_observed_at_future")
    started_at = _optional_timestamp(value.get("started_at_utc"))
    started_at_value = (
        None if started_at is None else _timestamp_value(started_at)[1]
    )
    if (
        started_at_value is not None
        and (started_at_value - observed_at_value).total_seconds() > MAX_FUTURE_SKEW_SECONDS
    ):
        raise CrawlerStatusError("crawler_status_started_at_future")
    elapsed_seconds = _optional_int(value.get("elapsed_seconds"), 86_400)
    if (started_at is None) != (elapsed_seconds is None):
        raise CrawlerStatusError("crawler_status_timing_invalid")
    if (
        started_at_value is not None
        and elapsed_seconds is not None
        and abs((observed_at_value - started_at_value).total_seconds() - elapsed_seconds)
        > 5
    ):
        raise CrawlerStatusError("crawler_status_timing_invalid")
    state = str(value["state"])
    error_code = value.get("error_code")
    if error_code is not None and error_code not in _ERROR_CODES:
        raise CrawlerStatusError("crawler_status_error_code_invalid")
    failure_phase = value.get("failure_phase")
    if failure_phase is not None and failure_phase not in {"scan", "publish"}:
        raise CrawlerStatusError("crawler_status_failure_phase_invalid")
    if failure_phase is not None and state != "failed_waiting_next_cycle":
        raise CrawlerStatusError("crawler_status_failure_phase_invalid")
    revision = None if value.get("revision") is None else _revision(value["revision"])
    release = None if value.get("release") is None else _release(value["release"])
    server_count = _optional_int(value.get("server_count"), 100)
    request_count = _optional_int(value.get("request_count"), 100)
    transport_attempt_count = _optional_int(
        value.get("transport_attempt_count"), 300
    )
    if transport_attempt_count is None:
        transport_attempt_count = request_count
    summary_rows = _optional_int(value.get("summary_rows"), 10_000_000)
    snapshot_changed = _optional_bool(value.get("snapshot_changed"))
    packet_bus_segments = _optional_int(value.get("packet_bus_segments"), 128)
    readback_verified = _optional_bool(value.get("readback_verified"))
    api_release_sha256 = value.get("api_release_sha256")
    if api_release_sha256 is not None and (
        not isinstance(api_release_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", api_release_sha256) is None
    ):
        raise CrawlerStatusError("crawler_status_api_release_invalid")
    last_success = _last_success(value.get("last_success"))
    run_id = value.get("run_id")
    if run_id is not None and (
        not isinstance(run_id, str)
        or re.fullmatch(r"market-[0-9a-f]{32}", run_id) is None
    ):
        raise CrawlerStatusError("crawler_status_run_id_invalid")

    next_run_at = _optional_timestamp(value.get("next_run_utc"))
    next_run_value = (
        None if next_run_at is None else _timestamp_value(next_run_at)[1]
    )
    if (
        next_run_value is not None
        and (
            (observed_at_value - next_run_value).total_seconds() > MAX_FUTURE_SKEW_SECONDS
            or (next_run_value - observed_at_value).total_seconds()
            > MAX_NEXT_RUN_AHEAD_SECONDS
        )
    ):
        raise CrawlerStatusError("crawler_status_next_run_invalid")

    if state == "published" and (
        revision is None
        or release is None
        or server_count != 100
        or request_count != 100
        or transport_attempt_count is None
        or transport_attempt_count < request_count
        or summary_rows is None
        or summary_rows <= 0
        or snapshot_changed is None
        or packet_bus_segments is None
        or packet_bus_segments <= 0
        or last_success is None
        or last_success["economyRevision"] != revision
    ):
        raise CrawlerStatusError("crawler_status_published_evidence_invalid")
    if (
        value["schema_version"] == "aion2.market-autorefresh.status/v2"
        and state == "published"
        and (
            readback_verified is not True
            or api_release_sha256 is None
            or run_id is None
            or scan_observed_at is None
            or last_success is None
            or last_success["runId"] != run_id
            or last_success["observedAt"] != scan_observed_at
        )
    ):
        raise CrawlerStatusError("crawler_status_readback_evidence_invalid")

    reported_revision = revision or (
        None if last_success is None else str(last_success["economyRevision"])
    )
    revision_pending = (
        reported_revision is not None and reported_revision != current_revision
    )
    cadence_due = next_run_value or (
        observed_at_value + timedelta(seconds=HOURLY_CADENCE_SECONDS)
    )
    stale = server_now > cadence_due + timedelta(seconds=STATUS_LATE_GRACE_SECONDS)

    state_mapping = {
        "disabled": ("paused", "idle"),
        "stopped": ("paused", "idle"),
        "preflight": ("starting", "session"),
        "scanning": ("running", "scan"),
        "publishing": ("running", "publish"),
        "verifying": ("running", "publish"),
        "published": ("completed", "complete"),
        "failed_waiting_next_cycle": ("failed", "scan"),
        "manual_refresh_ignored_disabled": ("paused", "idle"),
        "manual_refresh_ignored_cooldown": ("idle", "idle"),
        "supervisor_restarting": ("retrying", "session"),
        "supervisor_fault": ("failed", "idle"),
    }
    public_status, public_phase = state_mapping[state]
    if state == "failed_waiting_next_cycle" and failure_phase is not None:
        public_phase = failure_phase
    if revision_pending:
        public_status, public_phase = "failed", "publish"
    elif stale:
        public_status, public_phase = "failed", "idle"

    processed = server_count if server_count is not None else (request_count or 0)
    requests = request_count or 0
    transport_attempts = transport_attempt_count or requests
    errors: list[dict[str, Any]] = []
    if error_code is not None:
        errors.append(
            {
                "errorId": f"standalone-{error_code}",
                "at": observed_at,
                "code": error_code,
                "errorClass": "internal",
                "message": error_code,
                "retryable": True,
            }
        )
    if revision_pending:
        errors.append(
            {
                "errorId": "standalone-crawler_status_revision_pending",
                "at": observed_at,
                "code": "crawler_status_revision_pending",
                "errorClass": "storage",
                "message": "crawler_status_revision_pending",
                "retryable": True,
            }
        )
    if stale:
        errors.append(
            {
                "errorId": "standalone-crawler_status_stale",
                "at": observed_at,
                "code": "crawler_status_stale",
                "errorClass": "upstream",
                "message": "crawler_status_stale",
                "retryable": True,
            }
        )
    return {
        "configured": True,
        "source": "standalone_object",
        "controlAvailable": False,
        "economyRevision": current_revision,
        "release": release,
        "nextRunAt": next_run_at,
        "minimumNextAttemptAt": _optional_timestamp(value.get("minimum_next_attempt_utc")),
        "lastSuccess": last_success,
        "workUnit": "server_scopes",
        "runId": run_id,
        "status": public_status,
        "phase": public_phase,
        "revision": 0,
        "updatedAt": observed_at,
        "scanObservedAt": scan_observed_at,
        "startedAt": started_at,
        "elapsedSeconds": elapsed_seconds or 0,
        "totalItems": 100,
        "processedItems": processed,
        "successfulRequests": requests,
        "transportAttempts": transport_attempts,
        "failedRequests": 1 if error_code is not None else 0,
        "retriedRequests": max(0, transport_attempts - requests),
        "queueDepth": 0 if public_status == "completed" else max(0, 100 - processed),
        "checkpoint": None,
        "errors": errors,
    }
