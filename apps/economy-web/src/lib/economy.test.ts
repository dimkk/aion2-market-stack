import { describe, expect, it } from "vitest";

import {
  bestCraftServerId,
  economySection,
  mergeFarmSessions,
  recipeEconomics,
  sectionRollups,
  selectMovers,
  validateSnapshot,
} from "./economy";
import type { EconomyItem, EconomySnapshot, FarmSession } from "../types";

const item = (
  itemId: number,
  change24h: number,
  saleCount: number,
  liquidityScore: number,
): EconomyItem => ({
  itemId,
  name: `Item ${itemId}`,
  internalName: `item_${itemId}`,
  category: "Materials",
  itemLevel: 1,
  serverId: 12301,
  market: "Global",
  current: 100,
  minimum: 100,
  median: 110,
  saleCount,
  listingCount: saleCount,
  newlyLiquid: false,
  newlyLiquidAt: null,
  liquidityScore,
  change1h: null,
  change24h,
  change7d: null,
  history: [],
});

describe("economy selectors", () => {
  it("sorts movers by the requested metric", () => {
    const items = [
      item(1, 4, 10, 0.4),
      item(2, 12, 2, 0.8),
      item(3, -7, 30, 0.2),
    ];
    expect(selectMovers(items, "up")[0].itemId).toBe(2);
    expect(selectMovers(items, "down")[0].itemId).toBe(3);
    expect(selectMovers(items, "listings")[0].itemId).toBe(3);
    expect(selectMovers(items, "liquid")[0].itemId).toBe(2);
  });

  it("does not invent mover rankings when the requested horizon is unknown", () => {
    const unknown = { ...item(4, 0, 99, 0.9), change24h: null };
    expect(selectMovers([unknown], "up")).toEqual([]);
    expect(selectMovers([unknown], "down")).toEqual([]);
    expect(selectMovers([unknown], "listings")).toEqual([unknown]);

    const fresh = {
      ...item(9, 0, 1, 0.1),
      newlyLiquid: true,
      newlyLiquidAt: "2026-10-05T01:00:00Z",
    };
    expect(selectMovers([unknown, fresh], "new")).toEqual([fresh]);
  });

  it("calculates craft profit and ROI", () => {
    const result = recipeEconomics(
      {
        recipeId: 1,
        name: "Craft",
        outputItemId: 3,
        outputQuantity: 2,
        fee: 50,
        materials: [{ itemId: 1, quantity: 2 }],
      },
      new Map([
        [1, 100],
        [3, 500],
      ]),
      0.1,
    );
    expect(result.craftCost).toBe(250);
    expect(result.buyCost).toBe(1000);
    expect(result.buyVsCraftSavings).toBe(750);
    expect(result.tax).toBe(100);
    expect(result.sellProfit).toBe(650);
    expect(result.sellRoi).toBe(260);
    expect(result.complete).toBe(true);
  });

  it("keeps incomplete craft economics unknown instead of recommending buy", () => {
    const result = recipeEconomics(
      {
        recipeId: 1,
        name: "Craft",
        outputItemId: 3,
        outputQuantity: 1,
        fee: 50,
        materials: [{ itemId: 1, quantity: 2 }],
      },
      new Map([[1, 100]]),
      null,
    );
    expect(result.complete).toBe(false);
    expect(result.missingItemIds).toEqual([3]);
    expect(result.materialCost).toBe(200);
    expect(result.craftCost).toBe(250);
    expect(result.buyCost).toBeNull();
    expect(result.buyVsCraftSavings).toBeNull();
    expect(result.sellRoi).toBeNull();
  });

  it("keeps sell economics unknown without a sourced tax while preserving buy-vs-craft", () => {
    const result = recipeEconomics(
      {
        recipeId: 1,
        name: "Craft",
        outputItemId: 3,
        outputQuantity: 1,
        fee: 50,
        materials: [{ itemId: 1, quantity: 2 }],
      },
      new Map([
        [1, 100],
        [3, 500],
      ]),
      null,
    );
    expect(result.buyVsCraftSavings).toBe(250);
    expect(result.tax).toBeNull();
    expect(result.sellProfit).toBeNull();
  });

  it("selects the craft market with the most fully priced recipes", () => {
    const recipe = {
      recipeId: 1,
      name: "Craft",
      outputItemId: 3,
      outputQuantity: 1,
      fee: 0,
      materials: [{ itemId: 1, quantity: 1 }],
    };
    const sparse = { ...item(1, 0, 1, 0), serverId: 2301 };
    const completeMaterial = { ...item(1, 0, 1, 0), serverId: 12301 };
    const completeOutput = { ...item(3, 0, 1, 0), serverId: 12301 };
    expect(
      bestCraftServerId([recipe], [sparse, completeMaterial, completeOutput]),
    ).toBe(12301);
  });

  it("deduplicates farm sessions by stable ID", () => {
    const base: FarmSession = {
      sessionId: "s1",
      startedAt: "2026-01-01T00:00:00Z",
      endedAt: "2026-01-01T00:01:00Z",
      durationSeconds: 60,
      directKina: 100,
      lootValue: 200,
      totalValue: 300,
      kinaPerSecond: 5,
      source: "local",
      loot: [],
    };
    expect(mergeFarmSessions([base], [{ ...base, source: "server" }])).toEqual([
      { ...base, source: "server" },
    ]);
  });

  it("deduplicates local and server farm rows by payload id", () => {
    const local: FarmSession = {
      sessionId: "farm-1",
      payloadId: "a".repeat(64),
      startedAt: "2026-01-01T00:00:00Z",
      endedAt: "2026-01-01T00:01:00Z",
      durationSeconds: 60,
      directKina: 100,
      lootValue: 200,
      totalValue: 300,
      kinaPerSecond: 5,
      source: "local",
      loot: [],
    };
    const remote = {
      ...local,
      sessionId: "server-row",
      source: "server" as const,
    };
    expect(mergeFarmSessions([local], [remote])).toEqual([remote]);
  });

  it("rejects incompatible snapshots", () => {
    expect(() => validateSnapshot({ schemaVersion: "other" })).toThrow(
      "snapshot_schema_mismatch",
    );
    const good: EconomySnapshot = {
      schemaVersion: "aion2.economy.snapshot/v1",
      generatedAt: "",
      build: null,
      stats: { catalogItems: 0, activeItems: 0, listings: 0, servers: 0 },
      items: [],
      recipes: [],
      servers: [],
      limitations: [],
    };
    expect(validateSnapshot(good)).toBe(good);
  });

  it("builds poe.ninja-style rollups from confirmed client prefixes", () => {
    const gear = {
      ...item(1, 4, 10, 0.4),
      category: "Equip",
      internalName: "equip_bow",
      median: 120,
    };
    const material = {
      ...item(2, 2, 20, 0.8),
      category: "Usable",
      internalName: "material_ore",
      median: 40,
    };
    const potion = {
      ...item(3, -2, 30, 0.5),
      category: "Usable",
      internalName: "potion_hp",
      median: 20,
    };
    expect(economySection(gear)).toBe("Gear");
    expect(economySection(material)).toBe("Materials");
    expect(
      economySection({
        ...item(4, 0, 1, 0.1),
        internalName: "BM_unknown",
        category: "Usable",
      }),
    ).toBe("Other");
    expect(economySection(potion)).toBe("Consumables");
    const rollups = sectionRollups([gear, material, potion]);
    expect(rollups.find((row) => row.section === "Materials")).toMatchObject({
      activeItems: 1,
      medianPrice: 40,
      saleCount: 20,
    });
    expect(rollups.find((row) => row.section === "Crafting")).toMatchObject({
      activeItems: 0,
      medianPrice: null,
    });
  });
});
