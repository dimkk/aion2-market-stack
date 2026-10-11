from __future__ import annotations

import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch

from economy_api.models import CrawlerStatus, FarmSession
from economy_api.ydb_repository import YdbConnection, YdbRepository, _timestamp


class _Result:
    rows = [{"value": 1}]


class _Transaction:
    def __init__(self, prepared: object, params: dict[str, object]) -> None:
        self.prepared = prepared
        self.params = params
        self.called = False

    def execute(
        self,
        query: object,
        parameters: dict[str, object] | None = None,
        commit_tx: bool = False,
    ) -> list[_Result]:
        self.called = True
        if query is not self.prepared:
            raise AssertionError("raw_query_was_not_prepared")
        if parameters != self.params:
            raise AssertionError("parameters_were_not_forwarded")
        if commit_tx is not True:
            raise AssertionError("transaction_was_not_committed")
        return [_Result()]


class _Session:
    def __init__(self, source: str, params: dict[str, object]) -> None:
        self.source = source
        self.prepared = object()
        self.tx = _Transaction(self.prepared, params)

    def prepare(self, query: str) -> object:
        if query != self.source:
            raise AssertionError("unexpected_query")
        return self.prepared

    def transaction(self) -> _Transaction:
        return self.tx


class _Pool:
    def __init__(self, session: _Session) -> None:
        self.session = session

    def retry_operation_sync(self, operation: object) -> list[object]:
        return operation(self.session)  # type: ignore[operator]


class _RateTransaction:
    def __init__(self, prepared: list[object]) -> None:
        self.prepared = prepared
        self.calls: list[tuple[object, dict[str, object], bool]] = []

    def execute(
        self,
        query: object,
        parameters: dict[str, object],
        commit_tx: bool = False,
    ) -> list[_Result]:
        if query not in self.prepared:
            raise AssertionError("rate_limit_raw_query_was_not_prepared")
        self.calls.append((query, parameters, commit_tx))
        return [_ResultWithRows([])] if len(self.calls) == 1 else []


class _ResultWithRows:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows


class _RateSession:
    def __init__(self) -> None:
        self.prepared: list[object] = []
        self.tx = _RateTransaction(self.prepared)

    def prepare(self, _query: str) -> object:
        prepared = object()
        self.prepared.append(prepared)
        return prepared

    def transaction(self, _policy: object) -> _RateTransaction:
        return self.tx


class _AtomicFarmTransaction:
    def __init__(self, prepared: list[object], sources: list[str]) -> None:
        self.prepared = prepared
        self.sources = sources
        self.calls: list[tuple[object, dict[str, object], bool]] = []

    def execute(
        self,
        query: object,
        parameters: dict[str, object],
        commit_tx: bool = False,
    ) -> list[_ResultWithRows]:
        if query not in self.prepared:
            raise AssertionError("farm_raw_query_was_not_prepared")
        self.calls.append((query, parameters, commit_tx))
        source = self.sources[self.prepared.index(query)]
        if "COUNT(*) AS session_count" in source:
            return [_ResultWithRows([{"session_count": 0}])]
        if len(self.calls) in {1, 2}:
            return [_ResultWithRows([])]
        return []


class _AtomicFarmSession:
    def __init__(self) -> None:
        self.prepared: list[object] = []
        self.sources: list[str] = []
        self.tx = _AtomicFarmTransaction(self.prepared, self.sources)
        self.transaction_count = 0

    def prepare(self, query: str) -> object:
        prepared = object()
        self.prepared.append(prepared)
        self.sources.append(query)
        return prepared

    def transaction(self, _policy: object) -> _AtomicFarmTransaction:
        self.transaction_count += 1
        return self.tx


