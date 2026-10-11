import { describe, expect, it } from "vitest";

import {
  freshnessTotals,
  marketFreshness,
  observationAge,
  STALE_AFTER_MS,
} from "./freshness";

describe("market freshness", () => {
  const now = Date.parse("2026-10-06T12:00:00Z");

  it("fails visibly for missing or old observations", () => {
    expect(marketFreshness(null, now)).toBe("unknown");
    expect(marketFreshness("2026-10-06T11:00:00Z", now)).toBe("fresh");
    expect(
      marketFreshness(new Date(now - STALE_AFTER_MS - 1).toISOString(), now),
    ).toBe("stale");
  });

  it("formats an honest localized age", () => {
    expect(observationAge("2026-10-06T10:00:00Z", "ru", now)).toBe("2 ч назад");
    expect(observationAge("2026-10-04T10:00:00Z", "en", now)).toBe("2d ago");
  });

  it("splits aggregate totals instead of hiding stale markets", () => {
    const base = {
      region: "GLOBAL",
      name: "market",
      faction: "unknown",
      market: "server",
      dimensionsConfirmed: false,
      dimensionSource: null,
      coverageItems: 1,
    };
    const totals = freshnessTotals(
      [
        {
          ...base,
          serverId: 1,
          lastObservedAt: "2026-10-06T11:00:00Z",
          activeItems: 2,
          listings: 3,
        },
        {
          ...base,
          serverId: 2,
          lastObservedAt: "2026-10-05T11:00:00Z",
          activeItems: 5,
          listings: 7,
        },
      ],
      now,
    );
    expect(totals.fresh).toEqual({ activeItems: 2, listings: 3 });
    expect(totals.stale).toEqual({ activeItems: 5, listings: 7 });
  });
});
