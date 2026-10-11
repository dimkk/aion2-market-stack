import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";

import {
  Craft,
  craftDimensionName,
  craftLocalizedName,
  craftScopeLabel,
} from "./App";
import type { EconomyItem, EconomyServer, EconomySnapshot } from "./types";

describe("craft usability helpers", () => {
  it("uses build-scoped localized material names", () => {
    const value = {
      name: "Fine Ore",
      names: { en: "Fine Ore", ru: "Чистая руда" },
    };

    expect(craftLocalizedName(value, "ru", "Item 77")).toBe("Чистая руда");
    expect(craftLocalizedName(value, "en", "Item 77")).toBe("Fine Ore");
    expect(craftDimensionName("Jewelcrafting", "ru")).toBe("Ювелирное дело");
    expect(craftDimensionName("Accessory", "ru")).toBe("Украшения");
  });

  it("disambiguates a craft market by region, server, faction, and scope id", () => {
    const server: EconomyServer = {
      region: "Europe",
      serverId: 12301,
      name: "Ariel",
      names: { en: "Ariel", ru: "Ариэль" },
      faction: "Elyos",
      market: "server",
      dimensionsConfirmed: true,
      dimensionSource: "test",
      lastObservedAt: "2026-10-09T05:32:13Z",
      coverageItems: 9100,
      activeItems: 266,
      listings: 3135,
    };

    expect(craftScopeLabel(server, 12301, "ru")).toBe(
      "Европа · Ариэль · Элийцы · #12301",
    );
  });

  it("renders the Jewelcrafting accessory path with priced linked materials", () => {
    const item = (itemId: number, current: number, name: string): EconomyItem => ({
      itemId,
      name,
      names: { en: name, ru: itemId === 77 ? "Чистая руда" : "Серьга" },
      internalName: `item_${itemId}`,
      iconUrl: null,
      grade: "",
      tier: "",
      category: "Misc",
      itemLevel: 1,
      serverId: 12301,
      market: "server",
      lastObservedAt: "2026-10-09T05:32:13Z",
      current,
      minimum: current,
      median: null,
      saleCount: 1,
      listingCount: 1,
      newlyLiquid: false,
      newlyLiquidAt: null,
      liquidityScore: 0.1,
      change1h: null,
      change24h: null,
      change7d: null,
      history: [],
    });
    const server: EconomyServer = {
      region: "Europe",
      serverId: 12301,
      name: "Ariel",
      names: { en: "Ariel", ru: "Ариэль" },
      faction: "Elyos",
      market: "server",
      dimensionsConfirmed: true,
      dimensionSource: "test",
      lastObservedAt: "2026-10-09T05:32:13Z",
      coverageItems: 2,
      activeItems: 2,
      listings: 2,
    };
    const snapshot = {
      schemaVersion: "aion2.economy.snapshot/v1",
      generatedAt: "2026-10-09T05:32:13Z",
      build: 25650019,
      stats: { catalogItems: 2, activeItems: 2, listings: 2, servers: 1 },
      items: [item(42, 1000, "Earring"), item(77, 20, "Fine Ore")],
      recipes: [{
        recipeId: 7,
        name: "Earring",
        names: { en: "Earring", ru: "Серьга" },
        category: "Jewelcrafting",
        subcategory: "Accessory",
        subtab: "Earring",
        outputItemId: 42,
        outputName: "Earring",
        outputNames: { en: "Earring", ru: "Серьга" },
        outputQuantity: 1,
        fee: 5,
        materials: [{
          itemId: 77,
          quantity: 3,
          name: "Fine Ore",
          names: { en: "Fine Ore", ru: "Чистая руда" },
        }],
      }],
      servers: [server],
      limitations: [],
      marketRules: { saleTaxRate: null, saleTaxSource: null, serverRates: [] },
    } as unknown as EconomySnapshot;
    const originalLocalStorage = globalThis.localStorage;
    Object.defineProperty(globalThis, "localStorage", {
      configurable: true,
      value: { getItem: () => null, setItem: () => undefined },
    });
    try {
      const html = renderToStaticMarkup(<Craft snapshot={snapshot} locale="ru" />);
      expect(html).toContain("Ювелирное дело");
      expect(html).toContain("Украшения");
      expect(html).toContain("Европа · Ариэль · Элийцы · #12301");
      expect(html).toContain("Чистая руда");
      expect(html).toContain('href="/items/12301/77"');
      expect(html).toContain("20 × 3 = 60");
    } finally {
      Object.defineProperty(globalThis, "localStorage", {
        configurable: true,
        value: originalLocalStorage,
      });
    }
  });
});
