import importlib.util
import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "build_economy_views", ROOT / "scripts" / "build_economy_views.py"
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class EconomyViewTests(unittest.TestCase):
    def test_accepts_view_larger_than_retired_http_upload_cap(self):
        snapshot = {
            "schemaVersion": "aion2.economy.snapshot/v1",
            "revision": "r-direct-yc-publication",
            "generatedAt": "2026-10-11T00:00:00Z",
            "build": 1,
            "stats": {"activeItems": 1},
            "items": [{
                "itemId": 1, "serverId": 12301, "market": "server",
                "current": 10, "saleCount": 1, "listingCount": 1,
            }],
            "recipes": [], "servers": [], "farmSessions": [],
            "limitations": ["x" * (1024 * 1024)],
        }

        views = MODULE.build_views(snapshot)

        encoded = json.dumps(
            views["market.default.json"], ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        self.assertGreater(len(encoded), 900 * 1024)
        self.assertLessEqual(len(encoded), 8 * 1024 * 1024)

    def test_splits_full_snapshot_and_orders_movers(self):
        item = lambda item_id, change, liquidity, sales: {
            "itemId": item_id,
            "current": 10,
            "change24h": change,
            "liquidityScore": liquidity,
            "saleCount": sales,
            "listingCount": sales,
            "newlyLiquid": item_id == 2,
            "newlyLiquidAt": "2026-10-05T00:00:00Z" if item_id == 2 else None,
        }
        snapshot = {
            "schemaVersion": "aion2.economy.snapshot/v1",
            "revision": "r1",
            "generatedAt": "2026-10-05T00:00:00Z",
            "build": 1,
            "stats": {"activeItems": 3},
            "items": [
                {**item(1, -5, 0.2, 2), "serverId": 11301},
                {**item(2, 10, 0.9, 7), "serverId": 11301},
                {**item(3, None, 0.4, 3), "serverId": 12301},
                {**item(4, None, 0.1, 0), "serverId": 12301, "history": [{"value": 11}]},
            ],
            "recipes": [{"recipeId": 3}],
            "servers": [{"serverId": 4}],
            "farmSessions": [],
            "limitations": [],
        }
        views = MODULE.build_views(snapshot)
        self.assertEqual(set(views), {
            "market.default.json", "items.default.json", "movers.default.json",
            "servers.default.json", "craft.default.json", "farm-value.default.json",
        })
        self.assertEqual(len(views["market.default.json"]["items"]), 3)
        self.assertEqual(len(views["items.default.json"]["items"]), 4)
        self.assertEqual(views["items.default.json"]["itemCount"], 4)
        self.assertEqual(views["movers.default.json"]["up"][0]["itemId"], 2)
        self.assertEqual(views["movers.default.json"]["down"][0]["itemId"], 1)
        self.assertEqual(
            [row["itemId"] for row in views["movers.default.json"]["up"]],
            [2, 1],
        )
        self.assertEqual(
            [row["itemId"] for row in views["movers.default.json"]["down"]],
            [1, 2],
        )
        self.assertEqual(views["movers.default.json"]["new"][0]["itemId"], 2)
        self.assertEqual(views["movers.default.json"]["listings"][0]["itemId"], 2)
        self.assertEqual(views["market.default.json"]["revision"], "r1")

    def test_requires_revision(self):
        with self.assertRaisesRegex(ValueError, "snapshot_revision_missing"):
            MODULE.build_views({"schemaVersion": "aion2.economy.snapshot/v1"})

    def test_rejects_active_count_drift(self):
        snapshot = {
            "schemaVersion": "aion2.economy.snapshot/v1",
            "revision": "r1",
            "generatedAt": "2026-10-05T00:00:00Z",
            "build": 1,
            "stats": {"activeItems": 2},
            "items": [{
                "itemId": 1, "serverId": 12301, "current": 10,
                "saleCount": 1, "listingCount": 1,
            }],
            "recipes": [], "servers": [], "farmSessions": [], "limitations": [],
        }
        with self.assertRaisesRegex(ValueError, "snapshot_active_item_count_mismatch"):
            MODULE.build_views(snapshot)

    def test_segments_market_and_history_independently(self):
        def row(item_id, server_id, sales, padding):
            return {
                "itemId": item_id,
                "serverId": server_id,
                "market": "server",
                "current": 10 if sales else None,
                "saleCount": sales,
                "listingCount": sales,
                "history": [{"value": 10}],
                "padding": padding,
            }

        snapshot = {
            "schemaVersion": "aion2.economy.snapshot/v1",
            "revision": "r-segmented",
            "generatedAt": "2026-10-08T00:00:00Z",
            "build": 1,
            "stats": {"activeItems": 2},
            "items": [
                row(1, 11301, 1, ""),
                row(2, 12301, 1, ""),
                row(3, 11301, 0, "a" * 600),
                row(4, 12301, 0, "b" * 600),
            ],
            "recipes": [], "servers": [], "farmSessions": [], "limitations": [],
        }
        original_limit = MODULE.MAX_VIEW_BYTES
        MODULE.MAX_VIEW_BYTES = 1_400
        try:
            views = MODULE.build_views(snapshot)
        finally:
            MODULE.MAX_VIEW_BYTES = original_limit

        self.assertEqual(views["market.default.json"]["items"], [])
        self.assertEqual(views["market.default.json"]["segments"], ["s113", "s123"])
        self.assertEqual(views["items.default.json"]["items"], [])
        self.assertEqual(views["items.default.json"]["itemCount"], 4)
        self.assertEqual(views["items.default.json"]["segments"], ["s113", "s123"])
        market_rows = views["market.s113.json"]["items"] + views["market.s123.json"]["items"]
        item_rows = views["items.s113.json"]["items"] + views["items.s123.json"]["items"]
        self.assertEqual([entry["itemId"] for entry in market_rows], [1, 2])
        self.assertEqual([entry["itemId"] for entry in item_rows], [1, 3, 2, 4])

    def test_splits_one_server_group_into_size_bounded_parts(self):
        rows = [{
            "itemId": item_id,
            "serverId": 12301,
            "current": 10 if item_id == 1 else None,
            "saleCount": 1 if item_id == 1 else 0,
            "listingCount": 1 if item_id == 1 else 0,
            "padding": "" if item_id == 1 else chr(96 + item_id) * 260,
        } for item_id in range(1, 5)]
        snapshot = {
            "schemaVersion": "aion2.economy.snapshot/v1",
            "revision": "r-large-server-group",
            "generatedAt": "2026-10-10T00:00:00Z",
            "build": 1,
            "stats": {"activeItems": 1},
            "items": rows,
            "recipes": [], "servers": [], "farmSessions": [], "limitations": [],
        }
        original_limit = MODULE.MAX_VIEW_BYTES
        MODULE.MAX_VIEW_BYTES = 650
        try:
            views = MODULE.build_views(snapshot)
        finally:
            MODULE.MAX_VIEW_BYTES = original_limit

        segments = views["items.default.json"]["segments"]
        self.assertGreater(len(segments), 1)
        self.assertTrue(all(segment.startswith("s123_") for segment in segments))
        rebuilt = []
        for segment in segments:
            part = views[f"items.{segment}.json"]
            rebuilt.extend(part["items"])
            self.assertLessEqual(
                len(json.dumps(part, ensure_ascii=False, separators=(",", ":")).encode("utf-8")),
                650,
            )
        self.assertEqual([row["itemId"] for row in rebuilt], [1, 2, 3, 4])


if __name__ == "__main__":
    unittest.main()