class _CrawlerCleanupTransaction:
    def __init__(self, prepared: list[object], run_id: str, status_run_id: str) -> None:
        self.prepared = prepared
        self.run_id = run_id
        self.status_run_id = status_run_id
        self.calls: list[tuple[object, dict[str, object], bool]] = []
        self.commits = 0

    def execute(
        self,
        query: object,
        parameters: dict[str, object],
        commit_tx: bool = False,
    ) -> list[_ResultWithRows]:
        if query not in self.prepared:
            raise AssertionError("crawler_cleanup_raw_query_was_not_prepared")
        self.calls.append((query, parameters, commit_tx))
        if len(self.calls) == 1:
            return [_ResultWithRows([{"run_id": self.run_id}])]
        if len(self.calls) == 2:
            return [_ResultWithRows([{
                "payload_json": '{"runId":"' + self.status_run_id + '"}'
            }])]
        return []

    def commit(self) -> None:
        self.commits += 1


class _CrawlerCleanupSession:
    def __init__(self, run_id: str, status_run_id: str) -> None:
        self.prepared: list[object] = []
        self.sources: list[str] = []
        self.tx = _CrawlerCleanupTransaction(
            self.prepared, run_id, status_run_id
        )

    def prepare(self, query: str) -> object:
        prepared = object()
        self.prepared.append(prepared)
        self.sources.append(query)
        return prepared

    def transaction(self, _policy: object) -> _CrawlerCleanupTransaction:
        return self.tx


class _CrawlerStatusTransaction:
    def __init__(self, prepared: list[object], sources: list[str]) -> None:
        self.prepared = prepared
        self.sources = sources
        self.calls: list[tuple[object, dict[str, object], bool]] = []

    def execute(
        self,
        query: object,
        parameters: dict[str, object],
        commit_tx: bool = False,
    ) -> list[_ResultWithRows]:
        if query not in self.prepared:
            raise AssertionError("crawler_status_raw_query_was_not_prepared")
        self.calls.append((query, parameters, commit_tx))
        source = self.sources[self.prepared.index(query)]
        if "FROM crawler_leases" in source:
            return [_ResultWithRows([{
                "run_id": "run-1",
                "lease_digest": "digest-1",
            }])]
        if "SELECT payload_json FROM crawler_status" in source:
            return [_ResultWithRows([{"payload_json": '{"revision":1}'}])]
        return []

    def commit(self) -> None:
        raise AssertionError("valid_status_publish_must_commit_with_the_write")


class _CrawlerStatusSession:
    def __init__(self) -> None:
        self.prepared: list[object] = []
        self.sources: list[str] = []
        self.tx = _CrawlerStatusTransaction(self.prepared, self.sources)

    def prepare(self, query: str) -> object:
        prepared = object()
        self.prepared.append(prepared)
        self.sources.append(query)
        return prepared

    def transaction(self, _policy: object) -> _CrawlerStatusTransaction:
        return self.tx


