from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from economy_api.memory_repository import MemoryRepository
from economy_api.models import CrawlerAction, CrawlerStatus, FarmSession


NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


def initial_status() -> CrawlerStatus:
    return CrawlerStatus(
        run_id=None,
        status="running",
        phase="items",
        revision=0,
        updated_at=NOW,
        started_at=NOW,
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


class MemoryRepositoryTests(unittest.TestCase):
    def test_counts_farm_sessions_per_owner(self) -> None:
        repository = MemoryRepository()
        for owner, session_id in (("one", "a"), ("one", "b"), ("two", "c")):
            repository.upsert_farm_session(FarmSession(
                owner_username=owner,
                session_id=session_id,
                client_session_id=f"client-{session_id}",
                payload={},
                created_at=NOW,
                updated_at=NOW,
            ), 1000)
        self.assertEqual(repository.count_farm_sessions("one"), 2)
        self.assertEqual(repository.count_farm_sessions("two"), 1)

    def test_new_crawler_lease_fences_old_worker_and_requeues_claim(self) -> None:
        repository = MemoryRepository()
        first = repository.connect_crawler(
            "primary", "run-1", "digest-1", initial_status(), NOW
        )
        action = CrawlerAction(
            action_id="action-1",
            kind="pause",
            expected_revision=first.revision,
            status="queued",
            requested_by_user_id="admin-1",
            created_at=NOW,
            updated_at=NOW,
        )
        self.assertIsNotNone(repository.enqueue_crawler_action(action))
        claimed = repository.claim_crawler_action(
            "primary", "run-1", "digest-1", first.revision, NOW
        )
        self.assertEqual(claimed.status, "claimed")  # type: ignore[union-attr]

        self.assertIsNone(repository.connect_crawler(
            "primary", "run-2", "digest-2", initial_status(), NOW
        ))
        second = repository.connect_crawler(
            "primary", "run-2", "digest-2", initial_status(), NOW + timedelta(seconds=421)
        )
        assert second is not None
        self.assertEqual(second.revision, first.revision + 1)
        self.assertIsNone(
            repository.complete_crawler_action(
                "primary",
                "run-1",
                "digest-1",
                "action-1",
                first.revision,
                "applied",
                NOW,
            )
        )
        reclaimed = repository.claim_crawler_action(
            "primary", "run-2", "digest-2", second.revision, NOW
        )
        self.assertIsNotNone(reclaimed)
        self.assertEqual(reclaimed.action_id, "action-1")  # type: ignore[union-attr]
        self.assertEqual(reclaimed.expected_revision, second.revision)  # type: ignore[union-attr]

    def test_same_run_cannot_reconnect_without_lease_and_future_legacy_stamp_is_stale(self) -> None:
        repository = MemoryRepository()
        first = repository.connect_crawler(
            "primary", "run-1", "digest-1", initial_status(), NOW
        )
        self.assertIsNotNone(first)
        self.assertIsNone(repository.connect_crawler(
            "primary", "run-1", "different-digest", initial_status(), NOW
        ))

        # A pre-fix client-controlled future timestamp must not pin the lease
        # indefinitely after the server-authoritative implementation deploys.
        repository.crawler_lease_updated_at = NOW + timedelta(days=365)
        recovered = repository.connect_crawler(
            "primary", "run-2", "digest-2", initial_status(), NOW
        )
        self.assertIsNotNone(recovered)

    def test_crawler_heartbeat_uses_server_time_for_status_and_lease(self) -> None:
        repository = MemoryRepository()
        first = repository.connect_crawler(
            "primary", "run-1", "digest-1", initial_status(), NOW
        )
        assert first is not None
        worker_time = NOW + timedelta(days=365)
        status = replace(
            first, revision=first.revision + 1, updated_at=worker_time
        )
        published = repository.publish_crawler_status_cas(
            "primary", "run-1", "digest-1", first.revision, status, NOW
        )
        assert published is not None
        self.assertEqual(published.updated_at, NOW)
        self.assertEqual(repository.crawler_lease_updated_at, NOW)


if __name__ == "__main__":
    unittest.main()
