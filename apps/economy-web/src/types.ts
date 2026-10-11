export type PricePoint = {
  at: string;
  value: number;
  listings?: number;
};

export type LocalizedNames = {
  en?: string;
  ru?: string;
  "en-US"?: string;
  "ru-RU"?: string;
};

export type EconomyItem = {
  itemId: number;
  name: string;
  /** Build-scoped localized names. Optional for old snapshots. */
  names?: LocalizedNames;
  internalName: string;
  /** Private, content-hashed asset URL emitted by the snapshot exporter. */
  iconUrl?: string | null;
  grade?: string | null;
  tier?: number | string | null;
  category: string;
  itemLevel: number;
  serverId: number;
  market: string;
  /** Latest observation that actually contributed this item price/listing row. */
  lastObservedAt?: string | null;
  current: number | null;
  minimum: number | null;
  median: number | null;
  saleCount: number;
  listingCount: number;
  newlyLiquid: boolean;
  newlyLiquidAt: string | null;
  liquidityScore: number;
  change1h: number | null;
  change24h: number | null;
  change7d: number | null;
  history: PricePoint[];
};

export type RecipeMaterial = {
  itemId: number;
  quantity: number;
};

export type EconomyRecipe = {
  recipeId: number;
  name: string;
  outputItemId: number;
  outputQuantity: number;
  fee: number;
  materials: RecipeMaterial[];
};

export type FarmLoot = {
  itemId: number;
  name: string;
  quantity: number;
  unitPrice: number | null;
  estimatedValue: number | null;
  marketServerId?: number | null;
  priceObservedAtMs?: number | null;
  priceSource?: string | null;
};

export type FarmSession = {
  sessionId: string;
  payloadId?: string;
  startedAt: string;
  endedAt: string;
  durationSeconds: number;
  directKina: number;
  lootValue: number;
  totalValue: number;
  kinaPerSecond: number;
  character?: string;
  location?: string;
  source: "local" | "server";
  loot: FarmLoot[];
};

export type DesktopSyncToken = {
  id: string;
  scope: "farm:write";
  label: string | null;
  createdAt: string;
  expiresAt: string;
  revokedAt: string | null;
};

export type EconomyServer = {
  region: string;
  serverId: number;
  name: string;
  /** Build-scoped localized names. Optional for old snapshots. */
  names?: LocalizedNames;
  faction: string;
  market: string;
  dimensionsConfirmed: boolean;
  dimensionSource: string | null;
  lastObservedAt: string | null;
  oldestActiveObservedAt?: string | null;
  newestActiveObservedAt?: string | null;
  coverageItems: number;
  activeItems: number;
  listings: number;
};

export type EconomySnapshot = {
  schemaVersion: "aion2.economy.snapshot/v1";
  revision?: string;
  /** Protected market shards advertised by the default snapshot index. */
  segments?: string[];
  generatedAt: string;
  build: number | null;
  stats: {
    catalogItems: number;
    activeItems: number;
    listings: number;
    servers: number;
  };
  items: EconomyItem[];
  catalog?: Array<
    Pick<
      EconomyItem,
      | "itemId"
      | "name"
      | "names"
      | "internalName"
      | "iconUrl"
      | "grade"
      | "tier"
      | "category"
      | "itemLevel"
    >
  >;
  recipes: EconomyRecipe[];
  servers: EconomyServer[];
  farmSessions?: FarmSession[];
  limitations: string[];
  marketRules?: {
    saleTaxRate: number | null;
    saleTaxSource: string | null;
    serverRates?: Array<{
      serverId: number;
      saleTaxRate: number;
      source: string;
    }>;
  };
};

export type UserRole = "guest" | "registered" | "approved" | "admin";

export type SessionState = {
  authenticated: boolean;
  role: UserRole;
  username: string | null;
  csrfToken: string | null;
};

export type AdminUser = {
  id: string;
  username: string;
  role: Exclude<UserRole, "guest">;
};