class YdbConnectionTests(unittest.TestCase):
    def test_admin_farm_pagination_uses_both_owner_and_session_cursor(self) -> None:
        calls: list[tuple[str, dict[str, object]]] = []

        def execute(query: str, params: dict[str, object]) -> list[dict[str, object]]:
            calls.append((query, params))
            return []

        repository = YdbRepository(SimpleNamespace(execute=execute))  # type: ignore[arg-type]
        rows, next_cursor = repository.list_farm_sessions(
            None, 25, "alice:session-0042"
        )

        self.assertEqual(rows, [])
        self.assertIsNone(next_cursor)
        self.assertEqual(calls[0][1]["$cursor_owner"], "alice")
        self.assertEqual(calls[0][1]["$cursor_session"], "session-0042")
        self.assertIn("owner_username < $cursor_owner", calls[0][0])
        self.assertIn("ORDER BY owner_username DESC, session_id DESC", calls[0][0])

    def test_counts_farm_sessions_with_bounded_owner_query(self) -> None:
        calls: list[tuple[str, dict[str, object]]] = []

        def execute(query: str, params: dict[str, object]) -> list[dict[str, object]]:
            calls.append((query, params))
            return [{"session_count": 37}]

        connection = SimpleNamespace(execute=execute)
        repository = YdbRepository(connection)  # type: ignore[arg-type]
        self.assertEqual(repository.count_farm_sessions("approved"), 37)
        self.assertEqual(calls[0][1], {"$owner": "approved"})
        self.assertIn("COUNT(*)", calls[0][0])
        self.assertIn("owner_username = $owner", calls[0][0])

    def test_timestamp_accepts_ydb_microseconds_and_datetime(self) -> None:
        expected = datetime(2026, 10, 5, 6, 30, tzinfo=UTC)
        micros = int(expected.timestamp() * 1_000_000)

        self.assertEqual(_timestamp(micros), expected)
        self.assertEqual(_timestamp(expected.replace(tzinfo=None)), expected)
        self.assertEqual(_timestamp(expected), expected)

    def test_execute_prepares_text_query_before_passing_parameters(self) -> None:
        query = "DECLARE $value AS Uint64; SELECT $value AS value;"
        params: dict[str, object] = {"$value": 1}
        session = _Session(query, params)
        connection = object.__new__(YdbConnection)
        connection.pool = _Pool(session)  # type: ignore[attr-defined]

        rows = connection.execute(query, params)

        self.assertEqual(rows, [{"value": 1}])
        self.assertTrue(session.tx.called)

    def test_rate_limit_prepares_both_transaction_queries(self) -> None:
        session = _RateSession()
        connection = object.__new__(YdbConnection)
        connection.pool = _Pool(session)  # type: ignore[attr-defined]
        repository = YdbRepository(connection)

        with patch(
            "economy_api.ydb_repository.ydb",
            SimpleNamespace(SerializableReadWrite=lambda: object()),
        ):
            allowed = repository.consume_rate_limit(
                "scope:subject",
                5,
                60,
                datetime(2026, 10, 5, 6, 30, tzinfo=UTC),
            )

        self.assertTrue(allowed)
        self.assertEqual(len(session.prepared), 2)
        self.assertEqual([call[2] for call in session.tx.calls], [False, True])

    def test_farm_idempotency_mapping_and_row_commit_atomically(self) -> None:
        session = _AtomicFarmSession()
        connection = object.__new__(YdbConnection)
        connection.pool = _Pool(session)  # type: ignore[attr-defined]
        repository = YdbRepository(connection)
        row = FarmSession(
            owner_username="approved",
            session_id="deterministic-id",
            client_session_id="c" * 64,
            payload={"payload_id": "c" * 64},
            created_at=datetime(2026, 10, 5, 6, 30, tzinfo=UTC),
            updated_at=datetime(2026, 10, 5, 6, 31, tzinfo=UTC),
        )

        with patch(
            "economy_api.ydb_repository.ydb",
            SimpleNamespace(SerializableReadWrite=lambda: object()),
        ):
            stored = repository.upsert_farm_session(row, 1000)

        self.assertEqual(stored.session_id, "deterministic-id")
        self.assertEqual(session.transaction_count, 1)
        self.assertEqual(len(session.prepared), 4)
        self.assertEqual([call[2] for call in session.tx.calls], [False, False, False, True])
        final_query = session.sources[-1]
        self.assertIn("UPSERT INTO farm_sessions", final_query)
        self.assertIn("UPSERT INTO farm_session_ids", final_query)
        self.assertIn("client_session_id, session_id, updated_at", final_query)

    def test_crawler_connect_fences_lease_status_and_claim_in_one_transaction(self) -> None:
        session = _AtomicFarmSession()
        connection = object.__new__(YdbConnection)
        connection.pool = _Pool(session)  # type: ignore[attr-defined]
        repository = YdbRepository(connection)
        now = datetime(2026, 10, 5, 6, 30, tzinfo=UTC)
        initial = CrawlerStatus(
            run_id=None,
            status="starting",
            phase="session",
            revision=0,
            updated_at=now,
            started_at=now,
            elapsed_seconds=0,
            total_items=100,
            processed_items=0,
            successful_requests=0,
            failed_requests=0,
            retried_requests=0,
            queue_depth=0,
            checkpoint=None,
            errors=(),
        )
        with patch(
            "economy_api.ydb_repository.ydb",
            SimpleNamespace(SerializableReadWrite=lambda: object()),
        ):
            connected = repository.connect_crawler(
                "primary", "run-1", "digest-1", initial, now
            )

        self.assertEqual(connected.revision, 1)
        self.assertEqual(connected.run_id, "run-1")
        self.assertEqual(session.transaction_count, 1)
        self.assertEqual([call[2] for call in session.tx.calls], [False, False, True])
        final_query = session.sources[-1]
        self.assertIn("UPSERT INTO crawler_status", final_query)
        self.assertIn("UPSERT INTO crawler_leases", final_query)
        self.assertIn("UPDATE crawler_actions", final_query)

    def test_crawler_status_cas_publishes_updated_status_in_one_transaction(self) -> None:
        session = _CrawlerStatusSession()
        connection = object.__new__(YdbConnection)
        connection.pool = _Pool(session)  # type: ignore[attr-defined]
        repository = YdbRepository(connection)
        started_at = datetime(2026, 10, 5, 6, 30, tzinfo=UTC)
        heartbeat_at = datetime(2026, 10, 5, 6, 31, tzinfo=UTC)
        status = CrawlerStatus(
            run_id="run-1",
            status="running",
            phase="catalog",
            revision=2,
            updated_at=started_at,
            started_at=started_at,
            elapsed_seconds=60,
            total_items=100,
            processed_items=25,
            successful_requests=25,
            failed_requests=0,
            retried_requests=0,
            queue_depth=75,
            checkpoint={"item_id": 42},
            errors=(),
        )

        with patch(
            "economy_api.ydb_repository.ydb",
            SimpleNamespace(SerializableReadWrite=lambda: object()),
        ):
            published = repository.publish_crawler_status_cas(
                "primary", "run-1", "digest-1", 1, status, heartbeat_at
            )

        self.assertIsNotNone(published)
        assert published is not None
        self.assertEqual(published.updated_at, heartbeat_at)
        self.assertEqual(published.revision, 2)
        self.assertEqual([call[2] for call in session.tx.calls], [False, False, True])
        self.assertEqual(session.tx.calls[-1][1]["$revision"], 2)
        self.assertIn(
            '"updatedAt":"2026-10-05T06:31:00Z"',
            str(session.tx.calls[-1][1]["$payload"]),
        )

    def test_crawler_smoke_cleanup_deletes_only_matching_run_atomically(self) -> None:
        session = _CrawlerCleanupSession("smoke-run", "smoke-run")
        connection = object.__new__(YdbConnection)
        connection.pool = _Pool(session)  # type: ignore[attr-defined]
        repository = YdbRepository(connection)

        with patch(
            "economy_api.ydb_repository.ydb",
            SimpleNamespace(SerializableReadWrite=lambda: object()),
        ):
            deleted = repository.delete_crawler_state_if_owned(
                "primary", "smoke-run"
            )

        self.assertTrue(deleted)
        self.assertEqual([False, False, True], [row[2] for row in session.tx.calls])
        self.assertIn("run_id = $run_id", session.sources[-1])

    def test_crawler_smoke_cleanup_preserves_reconnected_run(self) -> None:
        session = _CrawlerCleanupSession("new-run", "new-run")
        connection = object.__new__(YdbConnection)
        connection.pool = _Pool(session)  # type: ignore[attr-defined]
        repository = YdbRepository(connection)

        with patch(
            "economy_api.ydb_repository.ydb",
            SimpleNamespace(SerializableReadWrite=lambda: object()),
        ):
            deleted = repository.delete_crawler_state_if_owned(
                "primary", "smoke-run"
            )

        self.assertFalse(deleted)
        self.assertEqual(2, len(session.tx.calls))
        self.assertEqual(1, session.tx.commits)


if __name__ == "__main__":
    unittest.main()
