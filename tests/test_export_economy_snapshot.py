import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "export_economy_snapshot", ROOT / "scripts" / "export_economy_snapshot.py"
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class EconomySnapshotTests(unittest.TestCase):
    def test_liquidity_score_is_bounded_when_detail_exceeds_summary(self):
        self.assertEqual(0.0, MODULE._liquidity_score(0, 10))
        self.assertEqual(0.0, MODULE._liquidity_score(10, 0))
        self.assertLess(MODULE._liquidity_score(2, 10), 1.0)
        self.assertEqual(1.0, MODULE._liquidity_score(11, 10))

    def test_exports_current_median_history_and_horizon_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "market.sqlite"
            connection = sqlite3.connect(database)
            connection.executescript(
                """
                CREATE TABLE catalogs(applies_to_build INTEGER,recovered_item_count INTEGER,imported_at TEXT);
                CREATE TABLE items(item_id INTEGER,display_name TEXT,display_name_ru TEXT,internal_name TEXT,item_type TEXT,
                  item_level INTEGER,last_listing_count INTEGER,current_generation INTEGER);
                CREATE TABLE item_server_state(game_server_id INTEGER,item_id INTEGER,current_generation INTEGER,
                  scan_status TEXT,last_scanned_at TEXT,last_listing_count INTEGER);
                CREATE TABLE summary_runs(summary_id INTEGER,observed_at TEXT,game_server_id INTEGER,summary_count INTEGER);
                CREATE TABLE summary_prices(summary_id INTEGER,item_id INTEGER,min_unit_price TEXT,sale_count INTEGER);
                CREATE TABLE scans(scan_id INTEGER,observed_at TEXT,game_server_id INTEGER);
                CREATE TABLE listings(scan_id INTEGER,sale_id TEXT,item_id INTEGER,unit_price TEXT,generation INTEGER);
                INSERT INTO catalogs VALUES(25650019,1,'2026-10-01T00:00:00Z');
                INSERT INTO items VALUES(42,'Courage Bow','Лук отваги','item_courage_bow','Gear',41,2,1);
                INSERT INTO item_server_state VALUES(12301,42,1,'scanned_listed','2026-10-04T01:00:00Z',2);
                INSERT INTO summary_runs VALUES(1,'2026-10-03T00:00:00Z',12301,1);
                INSERT INTO summary_runs VALUES(2,'2026-10-04T00:00:00Z',12301,1);
                INSERT INTO summary_runs VALUES(3,'2026-10-04T01:00:00Z',12301,1);
                INSERT INTO summary_prices VALUES(1,42,'100',2);
                INSERT INTO summary_prices VALUES(2,42,'150',4);
                INSERT INTO summary_prices VALUES(3,42,'200',8);
                INSERT INTO scans VALUES(1,'2026-10-04T01:00:00Z',12301);
                INSERT INTO scans VALUES(2,'2026-10-04T00:59:00Z',12301);
                INSERT INTO listings VALUES(1,'sale-a',42,'200',1);
                INSERT INTO listings VALUES(2,'sale-b',42,'300',1);
                """
            )
            connection.commit()
            connection.close()

            recipes = Path(directory) / "recipes.json"
            recipes.write_text(json.dumps({
                "schemaVersion": "aion2.craft-recipes/v1",
                "appliesToBuild": 25650019,
                "recipes": [
                    {
                        "recipeId": 7,
                        "name": "Visible recipe",
                        "category": "Jewelcrafting",
                        "subcategory": "Accessory",
                        "subtab": "Earring",
                        "showRecipe": True,
                        "fee": 12,
                        "output": {"itemId": 42, "quantity": 2, "name": "Courage Bow"},
                        "materials": [{
                            "itemId": 77,
                            "quantity": 3,
                            "name": "Fine Ore",
                            "names": {"en": "Fine Ore", "ru": "Чистая руда"},
                        }],
                        "internalName": "must_not_be_exported",
                    },
                    {
                        "recipeId": 8,
                        "name": "Hidden combo",
                        "showRecipe": False,
                        "fee": 0,
                        "output": {"itemId": 99, "quantity": 1},
                        "materials": [],
                    },
                ],
            }), encoding="utf-8")

            snapshot = MODULE.build_snapshot(database, recipes_path=recipes)
            self.assertEqual(snapshot["schemaVersion"], "aion2.economy.snapshot/v1")
            self.assertRegex(snapshot["revision"], r"^b25650019-20261004T010000Z-c[0-9a-f]{12}$")
            self.assertEqual(snapshot["build"], 25650019)
            item = snapshot["items"][0]
            self.assertEqual(item["lastObservedAt"], "2026-10-04T01:00:00Z")
            self.assertEqual(item["current"], 200)
            self.assertEqual(item["median"], 250)
            self.assertEqual(item["change1h"], 33.33)
            self.assertEqual(item["change24h"], 100.0)
            self.assertIsNone(item["change7d"])
            self.assertEqual(item["saleCount"], 2)
            self.assertEqual(item["listingCount"], 2)
            self.assertFalse(item["newlyLiquid"])
            self.assertIsNone(item["newlyLiquidAt"])
            self.assertEqual(snapshot["stats"]["listings"], 2)
            self.assertEqual(snapshot["servers"][0]["region"], "unconfirmed")
            self.assertEqual(snapshot["servers"][0]["market"], "unconfirmed")
            self.assertEqual(item["market"], "unconfirmed")
            self.assertFalse(snapshot["servers"][0]["dimensionsConfirmed"])
            self.assertIsNone(snapshot["servers"][0]["dimensionSource"])
            self.assertEqual(snapshot["recipes"], [{
                "recipeId": 7,
                "name": "Visible recipe",
                "names": {"en": "Courage Bow", "ru": "Лук отваги"},
                "category": "Jewelcrafting",
                "subcategory": "Accessory",
                "subtab": "Earring",
                "outputItemId": 42,
                "outputName": "Courage Bow",
                "outputNames": {"en": "Courage Bow", "ru": "Лук отваги"},
                "outputQuantity": 2,
                "fee": 12,
                "materials": [{
                    "itemId": 77,
                    "quantity": 3,
                    "name": "Fine Ore",
                    "names": {"en": "Fine Ore", "ru": "Чистая руда"},
                }],
            }])
            self.assertNotIn("internalName", snapshot["recipes"][0])
            self.assertFalse(any("Craft recipes remain disabled" in row for row in snapshot["limitations"]))

    def test_item_freshness_does_not_inherit_server_max_timestamp(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "market.sqlite"
            connection = sqlite3.connect(database)
            connection.executescript(
                """
                CREATE TABLE catalogs(applies_to_build INTEGER,recovered_item_count INTEGER,imported_at TEXT);
                CREATE TABLE items(item_id INTEGER,display_name TEXT,internal_name TEXT,item_type TEXT,
                  item_level INTEGER,last_listing_count INTEGER,current_generation INTEGER);
                CREATE TABLE item_server_state(game_server_id INTEGER,item_id INTEGER,current_generation INTEGER);
                CREATE TABLE summary_runs(summary_id INTEGER,observed_at TEXT,game_server_id INTEGER,summary_count INTEGER);
                CREATE TABLE summary_prices(summary_id INTEGER,item_id INTEGER,min_unit_price TEXT,sale_count INTEGER);
                CREATE TABLE scans(scan_id INTEGER,observed_at TEXT,game_server_id INTEGER);
                CREATE TABLE listings(scan_id INTEGER,sale_id TEXT,item_id INTEGER,unit_price TEXT,generation INTEGER);
                INSERT INTO catalogs VALUES(25650019,2,'2026-10-01T00:00:00Z');
                INSERT INTO items VALUES(41,'Old','item_old','Material',1,1,1);
                INSERT INTO items VALUES(42,'Fresh','item_fresh','Material',1,1,1);
                INSERT INTO item_server_state VALUES(2301,41,1);
                INSERT INTO item_server_state VALUES(2301,42,1);
                INSERT INTO scans VALUES(1,'2026-10-04T06:20:00Z',2301);
                INSERT INTO scans VALUES(2,'2026-10-06T04:16:00Z',2301);
                INSERT INTO scans VALUES(3,'2026-10-06T06:00:00Z',2301);
                INSERT INTO listings VALUES(1,'old',41,'100',1);
                INSERT INTO listings VALUES(2,'fresh',42,'200',1);
                """
            )
            connection.commit()
            connection.close()

            snapshot = MODULE.build_snapshot(database)
            by_id = {row["itemId"]: row for row in snapshot["items"]}
            self.assertEqual(by_id[41]["lastObservedAt"], "2026-10-04T06:20:00Z")
            self.assertEqual(by_id[42]["lastObservedAt"], "2026-10-06T04:16:00Z")
            self.assertEqual(snapshot["servers"][0]["oldestActiveObservedAt"], "2026-10-04T06:20:00Z")
            self.assertEqual(snapshot["servers"][0]["newestActiveObservedAt"], "2026-10-06T04:16:00Z")
            self.assertEqual(snapshot["servers"][0]["lastObservedAt"], "2026-10-06T06:00:00Z")

    def test_exports_sourced_market_fees_and_newly_liquid_transition(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "market.sqlite"
            connection = sqlite3.connect(database)
            connection.executescript(
                """
                CREATE TABLE catalogs(applies_to_build INTEGER,recovered_item_count INTEGER,imported_at TEXT);
                CREATE TABLE items(item_id INTEGER,display_name TEXT,internal_name TEXT,item_type TEXT,
                  item_level INTEGER,last_listing_count INTEGER,current_generation INTEGER);
                CREATE TABLE item_server_state(game_server_id INTEGER,item_id INTEGER,current_generation INTEGER);
                CREATE TABLE summary_runs(summary_id INTEGER,observed_at TEXT,game_server_id INTEGER,summary_count INTEGER);
                CREATE TABLE summary_prices(summary_id INTEGER,item_id INTEGER,min_unit_price TEXT,sale_count INTEGER);
                CREATE TABLE scans(scan_id INTEGER,observed_at TEXT,game_server_id INTEGER);
                CREATE TABLE listings(scan_id INTEGER,sale_id TEXT,item_id INTEGER,unit_price TEXT,generation INTEGER);
                INSERT INTO catalogs VALUES(25650019,2,'2026-10-01T00:00:00Z');
                INSERT INTO items VALUES(41,'Old','item_old','Material',1,1,1);
                INSERT INTO items VALUES(42,'New','item_new','Material',1,1,1);
                INSERT INTO summary_runs VALUES(1,'2026-10-04T00:00:00Z',12301,1);
                INSERT INTO summary_runs VALUES(2,'2026-10-05T00:00:00Z',12301,2);
                INSERT INTO summary_prices VALUES(1,41,'100',1);
                INSERT INTO summary_prices VALUES(2,41,'100',1);
                INSERT INTO summary_prices VALUES(2,42,'200',2);
                """
            )
            connection.commit()
            connection.close()
            rules = Path(directory) / "market-rules.json"
            rules.write_text(json.dumps({
                "schemaVersion": "aion2.market-rules/v1",
                "appliesToBuild": 25650019,
                "observedAt": "2026-10-04T13:03:00Z",
                "source": {
                    "authority": "qos-api.g.nc.com",
                    "path": "/trade/v1.0/policies",
                    "captureSha256": "a" * 64,
                    "gameServerIds": [12301, 2301],
                },
                "serverFees": [{"gameServerId": 12301, "fees": [
                    {"feeType": "RatioSellerCommission", "rawValue": 5},
                    {"feeType": "RatioRegistrationFee", "rawValue": 1},
                ],
                }],
            }), encoding="utf-8")

            snapshot = MODULE.build_snapshot(database, market_rules_path=rules)

            by_id = {row["itemId"]: row for row in snapshot["items"]}
            self.assertFalse(by_id[41]["newlyLiquid"])
            self.assertTrue(by_id[42]["newlyLiquid"])
            self.assertEqual(by_id[42]["newlyLiquidAt"], "2026-10-05T00:00:00Z")
            self.assertIsNone(snapshot["marketRules"]["saleTaxRate"])
            self.assertEqual(snapshot["marketRules"]["serverRates"][0]["saleTaxRate"], 0.05)
            self.assertIn("qos-api.g.nc.com/trade/v1.0/policies", snapshot["marketRules"]["serverRates"][0]["source"])

    def test_projects_evidenced_eu_na_and_cross_server_dimensions(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "market.sqlite"
            connection = sqlite3.connect(database)
            connection.executescript(
                """
                CREATE TABLE catalogs(applies_to_build INTEGER,recovered_item_count INTEGER,imported_at TEXT);
                CREATE TABLE items(item_id INTEGER,display_name TEXT,internal_name TEXT,item_type TEXT,
                  item_level INTEGER,last_listing_count INTEGER,current_generation INTEGER);
                CREATE TABLE item_server_state(game_server_id INTEGER,item_id INTEGER,current_generation INTEGER);
                CREATE TABLE summary_runs(summary_id INTEGER,observed_at TEXT,game_server_id INTEGER,summary_count INTEGER);
                CREATE TABLE summary_prices(summary_id INTEGER,item_id INTEGER,min_unit_price TEXT,sale_count INTEGER);
                CREATE TABLE scans(scan_id INTEGER,observed_at TEXT,game_server_id INTEGER);
                CREATE TABLE listings(scan_id INTEGER,sale_id TEXT,item_id INTEGER,unit_price TEXT,generation INTEGER);
                INSERT INTO catalogs VALUES(25650019,1,'2026-10-01T00:00:00Z');
                INSERT INTO items VALUES(42,'Synthetic Item','item_synthetic','Material',1,3,1);
                INSERT INTO summary_runs VALUES(1,'2026-10-05T00:00:00Z',101,1);
                INSERT INTO summary_runs VALUES(2,'2026-10-05T00:01:00Z',102,1);
                INSERT INTO summary_runs VALUES(3,'2026-10-05T00:02:00Z',201,1);
                INSERT INTO summary_prices VALUES(1,42,'100',1);
                INSERT INTO summary_prices VALUES(2,42,'200',1);
                INSERT INTO summary_prices VALUES(3,42,'300',1);
                """
            )
            connection.commit()
            connection.close()
            dimensions = Path(directory) / "dimensions.json"
            dimensions.write_text(json.dumps({
                "schemaVersion": "aion2.market-dimensions/v1",
                "appliesToBuild": 25650019,
                "observedAt": "2026-10-05T00:03:00Z",
                "source": {
                    "kind": "client-derived",
                    "authority": "unit-test.local",
                    "reference": "/synthetic/server-dimensions",
                    "evidenceSha256": "b" * 64,
                },
                "servers": [
                    {"gameServerId": 101, "region": "EU", "name": "EU Test Server", "faction": "Test Faction A", "market": "server"},
                    {"gameServerId": 102, "region": "EU", "name": "EU Test Pool", "faction": "All test factions", "market": "cross-server"},
                    {"gameServerId": 201, "region": "NA", "name": "NA Test Server", "faction": "Test Faction B", "market": "server"},
                ],
            }), encoding="utf-8")

            snapshot = MODULE.build_snapshot(database, market_dimensions_path=dimensions)

            by_id = {row["serverId"]: row for row in snapshot["servers"]}
            self.assertEqual(("EU", "EU Test Server", "Test Faction A", "server"), (
                by_id[101]["region"], by_id[101]["name"], by_id[101]["faction"], by_id[101]["market"],
            ))
            self.assertEqual("cross-server", by_id[102]["market"])
            self.assertEqual("NA", by_id[201]["region"])
            self.assertTrue(all(row["dimensionsConfirmed"] for row in by_id.values()))
            self.assertTrue(all(row["dimensionSource"].startswith("client-derived:unit-test.local") for row in by_id.values()))
            self.assertEqual({101: "server", 102: "cross-server", 201: "server"}, {
                row["serverId"]: row["market"] for row in snapshot["items"]
            })
            self.assertFalse(any("remain unconfirmed" in row for row in snapshot["limitations"]))

    def test_does_not_infer_missing_dimension_rows(self):
        dimensions = {
            "schemaVersion": "aion2.market-dimensions/v1",
            "appliesToBuild": 25650019,
            "observedAt": "2026-10-05T00:03:00Z",
            "source": {
                "kind": "official-contract",
                "authority": "example.invalid",
                "reference": "/synthetic",
                "evidenceSha256": "c" * 64,
            },
            "servers": [
                {"gameServerId": 101, "region": "EU", "name": "EU Test", "faction": "Test Faction", "market": "server"},
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dimensions.json"
            path.write_text(json.dumps(dimensions), encoding="utf-8")
            loaded, source = MODULE._load_market_dimensions(path, 25650019)
            self.assertEqual({101}, set(loaded))
            self.assertNotIn(202, loaded)
            self.assertIn("official-contract:example.invalid", source)

    def test_rejects_market_dimensions_build_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dimensions.json"
            path.write_text(json.dumps({
                "schemaVersion": "aion2.market-dimensions/v1",
                "appliesToBuild": 1,
                "observedAt": "2026-10-05T00:03:00Z",
                "source": {
                    "kind": "client-derived", "authority": "example.invalid",
                    "reference": "/synthetic", "evidenceSha256": "d" * 64,
                },
                "servers": [
                    {"gameServerId": 101, "region": "EU", "name": "Test", "faction": "Test", "market": "server"},
                ],
            }), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "market_dimensions_build_mismatch"):
                MODULE._load_market_dimensions(path, 2)

    def test_rejects_market_dimensions_when_catalog_build_is_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dimensions.json"
            path.write_text(json.dumps({
                "schemaVersion": "aion2.market-dimensions/v1",
                "appliesToBuild": None,
                "observedAt": "2026-10-05T00:03:00Z",
                "source": {
                    "kind": "client-derived", "authority": "example.invalid",
                    "reference": "/synthetic", "evidenceSha256": "e" * 64,
                },
                "servers": [
                    {"gameServerId": 101, "region": "EU", "name": "Test", "faction": "Test", "market": "server"},
                ],
            }), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "market_dimensions_build_mismatch"):
                MODULE._load_market_dimensions(path, None)

    def test_loads_only_build_scoped_content_hashed_item_icons(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "icons.json"
            path.write_text(json.dumps({
                "schemaVersion": "aion2.item-icons/v1",
                "appliesToBuild": 25650019,
                "items": [{
                    "itemId": 42,
                    "iconName": "Icon_Test",
                    "virtualPath": "AION2/Test/Icon_Test.uasset",
                    "sha256": "a" * 64,
                    "fileName": "item-42-aaaaaaaaaaaa.png",
                    "url": "/assets/item-42-aaaaaaaaaaaa.png",
                }],
            }), encoding="utf-8")

            self.assertEqual({42: "/assets/item-42-aaaaaaaaaaaa.png"}, MODULE._load_item_icons(path, 25650019))
            with self.assertRaisesRegex(ValueError, "item_icons_schema_mismatch"):
                MODULE._load_item_icons(path, 1)

    def test_rejects_observed_market_items_without_icons(self):
        with self.assertRaisesRegex(
            ValueError, "active_item_icons_missing:2:43,44"
        ):
            MODULE._require_active_item_icons(
                {42: "/assets/item-42-aaaaaaaaaaaa.png"}, {42, 43, 44}
            )

    def test_rejects_observed_market_items_without_real_names(self):
        with self.assertRaisesRegex(
            ValueError, "active_item_names_missing:2:43,44"
        ):
            MODULE._require_active_item_names(
                {
                    42: {"display_name": "Courage Bow"},
                    43: {"display_name": "Item 43"},
                },
                {42, 43, 44},
            )

    def test_rejects_recipe_build_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "recipes.json"
            path.write_text(json.dumps({
                "schemaVersion": "aion2.craft-recipes/v1",
                "appliesToBuild": 1,
                "recipes": [],
            }), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "craft_recipe_build_mismatch"):
                MODULE._load_recipes(path, 2)

    def test_sparse_history_and_missing_detail_do_not_invent_metrics(self):
        points = [
            MODULE.Point(MODULE._timestamp("2026-10-01T00:00:00Z"), 100, 1),
            MODULE.Point(MODULE._timestamp("2026-10-05T00:00:00Z"), 200, 1),
        ]
        self.assertIsNone(MODULE._change(points, MODULE.timedelta(hours=1)))
        self.assertIsNone(MODULE._change(points, MODULE.timedelta(hours=24)))

    def test_keeps_latest_prices_separate_for_each_observed_server(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "market.sqlite"
            connection = sqlite3.connect(database)
            connection.executescript(
                """
                CREATE TABLE catalogs(applies_to_build INTEGER,recovered_item_count INTEGER,imported_at TEXT);
                CREATE TABLE items(item_id INTEGER,display_name TEXT,internal_name TEXT,item_type TEXT,
                  item_level INTEGER,last_listing_count INTEGER,current_generation INTEGER);
                CREATE TABLE item_server_state(game_server_id INTEGER,item_id INTEGER,current_generation INTEGER);
                CREATE TABLE summary_runs(summary_id INTEGER,observed_at TEXT,game_server_id INTEGER,summary_count INTEGER);
                CREATE TABLE summary_prices(summary_id INTEGER,item_id INTEGER,min_unit_price TEXT,sale_count INTEGER);
                CREATE TABLE scans(scan_id INTEGER,observed_at TEXT,game_server_id INTEGER);
                CREATE TABLE listings(scan_id INTEGER,sale_id TEXT,item_id INTEGER,unit_price TEXT,generation INTEGER);
                INSERT INTO catalogs VALUES(25650019,1,'2026-10-01T00:00:00Z');
                INSERT INTO items VALUES(42,'Courage Bow','item_courage_bow','Gear',41,99,1);
                INSERT INTO summary_runs VALUES(1,'2026-10-04T00:00:00Z',101,1);
                INSERT INTO summary_runs VALUES(2,'2026-10-04T00:01:00Z',202,1);
                INSERT INTO summary_prices VALUES(1,42,'100',2);
                INSERT INTO summary_prices VALUES(2,42,'900',5);
                """
            )
            connection.commit()
            connection.close()

            snapshot = MODULE.build_snapshot(database)

            by_server = {row["serverId"]: row for row in snapshot["items"]}
            self.assertEqual({101, 202}, set(by_server))
            self.assertEqual(100, by_server[101]["current"])
            self.assertEqual(900, by_server[202]["current"])
            self.assertIsNone(by_server[101]["median"])
            self.assertEqual(
                {101: (1, 2), 202: (1, 5)},
                {row["serverId"]: (row["activeItems"], row["listings"])
                 for row in snapshot["servers"]},
            )

    def test_restricts_release_to_exact_market_dimension_set(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "market.sqlite"
            connection = sqlite3.connect(database)
            connection.executescript(
                """
                CREATE TABLE catalogs(applies_to_build INTEGER,recovered_item_count INTEGER,imported_at TEXT);
                CREATE TABLE items(item_id INTEGER,display_name TEXT,internal_name TEXT,item_type TEXT,
                  item_level INTEGER,last_listing_count INTEGER,current_generation INTEGER);
                CREATE TABLE item_server_state(game_server_id INTEGER,item_id INTEGER,current_generation INTEGER);
                CREATE TABLE summary_runs(summary_id INTEGER,observed_at TEXT,game_server_id INTEGER,summary_count INTEGER);
                CREATE TABLE summary_prices(summary_id INTEGER,item_id INTEGER,min_unit_price TEXT,sale_count INTEGER);
                CREATE TABLE scans(scan_id INTEGER,observed_at TEXT,game_server_id INTEGER);
                CREATE TABLE listings(scan_id INTEGER,sale_id TEXT,item_id INTEGER,unit_price TEXT,generation INTEGER);
                INSERT INTO catalogs VALUES(25650019,1,'2026-10-01T00:00:00Z');
                INSERT INTO items VALUES(42,'Synthetic Item','item_synthetic','Material',1,0,1);
                INSERT INTO summary_runs VALUES(1,'2026-10-08T00:00:00Z',101,1);
                INSERT INTO summary_runs VALUES(2,'2026-10-07T00:00:00Z',2301,1);
                INSERT INTO summary_prices VALUES(1,42,'100',1);
                INSERT INTO summary_prices VALUES(2,42,'999',1);
                """
            )
            connection.commit()
            connection.close()
            dimensions = root / "dimensions.json"
            dimensions.write_text(json.dumps({
                "schemaVersion": "aion2.market-dimensions/v1",
                "appliesToBuild": 25650019,
                "observedAt": "2026-10-08T00:00:00Z",
                "source": {
                    "kind": "client-derived",
                    "authority": "unit-test.local",
                    "reference": "/synthetic/server-dimensions",
                    "evidenceSha256": "b" * 64,
                },
                "servers": [{
                    "gameServerId": 101,
                    "region": "EU",
                    "name": "EU Test Server",
                    "faction": "Test Faction",
                    "market": "server",
                }],
            }), encoding="utf-8")

            snapshot = MODULE.build_snapshot(
                database,
                market_dimensions_path=dimensions,
                restrict_to_market_dimensions=True,
            )

            self.assertEqual([101], [row["serverId"] for row in snapshot["servers"]])
            self.assertEqual({101}, {row["serverId"] for row in snapshot["items"]})
            self.assertTrue(all(row["dimensionsConfirmed"] for row in snapshot["servers"]))

    def test_restricted_release_requires_every_dimension_to_be_observed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "market.sqlite"
            connection = sqlite3.connect(database)
            connection.executescript(
                """
                CREATE TABLE catalogs(applies_to_build INTEGER,recovered_item_count INTEGER,imported_at TEXT);
                CREATE TABLE items(item_id INTEGER,display_name TEXT,internal_name TEXT,item_type TEXT,
                  item_level INTEGER,last_listing_count INTEGER,current_generation INTEGER);
                CREATE TABLE item_server_state(game_server_id INTEGER,item_id INTEGER,current_generation INTEGER);
                CREATE TABLE summary_runs(summary_id INTEGER,observed_at TEXT,game_server_id INTEGER,summary_count INTEGER);
                CREATE TABLE summary_prices(summary_id INTEGER,item_id INTEGER,min_unit_price TEXT,sale_count INTEGER);
                CREATE TABLE scans(scan_id INTEGER,observed_at TEXT,game_server_id INTEGER);
                CREATE TABLE listings(scan_id INTEGER,sale_id TEXT,item_id INTEGER,unit_price TEXT,generation INTEGER);
                INSERT INTO catalogs VALUES(25650019,1,'2026-10-01T00:00:00Z');
                INSERT INTO items VALUES(42,'Synthetic Item','item_synthetic','Material',1,0,1);
                INSERT INTO summary_runs VALUES(1,'2026-10-08T00:00:00Z',101,1);
                INSERT INTO summary_prices VALUES(1,42,'100',1);
                """
            )
            connection.commit()
            connection.close()
            dimensions = root / "dimensions.json"
            dimensions.write_text(json.dumps({
                "schemaVersion": "aion2.market-dimensions/v1",
                "appliesToBuild": 25650019,
                "observedAt": "2026-10-08T00:00:00Z",
                "source": {
                    "kind": "client-derived",
                    "authority": "unit-test.local",
                    "reference": "/synthetic/server-dimensions",
                    "evidenceSha256": "b" * 64,
                },
                "servers": [
                    {"gameServerId": 101, "region": "EU", "name": "One", "faction": "A", "market": "server"},
                    {"gameServerId": 102, "region": "EU", "name": "Two", "faction": "B", "market": "server"},
                ],
            }), encoding="utf-8")

            with self.assertRaisesRegex(
                ValueError, "economy_snapshot_market_dimension_not_observed:102"
            ):
                MODULE.build_snapshot(
                    database,
                    market_dimensions_path=dimensions,
                    restrict_to_market_dimensions=True,
                )

    def test_complete_summary_makes_absent_old_detail_inactive(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "market.sqlite"
            connection = sqlite3.connect(database)
            connection.executescript(
                """
                CREATE TABLE catalogs(applies_to_build INTEGER,recovered_item_count INTEGER,imported_at TEXT);
                CREATE TABLE items(item_id INTEGER,display_name TEXT,internal_name TEXT,item_type TEXT,
                  item_level INTEGER,last_listing_count INTEGER,current_generation INTEGER);
                CREATE TABLE item_server_state(game_server_id INTEGER,item_id INTEGER,current_generation INTEGER,
                  scan_status TEXT,last_scanned_at TEXT,last_listing_count INTEGER);
                CREATE TABLE summary_runs(summary_id INTEGER,observed_at TEXT,game_server_id INTEGER,summary_count INTEGER);
                CREATE TABLE summary_prices(summary_id INTEGER,item_id INTEGER,min_unit_price TEXT,sale_count INTEGER);
                CREATE TABLE scans(scan_id INTEGER,observed_at TEXT,game_server_id INTEGER);
                CREATE TABLE listings(scan_id INTEGER,sale_id TEXT,item_id INTEGER,unit_price TEXT,generation INTEGER);
                INSERT INTO catalogs VALUES(25650019,1,'2026-10-01T00:00:00Z');
                INSERT INTO items VALUES(42,'Old Listed Item','old_item','Gear',1,1,1);
                INSERT INTO item_server_state VALUES(101,42,1,'scanned_listed','2026-10-07T00:00:00Z',1);
                INSERT INTO scans VALUES(1,'2026-10-07T00:00:00Z',101);
                INSERT INTO listings VALUES(1,'old-sale',42,'999',1);
                INSERT INTO summary_runs VALUES(1,'2026-10-08T00:00:00Z',101,0);
                """
            )
            connection.commit()
            connection.close()

            snapshot = MODULE.build_snapshot(database)

            self.assertEqual(1, len(snapshot["items"]))
            self.assertIsNone(snapshot["items"][0]["current"])
            self.assertIsNone(snapshot["items"][0]["median"])
            self.assertEqual(0, snapshot["items"][0]["saleCount"])
            self.assertEqual(0, snapshot["items"][0]["listingCount"])
            self.assertEqual(0, snapshot["stats"]["activeItems"])
            self.assertEqual(0, snapshot["stats"]["listings"])

    def test_fresh_summary_does_not_reuse_stale_detail_median_or_listing_count(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "market.sqlite"
            connection = sqlite3.connect(database)
            connection.executescript(
                """
                CREATE TABLE catalogs(applies_to_build INTEGER,recovered_item_count INTEGER,imported_at TEXT);
                CREATE TABLE items(item_id INTEGER,display_name TEXT,internal_name TEXT,item_type TEXT,
                  item_level INTEGER,last_listing_count INTEGER,current_generation INTEGER);
                CREATE TABLE item_server_state(game_server_id INTEGER,item_id INTEGER,current_generation INTEGER,
                  scan_status TEXT,last_scanned_at TEXT,last_listing_count INTEGER);
                CREATE TABLE summary_runs(summary_id INTEGER,observed_at TEXT,game_server_id INTEGER,summary_count INTEGER);
                CREATE TABLE summary_prices(summary_id INTEGER,item_id INTEGER,min_unit_price TEXT,sale_count INTEGER);
                CREATE TABLE scans(scan_id INTEGER,observed_at TEXT,game_server_id INTEGER);
                CREATE TABLE listings(scan_id INTEGER,sale_id TEXT,item_id INTEGER,unit_price TEXT,generation INTEGER);
                INSERT INTO catalogs VALUES(25650019,1,'2026-10-01T00:00:00Z');
                INSERT INTO items VALUES(42,'Fresh Summary Item','fresh_summary_item','Gear',1,1,1);
                INSERT INTO item_server_state VALUES(101,42,1,'scanned_listed','2026-10-07T00:00:00Z',1);
                INSERT INTO scans VALUES(1,'2026-10-07T00:00:00Z',101);
                INSERT INTO listings VALUES(1,'old-sale',42,'999',1);
                INSERT INTO summary_runs VALUES(1,'2026-10-08T00:00:00Z',101,1);
                INSERT INTO summary_prices VALUES(1,42,'100',7);
                """
            )
            connection.commit()
            connection.close()

            item = MODULE.build_snapshot(database)["items"][0]

            self.assertEqual(100, item["current"])
            self.assertEqual(100, item["minimum"])
            self.assertIsNone(item["median"])
            self.assertEqual(7, item["saleCount"])
            self.assertEqual(7, item["listingCount"])


if __name__ == "__main__":
    unittest.main()
