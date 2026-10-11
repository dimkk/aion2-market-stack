"""Farm-session state machine independent from capture and UI."""

from __future__ import annotations

import threading
import uuid
from typing import Any

from .contracts import DecodedEconomyEvent
from .history import HistoryStore
from .quotes import ItemCatalog, MarketPriceProvider


class FarmSessionTracker:
    def __init__(
        self,
        history: HistoryStore,
        prices: MarketPriceProvider,
        catalog: ItemCatalog,
        idle_seconds: int = 15,
    ) -> None:
        if not 1 <= idle_seconds <= 3_600:
            raise ValueError("farm_idle_timeout_invalid")
        self.history = history
        self.prices = prices
        self.catalog = catalog
        self.idle_ms = idle_seconds * 1000
        self._lock = threading.RLock()
        self._active: dict[str, Any] | None = None
        self._activity: dict[str, Any] | None = None

    def snapshot(self) -> dict[str, Any] | None:
        with self._lock:
            if self._active is None:
                return None
            return dict(self._active, loot=[dict(item) for item in self._active["loot"]])

    def _start(self, at_ms: int) -> dict[str, Any]:
        session = {
            "session_id": "farm-" + uuid.uuid4().hex,
            "status": "running",
            "activity": dict(self._activity) if self._activity else None,
            "started_at_ms": at_ms,
            "stopped_at_ms": None,
            "last_kina_at_ms": at_ms,
            "observed_direct_kina": 0,
            "estimated_loot_value": 0,
            "estimated_total_value": 0,
            "event_count": 0,
            "loot": [],
        }
        self._active = session
        self.history.persist(session, None, None)
        return session

    def _stop(self, at_ms: int) -> dict[str, Any] | None:
        if self._active is None:
            return None
        self._active["status"] = "stopped"
        self._active["stopped_at_ms"] = max(self._active["started_at_ms"] + 1, at_ms)
        self.history.persist(self._active, None, None)
        stopped = self._active
        self._active = None
        return stopped

    def expire(self, now_ms: int) -> dict[str, Any] | None:
        with self._lock:
            if self._active is None:
                return None
            deadline = self._active["last_kina_at_ms"] + self.idle_ms
            return self._stop(deadline) if now_ms >= deadline else None

    def stop(self, now_ms: int) -> dict[str, Any] | None:
        with self._lock:
            return self._stop(now_ms)

    def apply(self, value: dict[str, Any] | DecodedEconomyEvent) -> dict[str, Any]:
        event = value if isinstance(value, DecodedEconomyEvent) else DecodedEconomyEvent.parse(value)
        with self._lock:
            stopped = self.expire(event.observed_at_ms)
            if self.history.has_event(event.event_id):
                return {"applied": False, "auto_started": False, "auto_stopped": stopped is not None}
            if event.kind in {"zone_identity", "zone_map_identity"}:
                self._activity = {"kind": "spot", "label": event.label}
                if self._active is not None:
                    self._active["activity"] = dict(self._activity)
                    self.history.persist(self._active, None, None)
                self.history.remember_event(event.event_id, event.observed_at_ms)
                return {"applied": True, "auto_started": False, "auto_stopped": stopped is not None}
            if event.kind == "heartbeat":
                return {"applied": False, "auto_started": False, "auto_stopped": stopped is not None}
            starts = event.kind == "loot" or (event.kind == "kina_delta" and int(event.delta or 0) > 0)
            auto_started = False
            if self._active is None and starts:
                self._start(event.observed_at_ms)
                auto_started = True
            if self._active is None:
                return {"applied": False, "auto_started": False, "auto_stopped": stopped is not None}
            candidate = dict(self._active, loot=[dict(item) for item in self._active["loot"]])
            candidate["event_count"] += 1
            if event.kind == "kina_delta":
                candidate["observed_direct_kina"] += int(event.delta or 0)
                if int(event.delta or 0) > 0:
                    candidate["last_kina_at_ms"] = event.observed_at_ms
            elif event.kind == "loot":
                item_id, quantity = int(event.item_id or 0), int(event.quantity or 0)
                quote = self.prices.lookup(item_id, event.observed_at_ms)
                candidate["loot"].append({
                    "item_id": item_id,
                    "item_name": self.catalog.name(item_id),
                    "quantity": quantity,
                    "unit_price": quote.unit_price if quote else None,
                    "market_server_id": quote.market_server_id if quote else None,
                    "price_observed_at_ms": quote.observed_at_ms if quote else None,
                    "price_source": quote.source if quote else None,
                    "estimated_value": quote.unit_price * quantity if quote else None,
                })
            candidate["estimated_loot_value"] = sum(int(item["estimated_value"] or 0) for item in candidate["loot"])
            candidate["estimated_total_value"] = candidate["observed_direct_kina"] + candidate["estimated_loot_value"]
            if not self.history.persist(candidate, event.event_id, event.observed_at_ms):
                return {"applied": False, "auto_started": auto_started, "auto_stopped": stopped is not None}
            self._active = candidate
            return {"applied": True, "auto_started": auto_started, "auto_stopped": stopped is not None}
