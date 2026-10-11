import importlib.util
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location(
    "market_history", ROOT / "scripts" / "market_history.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def safe_document():
    listing = {
        "sale_id": "FE46D413-932D-4421-A914-AEF941252784",
        "game_item_id": 516150001,
        "quantity": 1,
        "sale_price": 594,
        "unit_price": "594.0000",
        "effective_from": "2026-10-04T03:45:10.611+03:00",
        "effective_to": "2026-10-07T03:45:10.611+03:00",
        "category_id": 48,
        "grade_id": 2,
        "is_set_item": False,
        "purchase_currency_item_id": 247,
        "purchase_currency_type": 2,
        "conditions": [{"key": "ItemLevel", "type": "1", "value": "0"}],
    }
    return {"report": {
        "authority": module.AUTHORITY,
        "method": "POST",
        "path": module.PATH,
        "request": {"game_server_id": 2301, "game_item_ids": [516150001],
                    "page": 2, "page_size": 100},
        "response": {"status_code": 200, "has_next_page": False,
                     "listing_count": 1, "listings": [listing]},
        "auth": {"persisted": False, "exported": False},
        "privacy": {"fresh_https_request": True, "credentials_exported": False,
                    "raw_headers_exported": False, "raw_bodies_exported": False},
    }}


def client_catalog():
    items = [
        {"id": 516150001, "display_name": "Kina Box", "internal_name": "kina_box",
         "item_type": "Usable", "global_item_level": 0},
        {"id": 110430006, "display_name": "Fantasy Bow", "internal_name": "fantasy_bow",
         "item_type": "Equip", "global_item_level": 41},
        {"id": 999000001, "display_name": "Empty Test", "internal_name": "empty_test",
         "item_type": "Misc", "global_item_level": 0},
    ]
    return {"format": module.CATALOG_FORMAT, "table_version": 13,
            "declared_item_count": len(items), "recovered_item_count": len(items),
            "localized_item_count": len(items), "items": items}


def safe_summary_document():
    rows = [
        {"game_item_id": 516150001, "min_unit_price": "164.0000",
         "sale_count": 8, "conditions": []},
        {"game_item_id": 110430006, "min_unit_price": "540000.0000",
         "sale_count": 3,
         "conditions": [{"key": "Enchant", "type": "1", "value": "0"}]},
        {"game_item_id": 999000001, "min_unit_price": "0",
         "sale_count": 0, "conditions": []},
    ]
    return {"report": {
        "format": module.SUMMARY_FORMAT,
        "authority": module.AUTHORITY,
        "method": "POST",
        "path": module.SUMMARY_PATH,
        "request": {"game_server_id": 2301},
        "response": {"status_code": 200, "summary_count": len(rows),
                     "summaries": rows},
        "auth": {"kind": "client_session_jwt_memory_only",
                 "client_must_remain_open": True,
                 "persisted": False, "exported": False},
        "privacy": {"fresh_https_request": True, "max_requests": 1,
                    "credentials_exported": False,
                    "raw_headers_exported": False,
                    "raw_bodies_exported": False,
                    "paths_exposed": False},
    }}


def safe_multi_summary_document():
    first = safe_summary_document()["report"]
    responses = []
    for server_id in (1301, 2301):
        responses.append({
            "game_server_id": server_id,
            **first["response"],
        })
    return {"report": {
        "format": module.MULTI_SUMMARY_FORMAT,
        "authority": module.AUTHORITY,
        "method": "POST",
        "path": module.SUMMARY_PATH,
        "request": {"game_server_ids": [1301, 2301],
                    "minimum_interval_seconds": 1.0},
        "responses": responses,
        "transport_attempts": 3,
        "target_batch_sha256": "a" * 64,
        "registry_sha256": "b" * 64,
        "catalog_sha256": "a" * 64,
        "auth": first["auth"],
        "privacy": {**first["privacy"], "max_requests": 6},
    }}


def safe_multi_scan_document():
    children = []
    for server_id in (1301, 2301):
        child = safe_document()["report"]
        child["format"] = "aion2_market_session_scan_v2"
        child["request"] = {
            "game_server_id": server_id,
            "game_item_ids": [516150001],
            "page": 1,
            "page_size": 100,
            "minimum_interval_seconds": 1.0,
            "items_per_request": 1,
            "sort": {"key": "SalePrice", "order_by": "ASC"},
            "targets": [{"item_id": 516150001, "start_page": 1}],
        }
        child["response"].update({
            "complete": True,
            "request_count": 1,
            "page_count": 1,
            "has_next_page": False,
            "pages": [{
                "item_ids": [516150001],
                "page": 1,
                "listing_count": 1,
                "has_next_page": False,
            }],
        })
        child["auth"].update({
            "kind": "client_session_jwt_memory_only",
            "client_must_remain_open": True,
        })
        child["privacy"].update({"max_requests": 30, "paths_exposed": False})
        children.append(child)
    return {"report": {
        "format": "aion2_market_multi_session_scan_v1",
        "authority": module.AUTHORITY,
        "method": "POST",
        "path": module.PATH,
        "request": {
            "game_server_ids": [1301, 2301],
            "target_count": 2,
            "minimum_interval_seconds": 1.0,
            "items_per_request": 1,
        },
        "responses": children,
        "request_count": 2,
        "listing_count": 2,
        "target_batch_sha256": "a" * 64,
        "source_catalog_sha256": "b" * 64,
        "registry_sha256": "c" * 64,
        "auth": children[0]["auth"],
        "privacy": {
            "fresh_https_request": True,
            "max_requests": 3000,
            "credentials_exported": False,
            "raw_headers_exported": False,
            "raw_bodies_exported": False,
            "paths_exposed": False,
        },
    }}


class MarketHistoryTests(unittest.TestCase):
    def test_distinct_multi_scan_parents_preserve_identical_child_observations(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "market.sqlite"
            first = root / "first.json"
            second = root / "second.json"
            first.write_text(json.dumps(safe_multi_scan_document()), encoding="utf-8")
            second_document = safe_multi_scan_document()
            second_document["report"]["target_batch_sha256"] = "d" * 64
            second.write_text(json.dumps(second_document), encoding="utf-8")
            os.utime(first, (1_700_000_000, 1_700_000_000))
            os.utime(second, (1_700_000_060, 1_700_000_060))

            first_receipt = module.ingest_multi_scan(first, database)
            second_receipt = module.ingest_multi_scan(second, database)
            replay_receipt = module.ingest_multi_scan(second, database)

            self.assertEqual(2, first_receipt["inserted_servers"])
            self.assertEqual(2, second_receipt["inserted_servers"])
            self.assertEqual(0, replay_receipt["inserted_servers"])
            self.assertEqual(2, replay_receipt["already_ingested_servers"])
            connection = sqlite3.connect(database)
            try:
                rows = connection.execute(
                    "SELECT observed_at,count(*) FROM scans GROUP BY observed_at ORDER BY observed_at"
                ).fetchall()
            finally:
                connection.close()
            self.assertEqual(2, len(rows))
            self.assertEqual([2, 2], [row[1] for row in rows])

    def test_multi_summary_is_atomic_and_server_scoped(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog, database = root / "catalog.json", root / "market.sqlite"
            catalog.write_text(json.dumps(client_catalog()), encoding="utf-8")
            module.import_catalog(catalog, database, 25624879, 25650019)
            report = root / "multi.json"
            report.write_text(json.dumps(safe_multi_summary_document()), encoding="utf-8")
            receipt = module.ingest_multi_summary(
                report, database, observed_at="2026-10-09T12:00:00Z"
            )
            self.assertEqual(2, receipt["inserted_servers"])
            self.assertEqual(6, receipt["summaries"])
            self.assertEqual(3, receipt["transport_attempts"])
            connection = sqlite3.connect(database)
            try:
                rows = connection.execute(
                    "SELECT game_server_id,summary_count FROM summary_runs ORDER BY game_server_id"
                ).fetchall()
            finally:
                connection.close()
            self.assertEqual([(1301, 3), (2301, 3)], rows)

    def test_multi_summary_expected_digest_is_checked_before_database_write(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = root / "multi.json"
            database = root / "market.sqlite"
            raw = json.dumps(safe_multi_summary_document()).encode("utf-8")
            report.write_bytes(raw)

            with self.assertRaisesRegex(
                ValueError, "market_summary_source_sha256_mismatch"
            ):
                module.ingest_multi_summary(
                    report,
                    database,
                    observed_at="2026-10-09T12:00:00Z",
                    expected_sha256="0" * 64,
                )

            self.assertFalse(database.exists())
            receipt = module.ingest_multi_summary(
                report,
                database,
                observed_at="2026-10-09T12:00:00Z",
                expected_sha256=hashlib.sha256(raw).hexdigest(),
            )
            self.assertEqual(2, receipt["inserted_servers"])

            os.utime(report, (1_900_000_000, 1_900_000_000))
            replay = module.ingest_multi_summary(
                report,
                database,
                observed_at="2026-10-09T12:00:00Z",
                expected_sha256=hashlib.sha256(raw).hexdigest(),
            )
            self.assertEqual(2, replay["already_ingested_servers"])
            connection = sqlite3.connect(database)
            try:
                observed = {
                    row[0]
                    for row in connection.execute(
                        "SELECT DISTINCT observed_at FROM summary_runs"
                    )
                }
            finally:
                connection.close()
            self.assertEqual({"2026-10-09T12:00:00Z"}, observed)

    def test_generations_and_queue_state_are_isolated_per_server(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog, database = root / "catalog.json", root / "market.sqlite"
            catalog.write_text(json.dumps(client_catalog()), encoding="utf-8")
            module.import_catalog(catalog, database, 25624879, 25650019)

            def write_report(name, server_id, price, sale_id):
                document = safe_document()
                document["report"]["request"].update({
                    "game_server_id": server_id, "page": 1,
                    "targets": [{"item_id": 516150001, "start_page": 1}],
                })
                document["report"]["response"].update({
                    "complete": True, "request_count": 1, "page_count": 1,
                    "pages": [{"item_ids": [516150001], "page": 1,
                               "listing_count": 1, "has_next_page": False}],
                })
                listing = document["report"]["response"]["listings"][0]
                listing.update({"sale_id": sale_id, "sale_price": price,
                                "unit_price": f"{price}.0000"})
                path = root / name
                path.write_text(json.dumps(document), encoding="utf-8")
                module.ingest(path, database)

            write_report("server-a-1.json", 2301, 100, "sale-a-1")
            write_report("server-b-1.json", 2302, 900, "sale-b-1")
            write_report("server-a-2.json", 2301, 200, "sale-a-2")

            connection = sqlite3.connect(database)
            try:
                states = connection.execute(
                    """SELECT game_server_id,current_generation,last_listing_count
                       FROM item_server_state WHERE item_id=516150001
                       ORDER BY game_server_id"""
                ).fetchall()
                current = connection.execute(
                    """SELECT s.game_server_id,l.unit_price FROM listings l
                       JOIN scans s USING(scan_id) JOIN item_server_state st
                         ON st.game_server_id=s.game_server_id
                        AND st.item_id=l.item_id
                        AND st.current_generation=l.generation
                       WHERE l.item_id=516150001 ORDER BY s.game_server_id"""
                ).fetchall()
            finally:
                connection.close()
            self.assertEqual([(2301, 2, 1), (2302, 1, 1)], states)
            self.assertEqual([(2301, "200.0000"), (2302, "900.0000")], current)

            batch_a, batch_b = root / "a.json", root / "b.json"
            module.plan_batch(database, batch_a, limit=10, game_server_id=2301)
            module.plan_batch(database, batch_b, limit=10, game_server_id=2302)
            self.assertEqual(2301, json.loads(batch_a.read_text())["game_server_id"])
            self.assertEqual(2302, json.loads(batch_b.read_text())["game_server_id"])
            self.assertNotIn(516150001, [
                row["item_id"] for row in json.loads(batch_a.read_text())["items"]
            ])

    def test_ingest_is_idempotent_and_render_is_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report, database, output = root / "report.json", root / "market.sqlite", root / "index.html"
            report.write_text(json.dumps(safe_document()), encoding="utf-8")
            first = module.ingest(report, database)
            second = module.ingest(report, database)
            rendered = module.render(database, output)
            self.assertEqual("ingested", first["status"])
            self.assertEqual("already_ingested", second["status"])
            self.assertEqual(1, rendered["current_listings"])
            connection = sqlite3.connect(database)
            try:
                self.assertEqual(1, connection.execute("SELECT count(*) FROM scans").fetchone()[0])
                self.assertEqual(1, connection.execute("SELECT count(*) FROM listings").fetchone()[0])
            finally:
                connection.close()
            page = output.read_text(encoding="utf-8")
            self.assertIn("Kina Chest: 400,000 Kina", page)
            self.assertIn("Скопировать для LLM", page)
            self.assertIn("location.href", page)
            self.assertNotIn('"seller":', page.lower())

    def test_rejects_non_fresh_or_credential_exporting_report(self):
        for field, value in (("fresh_https_request", False), ("credentials_exported", True)):
            document = safe_document()
            document["report"]["privacy"][field] = value
            with self.assertRaisesRegex(ValueError, "market_history_report_invalid"):
                module._report(document)

    def test_full_catalog_planning_empty_results_and_search_ui(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog, database = root / "catalog.json", root / "market.sqlite"
            batch, report, output = root / "batch.json", root / "empty.json", root / "index.html"
            catalog.write_text(json.dumps(client_catalog()), encoding="utf-8")
            imported = module.import_catalog(catalog, database, 25624879, 25650019)
            planned = module.plan_batch(database, batch, limit=3)
            self.assertEqual(3, imported["items"])
            self.assertEqual("batch_planned", planned["status"])
            batch_doc = json.loads(batch.read_text(encoding="utf-8"))
            self.assertEqual(3, len(batch_doc["items"]))

            document = safe_document()
            document["report"]["request"].update({
                "game_item_ids": [999000001],
                "targets": [{"item_id": 999000001, "start_page": 1}],
            })
            document["report"]["response"].update({
                "complete": True, "request_count": 1, "page_count": 1,
                "listing_count": 0, "listings": [],
                "pages": [{"item_ids": [999000001], "page": 1,
                           "listing_count": 0, "has_next_page": False}],
            })
            report.write_text(json.dumps(document), encoding="utf-8")
            ingested = module.ingest(report, database)
            rendered = module.render(database, output)
            self.assertEqual(1, ingested["attempted_targets"])
            connection = sqlite3.connect(database)
            try:
                state = connection.execute(
                    "SELECT scan_status FROM items WHERE item_id=999000001"
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual("scanned_empty", state)
            self.assertEqual(3, rendered["catalog_items"])
            page = output.read_text(encoding="utf-8")
            self.assertIn("internal name или item ID", page)
            self.assertIn("AION2 <em>economy</em>", page)
            self.assertIn('data-view="overview"', page)
            self.assertIn('data-view="Equip"', page)
            self.assertIn("price_history", page)
            self.assertIn("function spark", page)
            self.assertIn("coverage-incomplete", page)
            self.assertIn("Скопировать для LLM", page)

    def test_summary_drives_active_item_plan_database_and_ui(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = root / "catalog.json"
            summary = root / "summary.json"
            database = root / "market.sqlite"
            batch = root / "active.json"
            output = root / "index.html"
            catalog.write_text(json.dumps(client_catalog()), encoding="utf-8")
            summary.write_text(json.dumps(safe_summary_document()), encoding="utf-8")
            module.import_catalog(catalog, database, 25624879, 25650019)

            first = module.ingest_summary(summary, database)
            second = module.ingest_summary(summary, database)
            connection = sqlite3.connect(database)
            try:
                connection.execute(
                    "UPDATE items SET scan_status='scanned_listed' WHERE item_id=516150001"
                )
                connection.commit()
            finally:
                connection.close()
            planned = module.plan_batch(
                database, batch, limit=10, mode="summary-active"
            )
            rendered = module.render(database, output)

            self.assertEqual("summary_ingested", first["status"])
            self.assertEqual("already_ingested", second["status"])
            self.assertEqual(2, first["active_items"])
            self.assertEqual("batch_planned", planned["status"])
            batch_document = json.loads(batch.read_text(encoding="utf-8"))
            self.assertEqual(
                [516150001, 110430006],
                [row["item_id"] for row in batch_document["items"]],
            )
            self.assertEqual(2, rendered["summary_items"])
            self.assertEqual(11, rendered["summary_sale_count"])
            self.assertEqual(2, rendered["detail_pages"])

            detail_report = root / "detail.json"
            detail_document = safe_document()
            detail_document["report"]["request"].update({
                "page": 1,
                "game_item_ids": [516150001],
                "targets": [{"item_id": 516150001, "start_page": 1}],
            })
            detail_document["report"]["response"].update({
                "complete": True,
                "request_count": 1,
                "page_count": 1,
                "pages": [{"item_ids": [516150001], "page": 1,
                           "listing_count": 1, "has_next_page": False}],
            })
            detail_report.write_text(json.dumps(detail_document), encoding="utf-8")
            module.ingest(detail_report, database)
            resumed_batch = root / "active-resumed.json"
            resumed = module.plan_batch(
                database, resumed_batch, limit=10, mode="summary-active"
            )
            self.assertEqual("batch_planned", resumed["status"])
            self.assertEqual(
                [110430006],
                [row["item_id"] for row in json.loads(
                    resumed_batch.read_text(encoding="utf-8")
                )["items"]],
            )
            connection = sqlite3.connect(database)
            try:
                self.assertEqual(
                    1, connection.execute("SELECT count(*) FROM summary_runs").fetchone()[0]
                )
                self.assertEqual(
                    3, connection.execute("SELECT count(*) FROM summary_prices").fetchone()[0]
                )
                self.assertEqual(
                    "scanned_listed", connection.execute(
                        "SELECT scan_status FROM items WHERE item_id=516150001"
                    ).fetchone()[0]
                )
            finally:
                connection.close()
            page = output.read_text(encoding="utf-8")
            self.assertIn("Whole-market summary snapshots", page)
            self.assertIn("summary_active", page)
            self.assertIn("summary_items=", page)
            detail = (output.parent / "items" / "516150001.html").read_text(
                encoding="utf-8"
            )
            self.assertIn("Предложений в summary", detail)
            self.assertIn("164", detail)

    def test_summary_rejects_credential_export(self):
        document = safe_summary_document()
        document["report"]["privacy"]["credentials_exported"] = True
        with self.assertRaisesRegex(ValueError, "market_summary_report_invalid"):
            module._summary_report(document)

    def test_summary_active_plan_is_scoped_to_requested_server(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog, database = root / "catalog.json", root / "market.sqlite"
            catalog.write_text(json.dumps(client_catalog()), encoding="utf-8")
            module.import_catalog(catalog, database, 25624879, 25650019)

            for server_id, active_item, name in (
                (2301, 516150001, "summary-2301.json"),
                (12301, 110430006, "summary-12301.json"),
            ):
                document = safe_summary_document()
                document["report"]["request"]["game_server_id"] = server_id
                for row in document["report"]["response"]["summaries"]:
                    row["sale_count"] = 1 if row["game_item_id"] == active_item else 0
                path = root / name
                path.write_text(json.dumps(document), encoding="utf-8")
                module.ingest_summary(path, database)

            batch_2301, batch_12301 = root / "2301.json", root / "12301.json"
            module.plan_batch(
                database, batch_2301, limit=10, mode="summary-active",
                game_server_id=2301,
            )
            module.plan_batch(
                database, batch_12301, limit=10, mode="summary-active",
                game_server_id=12301,
            )
            self.assertEqual(
                [516150001],
                [row["item_id"] for row in json.loads(
                    batch_2301.read_text(encoding="utf-8")
                )["items"]],
            )
            self.assertEqual(
                [110430006],
                [row["item_id"] for row in json.loads(
                    batch_12301.read_text(encoding="utf-8")
                )["items"]],
            )


if __name__ == "__main__":
    unittest.main()
