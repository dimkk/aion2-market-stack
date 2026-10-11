import type {
  EconomyItem,
  EconomyRecipe,
  EconomySnapshot,
  FarmSession,
} from "../types";

export type MoverKind = "up" | "down" | "new" | "liquid" | "listings";
export type EconomySection =
  "Materials" | "Refining" | "Crafting" | "Gear" | "Consumables" | "Other";

export type EconomySectionRollup = {
  section: EconomySection;
  activeItems: number;
  medianPrice: number | null;
  change24h: number | null;
  liquidityScore: number;
  saleCount: number;
};

const prefix = (item: EconomyItem) =>
  item.internalName.toLowerCase().split("_", 1)[0];

export function economySection(item: EconomyItem): EconomySection {
  const value = prefix(item);
  if (
    item.category === "Equip" ||
    ["equip", "wing", "godstone"].includes(value)
  )
    return "Gear";
  if (["material", "spiritstone", "magicstone", "sealstone"].includes(value))
    return "Materials";
  if (
    ["elevate", "enchant", "enchantscroll", "charge", "recharge"].includes(
      value,
    )
  )
    return "Refining";
  if (["craft", "recipe", "conversion"].includes(value)) return "Crafting";
  if (
    [
      "food",
      "drink",
      "potion",
      "candy",
      "scroll",
      "questscroll",
      "kisk",
      "box",
    ].includes(value)
  )
    return "Consumables";
  return "Other";
}

function numericMedian(values: number[]): number | null {
  if (!values.length) return null;
  const sorted = [...values].sort((a, b) => a - b);
  const middle = Math.floor(sorted.length / 2);
  return sorted.length % 2
    ? sorted[middle]
    : (sorted[middle - 1] + sorted[middle]) / 2;
}

export function sectionRollups(items: EconomyItem[]): EconomySectionRollup[] {
  const sections: EconomySection[] = [
    "Materials",
    "Refining",
    "Crafting",
    "Gear",
    "Consumables",
    "Other",
  ];
  return sections.map((section) => {
    const rows = items.filter(
      (item) =>
        economySection(item) === section &&
        item.current != null &&
        item.saleCount > 0,
    );
    return {
      section,
      activeItems: rows.length,
      medianPrice: numericMedian(
        rows.flatMap((item) => (item.median == null ? [] : [item.median])),
      ),
      change24h: numericMedian(
        rows.flatMap((item) =>
          item.change24h == null ? [] : [item.change24h],
        ),
      ),
      liquidityScore: rows.length
        ? rows.reduce((sum, item) => sum + item.liquidityScore, 0) / rows.length
        : 0,
      saleCount: rows.reduce((sum, item) => sum + item.saleCount, 0),
    };
  });
}

export function selectMovers(
  items: EconomyItem[],
  kind: MoverKind,
  limit = 20,
): EconomyItem[] {
  const active = items.filter(
    (item) =>
      item.current != null &&
      item.saleCount > 0 &&
      (kind === "up" || kind === "down" ? item.change24h != null : true) &&
      (kind === "new" ? item.newlyLiquid : true),
  );
  return [...active]
    .sort((a, b) => {
      if (kind === "up")
        return (b.change24h ?? -Infinity) - (a.change24h ?? -Infinity);
      if (kind === "down")
        return (a.change24h ?? Infinity) - (b.change24h ?? Infinity);
      if (kind === "new")
        return (b.newlyLiquidAt ?? "").localeCompare(a.newlyLiquidAt ?? "");
      if (kind === "liquid") return b.liquidityScore - a.liquidityScore;
      return b.listingCount - a.listingCount;
    })
    .slice(0, limit);
}

export function recipeEconomics(
  recipe: EconomyRecipe,
  priceByItem: Map<number, number>,
  saleTaxRate: number | null,
) {
  const missingItemIds = recipe.materials
    .filter((material) => priceByItem.get(material.itemId) == null)
    .map((material) => material.itemId);
  const materialsComplete = missingItemIds.length === 0;
  const materialCost = materialsComplete
    ? recipe.materials.reduce(
        (sum, material) =>
          sum + priceByItem.get(material.itemId)! * material.quantity,
        0,
      )
    : null;
  const outputUnitPrice = priceByItem.get(recipe.outputItemId);
  if (outputUnitPrice == null) missingItemIds.push(recipe.outputItemId);
  const complete = materialsComplete && outputUnitPrice != null;
  const buyCost =
    outputUnitPrice == null ? null : outputUnitPrice * recipe.outputQuantity;
  const craftCost = materialCost == null ? null : materialCost + recipe.fee;
  const buyVsCraftSavings =
    buyCost == null || craftCost == null ? null : buyCost - craftCost;
  const tax =
    buyCost == null || saleTaxRate == null ? null : buyCost * saleTaxRate;
  const sellProfit =
    buyCost == null || tax == null || craftCost == null
      ? null
      : buyCost - tax - craftCost;
  const sellRoi =
    sellProfit == null || craftCost == null || craftCost <= 0
      ? null
      : (sellProfit / craftCost) * 100;
  return {
    complete,
    missingItemIds: [...new Set(missingItemIds)],
    materialCost,
    craftCost,
    buyCost,
    buyVsCraftSavings,
    saleTaxRate,
    tax,
    sellProfit,
    sellRoi,
  };
}

export function bestCraftServerId(
  recipes: EconomyRecipe[],
  items: EconomyItem[],
): number | null {
  const serverIds = [...new Set(items.map((item) => item.serverId))].sort(
    (a, b) => a - b,
  );
  let bestServerId: number | null = null;
  let bestCoverage = -1;
  for (const serverId of serverIds) {
    const prices = new Map(
      items.flatMap((item) =>
        item.serverId === serverId && item.current != null
          ? ([[item.itemId, item.current]] as Array<[number, number]>)
          : [],
      ),
    );
    const coverage = recipes.reduce(
      (count, recipe) =>
        count + (recipeEconomics(recipe, prices, null).complete ? 1 : 0),
      0,
    );
    if (coverage > bestCoverage) {
      bestCoverage = coverage;
      bestServerId = serverId;
    }
  }
  return bestServerId;
}

export function mergeFarmSessions(
  local: FarmSession[],
  remote: FarmSession[],
): FarmSession[] {
  const sessions = new Map<string, FarmSession>();
  for (const session of [...local, ...remote]) {
    sessions.set(session.payloadId ?? session.sessionId, session);
  }
  return [...sessions.values()].sort((a, b) =>
    b.startedAt.localeCompare(a.startedAt),
  );
}

export function validateSnapshot(value: unknown): EconomySnapshot {
  if (!value || typeof value !== "object")
    throw new Error("snapshot_not_object");
  const snapshot = value as Partial<EconomySnapshot>;
  if (snapshot.schemaVersion !== "aion2.economy.snapshot/v1")
    throw new Error("snapshot_schema_mismatch");
  if (
    !Array.isArray(snapshot.items) ||
    !Array.isArray(snapshot.servers) ||
    !Array.isArray(snapshot.recipes)
  ) {
    throw new Error("snapshot_collections_missing");
  }
  return snapshot as EconomySnapshot;
}
