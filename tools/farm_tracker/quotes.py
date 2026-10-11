"""Read-only market quotes and item names for farm valuation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
from pathlib import Path
import sqlite3


@dataclass(frozen=True)
class MarketQuote:
    item_id: int
    unit_price: int
    market_server_id: int
    observed_at_ms: int
    source: str


def _utc_ms(value: str) -> int:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1000)


class ItemCatalog:
    def __init__(self, path: Path) -> None:
        value = json.loads(path.read_text(encoding="utf-8"))
        rows = value.get("items") if isinstance(value, dict) else None
        if not isinstance(rows, list):
            raise ValueError("catalog_items_missing")
        self.names: dict[int, str] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            item_id = row.get("itemId", row.get("item_id", row.get("id")))
            name = row.get("displayName", row.get("display_name", row.get("name")))
            if isinstance(item_id, int) and 0 < item_id <= 0xFFFFFFFF and isinstance(name, str) and name.strip():
                self.names[item_id] = name.strip()
        if not self.names:
            raise ValueError("catalog_items_missing")

    def name(self, item_id: int) -> str:
        return self.names.get(item_id, f"Item {item_id}")


class MarketPriceProvider:
    def __init__(self, database: Path | None, market_server_id: int, max_age_seconds: int = 86_400) -> None:
        if market_server_id <= 0 or max_age_seconds <= 0:
            raise ValueError("market_quote_configuration_invalid")
        self.database = database
        self.market_server_id = market_server_id
        self.max_age_ms = max_age_seconds * 1000

    def lookup(self, item_id: int, now_ms: int) -> MarketQuote | None:
        if self.database is None:
            return None
        uri = f"file:{self.database.resolve().as_posix()}?mode=ro"
        try:
            with sqlite3.connect(uri, uri=True, timeout=2) as connection:
                row = connection.execute(
                    """
                    SELECT sp.min_unit_price,sr.observed_at
                      FROM summary_prices sp
                      JOIN summary_runs sr ON sr.summary_id=sp.summary_id
                     WHERE sp.item_id=? AND sr.game_server_id=? AND sp.sale_count>0
                     ORDER BY sr.observed_at DESC,CAST(sp.min_unit_price AS INTEGER) ASC
                     LIMIT 1
                    """,
                    (item_id, self.market_server_id),
                ).fetchone()
                source = "market-summary"
                if row is None:
                    row = connection.execute(
                        """
                        SELECT CAST(l.unit_price AS INTEGER),s.observed_at
                          FROM listings l
                          JOIN scans s ON s.scan_id=l.scan_id
                          JOIN item_server_state st
                            ON st.game_server_id=s.game_server_id AND st.item_id=l.item_id
                         WHERE l.item_id=? AND s.game_server_id=?
                           AND l.generation=st.current_generation
                         ORDER BY s.observed_at DESC,CAST(l.unit_price AS INTEGER) ASC
                         LIMIT 1
                        """,
                        (item_id, self.market_server_id),
                    ).fetchone()
                    source = "market-listing"
        except (OSError, sqlite3.Error, ValueError):
            return None
        if row is None:
            return None
        try:
            price, observed = int(row[0]), _utc_ms(str(row[1]))
        except (TypeError, ValueError, OverflowError):
            return None
        if price < 0 or now_ms - observed > self.max_age_ms or observed > now_ms + 60_000:
            return None
        return MarketQuote(item_id, price, self.market_server_id, observed, source)
