import { useEffect, useMemo, useState } from "react";
import {
  Activity,
  Anvil,
  ArrowDownRight,
  ArrowUpRight,
  Boxes,
  ChevronLeft,
  ChevronRight,
  Database,
  Gem,
  History,
  LayoutDashboard,
  LogOut,
  Pickaxe,
  Search,
  Server,
  ShieldCheck,
  TrendingUp,
  Upload,
} from "lucide-react";

import { AuthGate } from "./components/AuthGate";
import { CopyForLlm } from "./components/CopyForLlm";
import { Sparkline } from "./components/Sparkline";
import {
  Alert,
  Badge,
  Button,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  Input,
} from "./components/ui";
import { AdminUsers } from "./features/admin/AdminUsers";
import { CrawlerConsole, createCrawlerApi } from "./features/crawler";
import { DesktopSyncTokens } from "./features/farm/DesktopSyncTokens";
import { economyApi } from "./lib/api";
import {
  bestCraftServerId,
  economySection,
  mergeFarmSessions,
  recipeEconomics,
  sectionRollups,
  selectMovers,
  type EconomySection,
  type MoverKind,
  validateSnapshot,
} from "./lib/economy";
import {
  farmActivityRollups,
  farmExportToView,
  parseFarmSessionExport,
  verifyFarmSessionExportPayloadId,
  type FarmSessionWithProvenance,
} from "./lib/farm";
import { marketFreshness, observationAge } from "./lib/freshness";
import {
  categoryName,
  factionName,
  itemHref,
  itemName,
  numberLocale,
  regionName,
  serverName,
  type AppLocale,
} from "./lib/i18n";
import {
  appHref,
  parseAppRoute,
  routeHref,
  type AppRoute,
  type View,
} from "./lib/routes";
import { groupEconomyServers } from "./lib/serverHierarchy";
import { cn, formatKina, formatPercent } from "./lib/utils";
import type {
  EconomyItem,
  EconomySnapshot,
  FarmSession,
  SessionState,
} from "./types";

const text = {
  ru: {
    nav: {
      overview: "Экономика",
      items: "Предметы",
      movers: "Движение рынка",
      servers: "Серверы",
      craft: "Крафт и ROI",
      farm: "Фарм-сессии",
      crawler: "Краулер",
      admin: "Администрирование",
    },
    item: "Предмет",
    server: "Сервер",
    current: "Текущая",
    median: "Медиана detail-лотов",
    medianUnavailable: "Нет detail-лотов",
    trend: "Тренд",
    priceTrend: "Динамика цены",
    marketDimensions: "Измерения Global-рынка",
    activity: "Log-активность лотов",
    details: "Подробнее",
    activityHint:
      "Логарифмически нормализованный показатель объёма текущих лотов: log(1 + лоты предмета) / log(1 + максимум лотов на выбранном рынке). Это не число продаж и не доказательство скорости продажи.",
    serverItemObservations: "Server-item наблюдения",
    activeItems: "Уникальные активные предметы",
    listings: "Текущие лоты",
    servers: "Рынки",
    snapshot: "Срез",
    catalog: "в каталоге",
    latestSummary: "последний полный срез",
    categories: "Категории экономики",
    categoriesHint:
      "Обзор в стиле poe.ninja: цена, изменение и активность текущих лотов.",
    category: "Категория",
    active: "Активных",
    medianPrice: "Медиана detail-цен",
    open: "Открыть",
    sections: {
      Materials: "Материалы",
      Refining: "Улучшение",
      Crafting: "Крафт",
      Gear: "Экипировка",
      Consumables: "Расходники",
      Other: "Другое / не классифицировано",
    },
    growth: "Рост за 24 часа",
    mostActive: "Самые активные рынки",
    allCategories: "Все категории",
    allSections: "Все разделы",
    search: "Предмет, internal name или item ID",
    itemsCount: "предметов",
    close: "К списку",
    history: "История наблюдений",
    time: "Время",
    price: "Цена",
    lots: "Лоты",
    noHistory: "История ещё не накоплена",
    noHistoryBody: "Для 1ч/24ч/7д нужны соседние наблюдения этого предмета.",
    currentMin: "Текущая / минимум",
    marketListings: "Текущие лоты",
    summaryRows: "строк в последнем срезе",
    itemNotFound: "Предмет не найден",
    itemNotFoundBody:
      "В текущем snapshot нет указанной пары server ID / item ID.",
    movers: {
      up: "Рост 24ч",
      down: "Падение 24ч",
      new: "Новые позиции",
      liquid: "Log-активность лотов",
      listings: "Больше всего лотов",
    },
    observedServer: "Наблюдаемый рынок",
    crossServer: "Межсерверный",
    serverMarket: "Рынок сервера",
    backendId: "Backend server ID",
    faction: "Фракция",
    unconfirmed: "Не подтверждена",
    logout: "Выйти",
    latestSnapshot: "Сборка snapshot",
    loading: "Загрузка…",
    incomplete: "Часть данных пока неполная",
    loadFailed: "Не удалось загрузить economy snapshot",
    observedMarket: "Наблюдаемый рынок AION2",
    fresh: "свежие",
    stale: "устаревшие",
    unknownAge: "возраст неизвестен",
    observedAt: "Наблюдалось",
    staleTitle: "Основная часть цен устарела",
    staleBody:
      "Цена считается устаревшей через 6 часов после её собственного наблюдения. Время сборки страницы не является временем наблюдения рынка.",
  },
  en: {
    nav: {
      overview: "Economy",
      items: "Items",
      movers: "Market movers",
      servers: "Servers",
      craft: "Craft & ROI",
      farm: "Farm sessions",
      crawler: "Crawler",
      admin: "Administration",
    },
    item: "Item",
    server: "Server",
    current: "Current",
    median: "Detail-listing median",
    medianUnavailable: "No detail listings",
    trend: "Trend",
    priceTrend: "Price trend",
    marketDimensions: "Global market dimensions",
    activity: "Log-scaled listing activity",
    details: "Details",
    activityHint:
      "Log-scaled current-listing volume proxy: log(1 + item listings) / log(1 + the market maximum). It is not completed sales or proof of sale speed.",
    serverItemObservations: "Server-item observations",
    activeItems: "Unique active items",
    listings: "Current listings",
    servers: "Markets",
    snapshot: "Snapshot",
    catalog: "in catalog",
    latestSummary: "latest complete summary",
    categories: "Economy categories",
    categoriesHint:
      "poe.ninja-style overview: price, change, and current listing activity.",
    category: "Category",
    active: "Active",
    medianPrice: "Detail-price median",
    open: "Open",
    sections: {
      Materials: "Materials",
      Refining: "Refining",
      Crafting: "Crafting",
      Gear: "Gear",
      Consumables: "Consumables",
      Other: "Other / unclassified",
    },
    growth: "24-hour gainers",
    mostActive: "Most active markets",
    allCategories: "All categories",
    allSections: "All sections",
    search: "Item, internal name, or item ID",
    itemsCount: "items",
    close: "Back to list",
    history: "Observed history",
    time: "Time",
    price: "Price",
    lots: "Listings",
    noHistory: "History is not available yet",
    noHistoryBody:
      "The 1h / 24h / 7d comparisons need adjacent observations for this item.",
    currentMin: "Current / minimum",
    marketListings: "Current listings",
    summaryRows: "rows in latest summary",
    itemNotFound: "Item not found",
    itemNotFoundBody:
      "The current snapshot does not contain this server ID / item ID pair.",
    movers: {
      up: "24h gainers",
      down: "24h losers",
      new: "New listings",
      liquid: "Log-scaled listing activity",
      listings: "Most listings",
    },
    observedServer: "Observed market",
    crossServer: "Cross-Server",
    serverMarket: "Server market",
    backendId: "Backend server ID",
    faction: "Faction",
    unconfirmed: "Unconfirmed",
    logout: "Log out",
    latestSnapshot: "Snapshot build time",
    loading: "Loading…",
    incomplete: "Some data is incomplete",
    loadFailed: "Could not load economy snapshot",
    observedMarket: "Observed AION2 market",
    fresh: "fresh",
    stale: "stale",
    unknownAge: "age unknown",
    observedAt: "Observed",
    staleTitle: "Most prices are stale",
    staleBody:
      "A price becomes stale six hours after its own observation. The page build time is not the market observation time.",
  },
} as const;

const nav: Array<{ id: View; icon: typeof LayoutDashboard }> = [
  { id: "overview", icon: LayoutDashboard },
  { id: "items", icon: Boxes },
  { id: "movers", icon: TrendingUp },
  { id: "servers", icon: Server },
  { id: "craft", icon: Anvil },
  { id: "farm", icon: Pickaxe },
  { id: "crawler", icon: Activity },
  { id: "admin", icon: ShieldCheck },
];

const emptySnapshot: EconomySnapshot = {
  schemaVersion: "aion2.economy.snapshot/v1",
  generatedAt: new Date(0).toISOString(),
  build: null,
  stats: { catalogItems: 0, activeItems: 0, listings: 0, servers: 0 },
  items: [],
  catalog: [],
  recipes: [],
  servers: [],
  farmSessions: [],
  limitations: [],
  marketRules: { saleTaxRate: null, saleTaxSource: null, serverRates: [] },
};

function useSnapshot(enabled: boolean) {
  const [snapshot, setSnapshot] = useState<EconomySnapshot>(emptySnapshot);
  const [itemsSnapshot, setItemsSnapshot] = useState<EconomySnapshot>(emptySnapshot);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (!enabled) return;
    economyApi
      .snapshot()
      .then(async (value) => {
        const current = validateSnapshot(value);
        const items = await economyApi.items(current.revision);
        setSnapshot(current);
        setItemsSnapshot({ ...current, items });
      })
      .catch((reason: unknown) =>
        setError(
          reason instanceof Error ? reason.message : "snapshot_load_failed",
        ),
      );
  }, [enabled]);
  return { snapshot, itemsSnapshot, error };
}

function Delta({ value }: { value: number | null }) {
  const positive = value != null && value > 0;
  const negative = value != null && value < 0;
  return (
    <span
      className={cn(
        "inline-flex items-center justify-end gap-1 font-medium",
        positive && "text-emerald-300",
        negative && "text-red-300",
        value == null && "text-muted-foreground",
      )}
    >
      {positive ? (
        <ArrowUpRight className="size-3.5" />
      ) : negative ? (
        <ArrowDownRight className="size-3.5" />
      ) : null}
      {formatPercent(value)}
    </span>
  );
}

function DetailMedian({
  value,
  locale,
}: {
  value: number | null;
  locale: AppLocale;
}) {
  if (value == null)
    return (
      <span
        className="text-xs text-muted-foreground"
        title={
          locale === "ru"
            ? "Summary даёт только минимальную цену. Для медианы нужны отдельные detail-лоты."
            : "The summary contains only a minimum price. Separate detail listings are required for a median."
        }
      >
        {text[locale].medianUnavailable}
      </span>
    );
  return <>{formatKina(value)}</>;
}

function Stat({
  label,
  value,
  hint,
}: {
  label: string;
  value: string;
  hint?: string;
}) {
  return (
    <Card>
      <CardContent className="pt-5">
        <div className="text-xs uppercase tracking-[0.12em] text-muted-foreground">
          {label}
        </div>
        <div className="mt-2 text-2xl font-semibold tabular-nums">{value}</div>
        {hint && (
          <div className="mt-1 text-xs text-muted-foreground">{hint}</div>
        )}
      </CardContent>
    </Card>
  );
}

function ItemIcon({
  item,
  size = "small",
}: {
  item: EconomyItem;
  size?: "small" | "large";
}) {
  const [failed, setFailed] = useState(false);
  const className =
    size === "large" ? "size-24 rounded-xl" : "size-11 rounded-lg";
  if (!item.iconUrl || failed)
    return (
      <div
        className={cn(
          className,
          "grid shrink-0 place-items-center border border-border bg-muted text-xs text-muted-foreground",
        )}
      >
        #{item.itemId}
      </div>
    );
  return (
    <img
      className={cn(
        className,
        "shrink-0 border border-border bg-muted object-contain",
      )}
      src={item.iconUrl}
      alt={item.name}
      loading="lazy"
      onError={() => setFailed(true)}
    />
  );
}

function ItemTable({
  items,
  locale,
  servers,
  limit,
}: {
  items: EconomyItem[];
  locale: AppLocale;
  servers: Map<number, string>;
  limit?: number;
}) {
  const t = text[locale];
  const rows = limit ? items.slice(0, limit) : items;
  return (
    <div className="overflow-auto rounded-lg border border-border">
      <table className="w-full min-w-260 border-collapse text-sm">
        <thead>
          <tr className="bg-muted/70 text-xs uppercase tracking-wide text-muted-foreground">
            <th className="p-3 text-left">{t.item}</th>
            <th className="p-3 text-right">{t.server}</th>
            <th className="p-3 text-right">{t.current}</th>
            <th className="p-3 text-right">{t.median}</th>
            <th className="p-3 text-right">1h</th>
            <th className="p-3 text-right">24h</th>
            <th className="p-3 text-right">7d</th>
            <th className="p-3 text-right">
              <span title={t.activityHint}>{t.activity}</span>
            </th>
            <th className="p-3 text-right">{t.trend}</th>
            <th className="p-3 text-right">{t.details}</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((item) => {
            const observedAt = item.lastObservedAt;
            const freshness = marketFreshness(observedAt);
            return (
              <tr
                key={`${item.serverId}:${item.itemId}`}
                className={cn(
                  "border-t border-border/70 hover:bg-muted/30",
                  freshness !== "fresh" && "bg-amber-950/20",
                )}
              >
                <td className="p-3">
                  <a
                    data-app-route
                    className="flex items-center gap-3 hover:text-emerald-300"
                    href={itemHref(item.serverId, item.itemId)}
                  >
                    <ItemIcon item={item} />
                    <span>
                      <span className="font-medium">
                        {itemName(item, locale)}
                      </span>
                      <span className="block text-xs text-muted-foreground">
                        #{item.itemId} · {categoryName(item.category, locale)} · ilvl {item.itemLevel}
                      </span>
                    </span>
                  </a>
                </td>
                <td className="p-3 text-right">
                  <div>
                    {servers.get(item.serverId) ??
                      `${t.observedServer} · ${item.serverId}`}
                  </div>
                  <div className="mt-1 flex items-center justify-end gap-2 text-xs text-muted-foreground">
                    <span>#{item.serverId}</span>
                    <Badge
                      tone={freshness === "fresh" ? "positive" : "warning"}
                    >
                      {freshness === "unknown"
                        ? t.unknownAge
                        : `${freshness === "stale" ? t.stale : t.fresh} · ${observationAge(observedAt, locale)}`}
                    </Badge>
                  </div>
                </td>
                <td className="p-3 text-right font-semibold text-amber-200">
                  {formatKina(item.current)}
                </td>
                <td className="p-3 text-right">
                  <DetailMedian value={item.median} locale={locale} />
                </td>
                <td className="p-3 text-right">
                  <Delta value={item.change1h} />
                </td>
                <td className="p-3 text-right">
                  <Delta value={item.change24h} />
                </td>
                <td className="p-3 text-right">
                  <Delta value={item.change7d} />
                </td>
                <td className="p-3 text-right">
                  <Badge
                    tone={
                      item.liquidityScore > 0.65
                        ? "positive"
                        : item.liquidityScore > 0.25
                          ? "warning"
                          : "neutral"
                    }
                  >
                    {Math.round(item.liquidityScore * 100)}%
                  </Badge>
                </td>
                <td className="p-3 text-right">
                  <Sparkline points={item.history} ariaLabel={t.priceTrend} />
                </td>
                <td className="p-3 text-right">
                  <a
                    data-app-route
                    className="inline-flex h-9 items-center rounded-md border border-border px-3 text-sm hover:bg-muted"
                    href={itemHref(item.serverId, item.itemId)}
                  >
                    {t.open}
                  </a>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function ItemDetail({
  item,
  locale,
  serverLabel,
  observedAt,
}: {
  item: EconomyItem;
  locale: AppLocale;
  serverLabel: string;
  observedAt: string | null;
}) {
  const t = text[locale];
  const freshness = marketFreshness(observedAt);
  return (
    <div className="space-y-4">
      <a
        data-app-route
        className="inline-flex h-9 items-center rounded-md border border-border px-3 text-sm hover:bg-muted"
        href="/items"
      >
        <ChevronLeft className="mr-1 size-4" />
        {t.close}
      </a>
      {freshness !== "fresh" && (
        <Alert
          title={t.staleTitle}
          action={
            <CopyForLlm
              message={t.staleBody}
              context={{
                severity: "warning",
                state: "market-item-stale",
                itemId: item.itemId,
                serverId: item.serverId,
                lastObservedAt: observedAt ?? "unknown",
              }}
            />
          }
        >
          {t.staleBody} {t.observedAt}:{" "}
          {observedAt
            ? new Date(observedAt).toLocaleString(numberLocale(locale))
            : t.unknownAge}
          .
        </Alert>
      )}
      <Card>
        <CardHeader>
          <div className="flex items-start gap-4">
            <ItemIcon item={item} size="large" />
            <div>
              <div className="text-xs uppercase tracking-[0.12em] text-muted-foreground">
                {serverLabel} · #{item.serverId}
              </div>
              <CardTitle className="mt-1 text-2xl">
                {itemName(item, locale)}
              </CardTitle>
              <div className="mt-1 text-xs text-muted-foreground">
                #{item.itemId} · {item.internalName}
                {item.grade ? ` · ${item.grade}` : ""}
                {item.tier != null
                  ? ` · ${String(item.tier).replace(/^t/i, "T")}`
                  : ""}
              </div>
              <div className="mt-2">
                <Badge tone={freshness === "fresh" ? "positive" : "warning"}>
                  {freshness === "unknown"
                    ? t.unknownAge
                    : `${freshness === "stale" ? t.stale : t.fresh} · ${observationAge(observedAt, locale)}`}
                </Badge>
              </div>
            </div>
          </div>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <Stat
              label={t.currentMin}
              value={`${formatKina(item.current)} / ${formatKina(item.minimum)}`}
            />
            <Stat
              label={t.median}
              value={
                item.median == null
                  ? t.medianUnavailable
                  : formatKina(item.median)
              }
            />
            <Stat
              label={t.marketListings}
              value={item.listingCount.toLocaleString(numberLocale(locale))}
              hint={`${item.saleCount.toLocaleString(numberLocale(locale))} ${t.summaryRows}`}
            />
            <Stat
              label={t.activity}
              value={`${Math.round(item.liquidityScore * 100)}%`}
              hint={t.activityHint}
            />
          </div>
          <div className="grid gap-3 sm:grid-cols-3">
            <Card>
              <CardContent className="pt-4">
                <div className="text-xs text-muted-foreground">1h</div>
                <div className="mt-1">
                  <Delta value={item.change1h} />
                </div>
              </CardContent>
            </Card>
            <Card>
              <CardContent className="pt-4">
                <div className="text-xs text-muted-foreground">24h</div>
                <div className="mt-1">
                  <Delta value={item.change24h} />
                </div>
              </CardContent>
            </Card>
            <Card>
              <CardContent className="pt-4">
                <div className="text-xs text-muted-foreground">7d</div>
                <div className="mt-1">
                  <Delta value={item.change7d} />
                </div>
              </CardContent>
            </Card>
          </div>
          <div>
            <div className="mb-2 text-sm font-medium">{t.history}</div>
            {item.history.length ? (
              <div className="max-h-64 overflow-auto rounded-md border border-border">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="bg-muted/70 text-xs uppercase text-muted-foreground">
                      <th className="p-2 text-left">{t.time}</th>
                      <th className="p-2 text-right">{t.price}</th>
                      <th className="p-2 text-right">{t.lots}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {[...item.history].reverse().map((point) => (
                      <tr
                        key={`${point.at}:${point.value}:${point.listings}`}
                        className="border-t border-border"
                      >
                        <td className="p-2">
                          {new Date(point.at).toLocaleString(
                            numberLocale(locale),
                          )}
                        </td>
                        <td className="p-2 text-right">
                          {formatKina(point.value)}
                        </td>
                        <td className="p-2 text-right">{point.listings}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <Alert
                title={t.noHistory}
                action={
                  <CopyForLlm
                    message={t.noHistoryBody}
                    context={{
                      severity: "warning",
                      state: "market-item-history-empty",
                      itemId: item.itemId,
                      serverId: item.serverId,
                    }}
                  />
                }
              >
                {t.noHistoryBody}
              </Alert>
            )}
          </div>
        </CardContent>
      </Card>
    </div>
  );
}

function serverLabels(snapshot: EconomySnapshot, locale: AppLocale) {
  return new Map(
    snapshot.servers.map((server) => [
      server.serverId,
      serverName(server, locale),
    ]),
  );
}

function Overview({
  snapshot,
  locale,
}: {
  snapshot: EconomySnapshot;
  locale: AppLocale;
}) {
  const t = text[locale];
  const up = selectMovers(snapshot.items, "up", 5);
  const liquid = selectMovers(snapshot.items, "liquid", 5);
  const sections = sectionRollups(snapshot.items);
  const servers = serverLabels(snapshot, locale);
  const activeItems = snapshot.items.filter((item) => item.saleCount > 0);
  const uniqueActiveItems = new Set(activeItems.map((item) => item.itemId)).size;
  const freshItems = activeItems.filter(
    (item) => marketFreshness(item.lastObservedAt) === "fresh",
  );
  const staleItems = activeItems.filter(
    (item) => marketFreshness(item.lastObservedAt) !== "fresh",
  );
  const staleServers = snapshot.servers.filter((server) =>
    snapshot.items.some(
      (item) =>
        item.serverId === server.serverId &&
        item.saleCount > 0 &&
        marketFreshness(item.lastObservedAt) !== "fresh",
    ),
  );
  const freshnessHint = `${t.fresh}: ${freshItems.length.toLocaleString(numberLocale(locale))} · ${t.stale}: ${staleItems.length.toLocaleString(numberLocale(locale))}`;
  const listingHint = `${t.fresh}: ${freshItems.reduce((sum, item) => sum + item.saleCount, 0).toLocaleString(numberLocale(locale))} · ${t.stale}: ${staleItems.reduce((sum, item) => sum + item.saleCount, 0).toLocaleString(numberLocale(locale))}`;
  return (
    <div className="space-y-6">
      {staleItems.length > 0 && (
        <Alert
          title={t.staleTitle}
          action={
            <CopyForLlm
              message={t.staleBody}
              context={{
                severity: "warning",
                state: "market-snapshot-stale",
                staleServerIds: staleServers
                  .map((server) => server.serverId)
                  .join(","),
                staleItems: staleItems.length,
                generatedAt: snapshot.generatedAt,
              }}
            />
          }
        >
          {t.staleBody} {t.stale}:{" "}
          {staleItems.length.toLocaleString(numberLocale(locale))} /{" "}
          {activeItems.length.toLocaleString(numberLocale(locale))}.
        </Alert>
      )}
      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-5">
        <Stat
          label={t.serverItemObservations}
          value={snapshot.stats.activeItems.toLocaleString(numberLocale(locale))}
          hint={`${t.latestSummary} · ${freshnessHint}`}
        />
        <Stat
          label={t.activeItems}
          value={uniqueActiveItems.toLocaleString(numberLocale(locale))}
          hint={`${snapshot.stats.catalogItems.toLocaleString(numberLocale(locale))} ${t.catalog}`}
        />
        <Stat
          label={t.listings}
          value={snapshot.stats.listings.toLocaleString(numberLocale(locale))}
          hint={listingHint}
        />
        <Stat
          label={t.servers}
          value={String(snapshot.stats.servers)}
          hint={t.marketDimensions}
        />
        <Stat
          label={t.snapshot}
          value={
            snapshot.generatedAt === new Date(0).toISOString()
              ? "—"
              : new Date(snapshot.generatedAt).toLocaleString(
                  numberLocale(locale),
                )
          }
          hint={snapshot.build ? `Build ${snapshot.build}` : "Build —"}
        />
      </div>
      <Card>
        <CardHeader>
          <CardTitle>{t.categories}</CardTitle>
          <p className="text-sm text-muted-foreground">{t.categoriesHint}</p>
        </CardHeader>
        <CardContent>
          <div className="overflow-auto">
            <table className="w-full min-w-200 text-sm">
              <thead>
                <tr className="text-xs uppercase text-muted-foreground">
                  <th className="p-3 text-left">{t.category}</th>
                  <th className="p-3 text-right">{t.active}</th>
                  <th className="p-3 text-right">{t.medianPrice}</th>
                  <th className="p-3 text-right">24h</th>
                  <th className="p-3 text-right">
                    <span title={t.activityHint}>{t.activity}</span>
                  </th>
                  <th className="p-3 text-right">{t.listings}</th>
                  <th className="p-3 text-right" />
                </tr>
              </thead>
              <tbody>
                {sections.map((section) => (
                  <tr
                    key={section.section}
                    className="border-t border-border hover:bg-muted/30"
                  >
                    <td className="p-3 font-medium">
                      {t.sections[section.section]}
                    </td>
                    <td className="p-3 text-right">
                      {section.activeItems.toLocaleString(numberLocale(locale))}
                    </td>
                    <td className="p-3 text-right font-semibold text-amber-200">
                      <DetailMedian
                        value={section.medianPrice}
                        locale={locale}
                      />
                    </td>
                    <td className="p-3 text-right">
                      <Delta value={section.change24h} />
                    </td>
                    <td className="p-3 text-right">
                      <Badge
                        tone={
                          section.liquidityScore > 0.65
                            ? "positive"
                            : section.liquidityScore > 0.25
                              ? "warning"
                              : "neutral"
                        }
                      >
                        {Math.round(section.liquidityScore * 100)}%
                      </Badge>
                    </td>
                    <td className="p-3 text-right">
                      {section.saleCount.toLocaleString(numberLocale(locale))}
                    </td>
                    <td className="p-3 text-right">
                      <a
                        data-app-route
                        className="text-emerald-300 hover:underline"
                        href={appHref("items", section.section)}
                      >
                        {t.open}
                      </a>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </CardContent>
      </Card>
      <div className="grid gap-5 xl:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <ArrowUpRight className="size-4 text-emerald-300" />
              {t.growth}
            </CardTitle>
          </CardHeader>
          <CardContent>
            <ItemTable items={up} locale={locale} servers={servers} />
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Gem className="size-4 text-sky-300" />
              {t.mostActive}
            </CardTitle>
          </CardHeader>
          <CardContent>
            <ItemTable items={liquid} locale={locale} servers={servers} />
          </CardContent>
        </Card>
      </div>
    </div>
  );
}

function Items({
  snapshot,
  locale,
  initialSection,
  initialServerId,
  initialRegion,
  initialFaction,
}: {
  snapshot: EconomySnapshot;
  locale: AppLocale;
  initialSection?: EconomySection;
  initialServerId?: number;
  initialRegion?: string;
  initialFaction?: string;
}) {
  const t = text[locale];
  const [query, setQuery] = useState("");
  const [category, setCategory] = useState("all");
  const [section, setSection] = useState<EconomySection | "all">(
    initialSection ?? "all",
  );
  const [serverId, setServerId] = useState<number | null>(
    initialServerId ?? null,
  );
  const [region, setRegion] = useState(initialRegion ?? "all");
  const [faction, setFaction] = useState(initialFaction ?? "all");
  const [page, setPage] = useState(0);
  useEffect(() => setSection(initialSection ?? "all"), [initialSection]);
  useEffect(() => setServerId(initialServerId ?? null), [initialServerId]);
  useEffect(() => setRegion(initialRegion ?? "all"), [initialRegion]);
  useEffect(() => setFaction(initialFaction ?? "all"), [initialFaction]);
  const categories = useMemo(
    () => ["all", ...new Set(snapshot.items.map((item) => item.category))],
    [snapshot.items],
  );
  const serverById = useMemo(
    () => new Map(snapshot.servers.map((server) => [server.serverId, server])),
    [snapshot.servers],
  );
  const serverOptions = useMemo(
    () =>
      [...snapshot.servers].sort((a, b) =>
        serverName(a, locale).localeCompare(serverName(b, locale)),
      ),
    [snapshot.servers, locale],
  );
  const regions = useMemo(
    () => [...new Set(snapshot.servers.map((server) => server.region))].sort(),
    [snapshot.servers],
  );
  const factions = useMemo(
    () => [...new Set(snapshot.servers.map((server) => server.faction))].sort(),
    [snapshot.servers],
  );
  const updateItemsUrl = (
    nextSection: EconomySection | "all",
    nextServerId: number | null,
    nextRegion: string,
    nextFaction: string,
  ) => {
    window.history.pushState(
      null,
      "",
      appHref("items", nextSection === "all" ? undefined : nextSection, {
        serverId: nextServerId ?? undefined,
        region: nextRegion === "all" ? undefined : nextRegion,
        faction: nextFaction === "all" ? undefined : nextFaction,
      }),
    );
  };
  const filtered = useMemo(
    () =>
      snapshot.items
        .filter((item) => {
          const needle = query.trim().toLowerCase();
          const names = `${item.name} ${item.names?.en ?? ""} ${item.names?.ru ?? ""} ${item.names?.["en-US"] ?? ""} ${item.names?.["ru-RU"] ?? ""}`;
          const itemServer = serverById.get(item.serverId);
          return (
            (category === "all" || item.category === category) &&
            (section === "all" || economySection(item) === section) &&
            (serverId === null || item.serverId === serverId) &&
            (region === "all" || itemServer?.region === region) &&
            (faction === "all" || itemServer?.faction === faction) &&
            (!needle ||
              `${item.itemId} ${names} ${item.internalName}`
                .toLowerCase()
                .includes(needle))
          );
        })
        .sort((a, b) => (a.current ?? Infinity) - (b.current ?? Infinity)),
    [
      snapshot.items,
      query,
      category,
      section,
      serverId,
      region,
      faction,
      serverById,
    ],
  );
  const size = 100;
  const pages = Math.max(1, Math.ceil(filtered.length / size));
  const currentPage = Math.min(page, pages - 1);
  const servers = serverLabels(snapshot, locale);
  return (
    <div className="space-y-4">
      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-[1fr_190px_190px_190px_190px]">
        <label className="relative">
          <Search className="absolute left-3 top-2.5 size-4 text-muted-foreground" />
          <Input
            className="pl-9"
            value={query}
            onChange={(event) => {
              setQuery(event.target.value);
              setPage(0);
            }}
            placeholder={t.search}
          />
        </label>
        <select
          className="h-9 rounded-md border border-border bg-background px-3 text-sm"
          value={section}
          onChange={(event) => {
            const next = event.target.value as EconomySection | "all";
            setSection(next);
            setPage(0);
            updateItemsUrl(next, serverId, region, faction);
          }}
        >
          <option value="all">{t.allSections}</option>
          {(Object.keys(t.sections) as EconomySection[]).map((value) => (
            <option key={value} value={value}>
              {t.sections[value]}
            </option>
          ))}
        </select>
        <select
          className="h-9 rounded-md border border-border bg-background px-3 text-sm"
          value={category}
          onChange={(event) => {
            setCategory(event.target.value);
            setPage(0);
          }}
        >
          {categories.map((value) => (
            <option key={value} value={value}>
              {value === "all" ? t.allCategories : categoryName(value, locale)}
            </option>
          ))}
        </select>
        <select
          className="h-9 rounded-md border border-border bg-background px-3 text-sm"
          value={serverId ?? "all"}
          onChange={(event) => {
            const next = event.target.value === "all" ? null : Number(event.target.value);
            setServerId(next);
            setPage(0);
            updateItemsUrl(section, next, region, faction);
          }}
        >
          <option value="all">
            {locale === "ru" ? "Все серверы" : "All servers"}
          </option>
          {serverOptions.map((server) => (
            <option key={server.serverId} value={server.serverId}>
              {serverName(server, locale)} · {factionName(server.faction, locale)}
            </option>
          ))}
        </select>
        <select
          className="h-9 rounded-md border border-border bg-background px-3 text-sm"
          value={region}
          onChange={(event) => {
            const next = event.target.value;
            setRegion(next);
            setPage(0);
            updateItemsUrl(section, serverId, next, faction);
          }}
        >
          <option value="all">{locale === "ru" ? "Все регионы" : "All regions"}</option>
          {regions.map((value) => (
            <option key={value} value={value}>{regionName(value, locale)}</option>
          ))}
        </select>
        <select
          className="h-9 rounded-md border border-border bg-background px-3 text-sm"
          value={faction}
          onChange={(event) => {
            const next = event.target.value;
            setFaction(next);
            setPage(0);
            updateItemsUrl(section, serverId, region, next);
          }}
        >
          <option value="all">{locale === "ru" ? "Все фракции" : "All factions"}</option>
          {factions.map((value) => (
            <option key={value} value={value}>{factionName(value, locale)}</option>
          ))}
        </select>
      </div>
      <ItemTable
        items={filtered.slice(currentPage * size, (currentPage + 1) * size)}
        locale={locale}
        servers={servers}
      />
      <div className="flex items-center justify-end gap-3 text-sm text-muted-foreground">
        <span>
          {filtered.length.toLocaleString(numberLocale(locale))} {t.itemsCount}{" "}
          · {currentPage + 1}/{pages}
        </span>
        <Button
          variant="outline"
          disabled={currentPage === 0}
          onClick={() => setPage(currentPage - 1)}
        >
          <ChevronLeft className="size-4" />
        </Button>
        <Button
          variant="outline"
          disabled={currentPage + 1 >= pages}
          onClick={() => setPage(currentPage + 1)}
        >
          <ChevronRight className="size-4" />
        </Button>
      </div>
    </div>
  );
}

function Movers({
  snapshot,
  locale,
}: {
  snapshot: EconomySnapshot;
  locale: AppLocale;
}) {
  const [kind, setKind] = useState<MoverKind>("up");
  const labels = text[locale].movers;
  const rows = selectMovers(snapshot.items, kind, 100);
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap gap-2">
        {(Object.keys(labels) as MoverKind[]).map((value) => (
          <Button
            key={value}
            variant={kind === value ? "default" : "outline"}
            onClick={() => setKind(value)}
          >
            {labels[value]}
          </Button>
        ))}
      </div>
      {!rows.length && (kind === "up" || kind === "down") && (
        <Alert
          title={
            locale === "ru"
              ? "24-часовая динамика пока не вычисляется"
              : "24-hour change is not available yet"
          }
          action={
            <CopyForLlm
              message="No items have a confirmed 24-hour comparison window in the current market snapshot."
              context={{
                severity: "warning",
                state: "mover-horizon-unavailable",
                horizon: "24h",
                build: snapshot.build,
              }}
            />
          }
        >
          {locale === "ru"
            ? "Нужны соседние наблюдения с интервалом не меньше 24 часов; строки не сортируются по выдуманному нулю."
            : "Adjacent observations at least 24 hours apart are required; rows are not sorted by an invented zero."}
        </Alert>
      )}
      {!rows.length && kind === "new" && (
        <Alert
          title={locale === "ru" ? "Новых позиций нет" : "No new listings"}
          action={
            <CopyForLlm
              message="No item made a confirmed zero-to-positive transition between the two latest complete market summaries."
              context={{
                severity: "warning",
                state: "newly-liquid-empty",
                build: snapshot.build,
              }}
            />
          }
        >
          {locale === "ru"
            ? "Новая позиция требует подтверждённого перехода от нуля к ненулевому числу лотов."
            : "A new listing requires a confirmed zero-to-positive transition between complete summaries."}
        </Alert>
      )}
      <ItemTable
        items={rows}
        locale={locale}
        servers={serverLabels(snapshot, locale)}
      />
    </div>
  );
}

function Servers({
  snapshot,
  locale,
}: {
  snapshot: EconomySnapshot;
  locale: AppLocale;
}) {
  const t = text[locale];
  const observed = snapshot.servers;
  const groups = groupEconomyServers(observed);
  const dimensionsUnconfirmed = groups.some(
    (group) => !group.dimensionsConfirmed,
  );
  const marketCard = (server: EconomySnapshot["servers"][number]) => {
    const activeServerItems = snapshot.items.filter(
      (item) => item.serverId === server.serverId && item.saleCount > 0,
    );
    const freshCount = activeServerItems.filter(
      (item) => marketFreshness(item.lastObservedAt) === "fresh",
    ).length;
    const staleCount = activeServerItems.length - freshCount;
    const freshness =
      staleCount > 0 ? "stale" : freshCount > 0 ? "fresh" : "unknown";
    return (
      <Card
        key={`${server.region}:${server.serverId}:${server.market}:${server.faction}`}
      >
        <CardHeader>
          <CardTitle className="flex items-center justify-between gap-3">
            <a
              data-app-route
              className="hover:text-emerald-300 hover:underline"
              href={appHref("items", undefined, { serverId: server.serverId })}
            >
              {serverName(server, locale)}
            </a>
            <div className="flex flex-wrap justify-end gap-2">
              <Badge
                tone={
                  server.dimensionsConfirmed && server.market === "cross-server"
                    ? "positive"
                    : "neutral"
                }
              >
                {!server.dimensionsConfirmed
                  ? t.unconfirmed
                  : server.market === "cross-server"
                    ? t.crossServer
                    : t.serverMarket}
              </Badge>
              <Badge tone={freshness === "fresh" ? "positive" : "warning"}>
                {freshness === "unknown"
                  ? t.unknownAge
                  : `${t.fresh}: ${freshCount} · ${t.stale}: ${staleCount}`}
              </Badge>
            </div>
          </CardTitle>
        </CardHeader>
        <CardContent className="grid grid-cols-2 gap-4 text-sm">
          <div>
            <span className="text-muted-foreground">{t.backendId}</span>
            <div className="mt-1 font-medium">{server.serverId}</div>
          </div>
          <div>
            <span className="text-muted-foreground">{t.faction}</span>
            <div className="mt-1 font-medium">
              {server.dimensionsConfirmed
                ? factionName(server.faction, locale)
                : t.unconfirmed}
            </div>
          </div>
          <div>
            <span className="text-muted-foreground">{t.activeItems}</span>
            <div className="mt-1 text-xl font-semibold">
              {server.activeItems}
            </div>
          </div>
          <div>
            <span className="text-muted-foreground">{t.listings}</span>
            <div className="mt-1 text-xl font-semibold">{server.listings}</div>
          </div>
          <div className="col-span-2">
            <span className="text-muted-foreground">{t.observedAt}</span>
            <div className="mt-1 font-medium">
              {server.oldestActiveObservedAt && server.newestActiveObservedAt
                ? `${new Date(server.oldestActiveObservedAt).toLocaleString(numberLocale(locale))} — ${new Date(server.newestActiveObservedAt).toLocaleString(numberLocale(locale))}`
                : server.lastObservedAt
                  ? new Date(server.lastObservedAt).toLocaleString(
                      numberLocale(locale),
                    )
                  : t.unknownAge}
            </div>
          </div>
        </CardContent>
      </Card>
    );
  };
  return (
    <div className="space-y-4">
      {dimensionsUnconfirmed && (
        <Alert
          title={
            locale === "ru"
              ? "Часть параметров серверов не подтверждена"
              : "Some server dimensions are unconfirmed"
          }
          action={
            <CopyForLlm
              message="Economy server hierarchy keeps backend IDs without build-scoped evidence in an explicit unconfirmed group; no EU/NA, faction or Cross-Server label was inferred."
              context={{
                severity: "warning",
                state: "server-dimensions-unconfirmed",
                observedServerIds: observed
                  .filter((server) => !server.dimensionsConfirmed)
                  .map((server) => server.serverId)
                  .join(","),
              }}
            />
          }
        >
          {locale === "ru"
            ? "Для неподтверждённых полей показан только наблюдаемый backend ID."
            : "Only the observed backend ID is shown for unconfirmed dimensions."}
        </Alert>
      )}
      {groups.map((group) => (
        <section key={group.region} className="space-y-3">
          <div className="flex items-center gap-3">
            <h2 className="text-lg font-semibold">
              {group.dimensionsConfirmed
                ? regionName(group.region, locale)
                : t.unconfirmed}
            </h2>
            <Badge tone={group.dimensionsConfirmed ? "positive" : "warning"}>
              {group.dimensionsConfirmed
                ? locale === "ru"
                  ? "подтверждено"
                  : "evidenced"
                : locale === "ru"
                  ? "только backend ID"
                  : "backend IDs only"}
            </Badge>
          </div>
          {group.serverMarkets.length > 0 && (
            <div className="space-y-2">
              <div className="text-xs uppercase tracking-[0.12em] text-muted-foreground">
                {group.dimensionsConfirmed ? t.serverMarket : t.observedServer}
              </div>
              <div className="grid gap-4 lg:grid-cols-2">
                {group.serverMarkets.map(marketCard)}
              </div>
            </div>
          )}
          {group.crossServerMarkets.length > 0 && (
            <div className="space-y-2">
              <div className="text-xs uppercase tracking-[0.12em] text-emerald-300">
                {group.dimensionsConfirmed ? t.crossServer : t.observedServer}
              </div>
              <div className="grid gap-4 lg:grid-cols-2">
                {group.crossServerMarkets.map(marketCard)}
              </div>
            </div>
          )}
        </section>
      ))}
    </div>
  );
}

type CraftMaterialView = EconomySnapshot["recipes"][number]["materials"][number] & {
  name?: string;
  names?: Record<string, string | undefined>;
};

type CraftRecipeView = Omit<
  EconomySnapshot["recipes"][number],
  "materials"
> & {
  names?: Record<string, string | undefined>;
  category?: string;
  subcategory?: string;
  subtab?: string;
  outputName?: string;
  outputNames?: Record<string, string | undefined>;
  materials: CraftMaterialView[];
};

export function craftLocalizedName(
  value: { name?: string; names?: Record<string, string | undefined> },
  locale: AppLocale,
  fallback: string,
) {
  return value.names?.[locale] ?? value.names?.en ?? value.name ?? fallback;
}

export function craftScopeLabel(
  server: EconomySnapshot["servers"][number] | undefined,
  serverId: number,
  locale: AppLocale,
) {
  if (!server)
    return `${locale === "ru" ? "Регион не подтверждён" : "Unconfirmed region"} · ${locale === "ru" ? "Рынок" : "Market"} ${serverId} · ${locale === "ru" ? "Фракция не подтверждена" : "Unconfirmed faction"} · #${serverId}`;
  return `${regionName(server.region, locale)} · ${serverName(server, locale)} · ${factionName(server.faction, locale)} · #${serverId}`;
}

const craftDimensionNamesRu: Record<string, string> = {
  Alchemy: "Алхимия",
  Blacksmithing: "Кузнечное дело",
  Cooking: "Кулинария",
  Jewelcrafting: "Ювелирное дело",
  Tailoring: "Шитьё",
  Accessory: "Украшения",
  Weapon: "Оружие",
  Misc: "Разное",
  Earring: "Серьги",
  Necklace: "Ожерелья",
  Ring: "Кольца",
  Bracelet: "Браслеты",
};

export function craftDimensionName(value: string, locale: AppLocale) {
  return locale === "ru" ? (craftDimensionNamesRu[value] ?? value) : value;
}

export function Craft({
  snapshot,
  locale,
}: {
  snapshot: EconomySnapshot;
  locale: AppLocale;
}) {
  const recipes = snapshot.recipes as CraftRecipeView[];
  const serverIds = useMemo(
    () =>
      [...new Set(snapshot.items.map((item) => item.serverId))].sort(
        (a, b) => a - b,
      ),
    [snapshot.items],
  );
  const recommendedServerId = useMemo(
    () => bestCraftServerId(snapshot.recipes, snapshot.items),
    [snapshot.recipes, snapshot.items],
  );
  const [serverId, setServerId] = useState<number | null>(() => {
    const stored = Number(localStorage.getItem("aion2.craft-server/v1"));
    return Number.isSafeInteger(stored) && serverIds.includes(stored)
      ? stored
      : recommendedServerId;
  });
  const [query, setQuery] = useState("");
  const [showIncomplete, setShowIncomplete] = useState(false);
  const [categoryFilter, setCategoryFilter] = useState("Jewelcrafting");
  const [subcategoryFilter, setSubcategoryFilter] = useState("Accessory");
  const [subtabFilter, setSubtabFilter] = useState("all");
  const effectiveServerId =
    serverId != null && serverIds.includes(serverId)
      ? serverId
      : recommendedServerId;
  const pricedItems = useMemo(
    () =>
      snapshot.items.filter(
        (item) => item.serverId === effectiveServerId && item.current != null,
      ),
    [snapshot.items, effectiveServerId],
  );
  const priceByItem = useMemo(
    () =>
      new Map(
        pricedItems.flatMap((item) =>
          item.current == null ? [] : [[item.itemId, item.current] as const],
        ),
      ),
    [pricedItems],
  );
  if (!recipes.length)
    return (
      <Alert
        title={
          locale === "ru"
            ? "Рецепты ещё не опубликованы"
            : "Craft recipes are not published yet"
        }
        action={
          <CopyForLlm
            message="Craft economics недоступна: decoder CraftRecipe.dat ещё не дал проверенную семантику."
            context={{
              severity: "warning",
              state: "craft-recipes-unavailable",
              build: snapshot.build,
              activeItems: snapshot.stats.activeItems,
            }}
          />
        }
      >
        {locale === "ru"
          ? "UI и формулы готовы, но показывать выдуманные материалы и количество результата нельзя. Нужен подтверждённый parser для CraftRecipe.dat."
          : "The UI and formulas are ready, but invented materials and output quantities cannot be shown. A verified CraftRecipe.dat parser is required."}
      </Alert>
    );
  const serverTax = snapshot.marketRules?.serverRates?.find(
    (row) => row.serverId === effectiveServerId,
  );
  const taxRate =
    serverTax?.saleTaxRate ?? snapshot.marketRules?.saleTaxRate ?? null;
  const categories = [...new Set(recipes.map((recipe) => recipe.category).filter(Boolean))]
    .sort() as string[];
  const effectiveCategory = categories.includes(categoryFilter)
    ? categoryFilter
    : "all";
  const subcategories = [
    ...new Set(
      recipes
        .filter(
          (recipe) =>
            effectiveCategory === "all" || recipe.category === effectiveCategory,
        )
        .map((recipe) => recipe.subcategory)
        .filter(Boolean),
    ),
  ].sort() as string[];
  const effectiveSubcategory = subcategories.includes(subcategoryFilter)
    ? subcategoryFilter
    : "all";
  const subtabs = [
    ...new Set(
      recipes
        .filter(
          (recipe) =>
            (effectiveCategory === "all" || recipe.category === effectiveCategory) &&
            (effectiveSubcategory === "all" ||
              recipe.subcategory === effectiveSubcategory),
        )
        .map((recipe) => recipe.subtab)
        .filter((value): value is string => Boolean(value) && value !== "None"),
    ),
  ].sort();
  const effectiveSubtab = subtabs.includes(subtabFilter) ? subtabFilter : "all";
  const allRows = recipes
    .map((recipe) => ({
      recipe,
      result: recipeEconomics(recipe, priceByItem, taxRate),
    }))
    .sort(
      (a, b) =>
        (b.result.buyVsCraftSavings ?? -Infinity) -
        (a.result.buyVsCraftSavings ?? -Infinity),
    );
  const completeRows = allRows.filter((row) => row.result.complete);
  const needle = query.trim().toLowerCase();
  const rows = allRows
    .filter(
      ({ recipe, result }) =>
        (showIncomplete || result.complete) &&
        (effectiveCategory === "all" || recipe.category === effectiveCategory) &&
        (effectiveSubcategory === "all" ||
          recipe.subcategory === effectiveSubcategory) &&
        (effectiveSubtab === "all" || recipe.subtab === effectiveSubtab) &&
        (!needle ||
          [
            recipe.recipeId,
            recipe.name,
            recipe.outputItemId,
            recipe.outputName,
            recipe.outputNames?.en,
            recipe.outputNames?.ru,
            recipe.category,
            recipe.subcategory,
            recipe.subtab,
            ...recipe.materials.flatMap((material) => [
              material.itemId,
              material.name,
              material.names?.en,
              material.names?.ru,
            ]),
          ]
            .join(" ")
            .toLowerCase()
            .includes(needle)),
    )
    .slice(0, 100);
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <label className="text-sm text-muted-foreground">
          {locale === "ru" ? "Scope рынка" : "Market scope"}
        </label>
        <select
          className="h-9 rounded-md border border-border bg-background px-3 text-sm"
          value={effectiveServerId ?? ""}
          onChange={(event) => {
            const nextServerId = Number(event.target.value);
            setServerId(nextServerId);
            localStorage.setItem("aion2.craft-server/v1", String(nextServerId));
          }}
        >
          {serverIds.map((value) => (
            <option key={value} value={value}>
              {craftScopeLabel(
                snapshot.servers.find((server) => server.serverId === value),
                value,
                locale,
              )}
            </option>
          ))}
        </select>
      </div>
      {pricedItems.some(
        (item) => marketFreshness(item.lastObservedAt) !== "fresh",
      ) && (
        <Alert
          title={
            locale === "ru"
              ? "В расчётах есть устаревшие цены"
              : "Some prices in these calculations are stale"
          }
          action={
            <CopyForLlm
              message="Craft economics uses one or more stale or undated market quotes."
              context={{
                severity: "warning",
                state: "craft-prices-stale",
                serverId: effectiveServerId,
                stalePrices: pricedItems.filter(
                  (item) => marketFreshness(item.lastObservedAt) !== "fresh",
                ).length,
                pricedItems: pricedItems.length,
              }}
            />
          }
        >
          {locale === "ru"
            ? "Результаты buy-vs-craft считаются по последнему сохранённому срезу и могут не отражать текущий рынок."
            : "Buy-versus-craft results use the last stored snapshot and may not reflect the current market."}{" "}
          {pricedItems.length > 0
            ? (locale === "ru" ? "Новейшая цена: " : "Newest quote: ") +
              observationAge(
                pricedItems
                  .map((item) => item.lastObservedAt)
                  .filter((value): value is string => Boolean(value))
                  .sort()
                  .at(-1) ?? null,
                locale,
              ) +
              "."
            : locale === "ru"
              ? "Датированных цен нет."
              : "No dated prices are available."}
        </Alert>
      )}
      <div className="grid gap-3 md:grid-cols-3">
        <label className="space-y-1 text-sm">
          <span className="text-muted-foreground">
            {locale === "ru" ? "Профессия" : "Profession"}
          </span>
          <select
            className="h-9 w-full rounded-md border border-border bg-background px-3"
            value={effectiveCategory}
            onChange={(event) => {
              const value = event.target.value;
              setCategoryFilter(value);
              const categorySubcategories = recipes
                .filter((recipe) => value === "all" || recipe.category === value)
                .map((recipe) => recipe.subcategory);
              setSubcategoryFilter(
                categorySubcategories.includes("Accessory") ? "Accessory" : "all",
              );
              setSubtabFilter("all");
            }}
          >
            <option value="all">{locale === "ru" ? "Все профессии" : "All professions"}</option>
            {categories.map((value) => (
              <option key={value} value={value}>{craftDimensionName(value, locale)}</option>
            ))}
          </select>
        </label>
        <label className="space-y-1 text-sm">
          <span className="text-muted-foreground">
            {locale === "ru" ? "Категория" : "Category"}
          </span>
          <select
            className="h-9 w-full rounded-md border border-border bg-background px-3"
            value={effectiveSubcategory}
            onChange={(event) => {
              setSubcategoryFilter(event.target.value);
              setSubtabFilter("all");
            }}
          >
            <option value="all">{locale === "ru" ? "Все категории" : "All categories"}</option>
            {subcategories.map((value) => (
              <option key={value} value={value}>{craftDimensionName(value, locale)}</option>
            ))}
          </select>
        </label>
        <label className="space-y-1 text-sm">
          <span className="text-muted-foreground">
            {locale === "ru" ? "Тип украшения" : "Accessory type"}
          </span>
          <select
            className="h-9 w-full rounded-md border border-border bg-background px-3"
            value={effectiveSubtab}
            onChange={(event) => setSubtabFilter(event.target.value)}
          >
            <option value="all">{locale === "ru" ? "Все типы" : "All types"}</option>
            {subtabs.map((value) => (
              <option key={value} value={value}>{craftDimensionName(value, locale)}</option>
            ))}
          </select>
        </label>
      </div>
      <div className="grid gap-3 lg:grid-cols-[1fr_auto]">
        <Input
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder={
            locale === "ru" ? "Поиск по рецепту или ID" : "Search recipe or ID"
          }
        />
        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={showIncomplete}
            onChange={(event) => setShowIncomplete(event.target.checked)}
          />
          <span>
            {locale === "ru"
              ? "Показывать без полной цены"
              : "Show rows with missing prices"}
          </span>
        </label>
      </div>
      <div className="text-sm text-muted-foreground">
        {locale === "ru" ? "Полностью рассчитано" : "Fully priced"}:{" "}
        {completeRows.length.toLocaleString(numberLocale(locale))} /{" "}
        {allRows.length.toLocaleString(numberLocale(locale))}.{" "}
        {rows.length === 100
          ? locale === "ru"
            ? "Показаны первые 100 строк; уточните поиск."
            : "Showing the first 100 rows; narrow the search."
          : ""}
      </div>
      {taxRate == null && (
        <Alert
          title={
            locale === "ru"
              ? "Налог продажи не подтверждён"
              : "Sale tax is not confirmed"
          }
          action={
            <CopyForLlm
              message="Sell profit and ROI are hidden because the current market snapshot has no sourced AION2 sale-tax rate."
              context={{
                severity: "warning",
                state: "sale-tax-unconfirmed",
                serverId: effectiveServerId,
                build: snapshot.build,
              }}
            />
          }
        >
          {locale === "ru"
            ? "Сравнение «купить готовое или скрафтить» работает без налога. Прибыль/ROI продажи появятся только после подтверждённого налогового контракта."
            : "Buy-versus-craft works without tax. Sale profit/ROI will appear only after a sourced tax contract."}
        </Alert>
      )}
      <div className="overflow-auto rounded-lg border border-border">
        <table className="w-full min-w-260 text-sm">
          <thead>
            <tr className="bg-muted/70 text-xs uppercase text-muted-foreground">
              <th className="p-3 text-left">
                {locale === "ru" ? "Рецепт" : "Recipe"}
              </th>
              <th className="p-3 text-right">
                {locale === "ru" ? "Себестоимость крафта" : "Craft cost"}
              </th>
              <th className="p-3 text-right">
                {locale === "ru" ? "Цена покупки" : "Buy cost"}
              </th>
              <th className="p-3 text-right">
                {locale === "ru" ? "Экономия крафта" : "Craft savings"}
              </th>
              <th className="p-3 text-right">
                {locale === "ru" ? "Налог продажи" : "Sell tax"}
              </th>
              <th className="p-3 text-right">
                {locale === "ru" ? "Прибыль продажи" : "Sell profit"}
              </th>
              <th className="p-3 text-right">
                {locale === "ru" ? "ROI продажи" : "Sell ROI"}
              </th>
              <th className="p-3 text-right">
                {locale === "ru" ? "Для себя" : "For own use"}
              </th>
            </tr>
          </thead>
          <tbody>
            {rows.map(({ recipe, result }) => (
              <tr key={recipe.recipeId} className="border-t border-border">
                <td className="p-3 font-medium">
                  <div>
                    {effectiveServerId == null ? (
                      craftLocalizedName(
                        { name: recipe.outputName ?? recipe.name, names: recipe.outputNames },
                        locale,
                        `Item ${recipe.outputItemId}`,
                      )
                    ) : (
                      <a
                        data-app-route
                        className="hover:text-emerald-300"
                        href={itemHref(effectiveServerId, recipe.outputItemId)}
                      >
                        {craftLocalizedName(
                          { name: recipe.outputName ?? recipe.name, names: recipe.outputNames },
                          locale,
                          `Item ${recipe.outputItemId}`,
                        )}
                      </a>
                    )}
                    <div className="text-xs font-normal text-muted-foreground">
                      #{recipe.outputItemId} · {recipe.category ? craftDimensionName(recipe.category, locale) : "—"} / {recipe.subcategory ? craftDimensionName(recipe.subcategory, locale) : "—"}
                      {recipe.subtab && recipe.subtab !== "None" ? ` / ${craftDimensionName(recipe.subtab, locale)}` : ""} · ×{recipe.outputQuantity}
                    </div>
                  </div>
                  <details className="mt-2 text-xs font-normal">
                    <summary className="cursor-pointer text-emerald-300">
                      {locale === "ru" ? "Материалы" : "Materials"}: {recipe.materials.length}
                    </summary>
                    <div className="mt-2 space-y-1 border-l border-border pl-3">
                      {recipe.materials.map((material) => {
                        const unitPrice = priceByItem.get(material.itemId) ?? null;
                        const subtotal =
                          unitPrice == null ? null : unitPrice * material.quantity;
                        const label = craftLocalizedName(
                          material,
                          locale,
                          `Item ${material.itemId}`,
                        );
                        return (
                          <div
                            key={`${recipe.recipeId}:${material.itemId}`}
                            className="grid gap-1 sm:grid-cols-[minmax(12rem,1fr)_auto_auto]"
                          >
                            <span>
                              {effectiveServerId == null ? (
                                label
                              ) : (
                                <a
                                  data-app-route
                                  className="hover:text-emerald-300"
                                  href={itemHref(effectiveServerId, material.itemId)}
                                >
                                  {label}
                                </a>
                              )}{" "}
                              <span className="text-muted-foreground">#{material.itemId}</span>
                            </span>
                            <span>×{material.quantity}</span>
                            <span className="text-right text-muted-foreground">
                              {unitPrice == null
                                ? locale === "ru"
                                  ? "цена —"
                                  : "price —"
                                : `${formatKina(unitPrice)} × ${material.quantity} = ${formatKina(subtotal)}`}
                            </span>
                          </div>
                        );
                      })}
                      {recipe.fee > 0 && (
                        <div className="pt-1 text-muted-foreground">
                          {locale === "ru" ? "Комиссия крафта" : "Craft fee"}: {formatKina(recipe.fee)}
                        </div>
                      )}
                    </div>
                  </details>
                  {!result.complete && (
                    <div className="mt-1 flex items-center gap-2 text-xs text-muted-foreground">
                      <span>
                        {locale === "ru" ? "Нет цены" : "Missing prices"}:{" "}
                        {result.missingItemIds.join(", ")}
                      </span>
                      <CopyForLlm
                        message="Craft row has missing market prices."
                        context={{
                          severity: "warning",
                          state: "craft-price-missing",
                          recipeId: recipe.recipeId,
                          serverId: effectiveServerId,
                          missingItemIds: result.missingItemIds.join(","),
                        }}
                      />
                    </div>
                  )}
                </td>
                <td className="p-3 text-right">
                  {formatKina(result.craftCost)}
                </td>
                <td className="p-3 text-right">{formatKina(result.buyCost)}</td>
                <td className="p-3 text-right">
                  {formatKina(result.buyVsCraftSavings)}
                </td>
                <td className="p-3 text-right">{formatKina(result.tax)}</td>
                <td className="p-3 text-right">
                  {formatKina(result.sellProfit)}
                </td>
                <td className="p-3 text-right">
                  {result.sellRoi == null
                    ? "—"
                    : `${result.sellRoi.toFixed(1)}%`}
                </td>
                <td className="p-3 text-right">
                  {result.buyVsCraftSavings == null ? (
                    <Badge tone="neutral">
                      {locale === "ru" ? "Нет данных" : "No data"}
                    </Badge>
                  ) : (
                    <Badge
                      tone={
                        result.buyVsCraftSavings > 0 ? "positive" : "negative"
                      }
                    >
                      {result.buyVsCraftSavings > 0
                        ? locale === "ru"
                          ? "Крафтить"
                          : "Craft"
                        : locale === "ru"
                          ? "Купить"
                          : "Buy"}
                    </Badge>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function Farm({
  snapshot,
  session,
  locale,
}: {
  snapshot: EconomySnapshot;
  session: SessionState;
  locale: AppLocale;
}) {
  const [local, setLocal] = useState<FarmSession[]>(() => {
    try {
      return JSON.parse(
        localStorage.getItem("aion2.farm-sessions/v1") ?? "[]",
      ) as FarmSession[];
    } catch {
      return [];
    }
  });
  const [remote, setRemote] = useState<FarmSession[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [syncOnImport, setSyncOnImport] = useState(false);
  const tr = (ru: string, en: string) => (locale === "ru" ? ru : en);
  useEffect(() => {
    const controller = new AbortController();
    economyApi
      .farmSessions()
      .then(setRemote)
      .catch((reason: unknown) => {
        if (!controller.signal.aborted)
          setError(
            reason instanceof Error
              ? reason.message
              : "farm_history_load_failed",
          );
      });
    return () => controller.abort();
  }, []);
  const sessions = mergeFarmSessions(
    mergeFarmSessions(local, snapshot.farmSessions ?? []),
    remote,
  );
  const provenanceSessions = sessions as FarmSessionWithProvenance[];
  const activityRollups = farmActivityRollups(
    provenanceSessions,
    tr("Без метки", "Unlabelled"),
  );
  const itemNames = useMemo(
    () =>
      new Map(
        snapshot.items.map((item) => [item.itemId, itemName(item, locale)]),
      ),
    [snapshot.items, locale],
  );
  const importExport = async (file: File) => {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      if (file.size > 64 * 1024) throw new Error("farm_export_too_large");
      const payload = parseFarmSessionExport(JSON.parse(await file.text()));
      await verifyFarmSessionExportPayloadId(payload);
      const localSession = farmExportToView(payload, "local");
      const nextLocal = mergeFarmSessions(
        local.filter((item) => item.sessionId !== localSession.sessionId),
        [localSession],
      );
      setLocal(nextLocal);
      localStorage.setItem("aion2.farm-sessions/v1", JSON.stringify(nextLocal));
      if (syncOnImport) {
        const stored = await economyApi.uploadFarmExport(
          payload,
          session.csrfToken,
        );
        setRemote((current) =>
          mergeFarmSessions(
            current.filter((item) => item.sessionId !== stored.sessionId),
            [stored],
          ),
        );
        setNotice(
          `${tr("Сессия", "Session")} ${stored.sessionId} ${tr("сохранена локально и синхронизирована с сервером.", "was saved locally and synchronized with the server.")}`,
        );
      } else {
        setNotice(
          `${tr("Сессия", "Session")} ${localSession.sessionId} ${tr("сохранена только в этом браузере.", "was saved only in this browser.")}`,
        );
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "farm_import_failed");
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="space-y-4">
      <DesktopSyncTokens session={session} locale={locale} />
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="space-y-2">
          <p className="text-sm text-muted-foreground">
            {tr(
              "Импорт принимает только агрегат",
              "Import accepts only the aggregate",
            )}{" "}
            <code>a2.farm.session-export.v1</code>;{" "}
            {tr(
              "raw-пакеты и игровые токены на сервер не отправляются.",
              "raw packets and game tokens are not sent to the server.",
            )}
          </p>
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={syncOnImport}
              onChange={(event) => setSyncOnImport(event.target.checked)}
            />
            <span>
              {tr(
                "После импорта синхронизировать агрегат с приватным сервером",
                "Synchronize the aggregate with the private server after import",
              )}
            </span>
          </label>
        </div>
        <label
          className={cn(
            "inline-flex h-9 cursor-pointer items-center rounded-md bg-emerald-500 px-4 text-sm font-medium text-black transition-colors hover:bg-emerald-400",
            busy && "pointer-events-none opacity-50",
          )}
        >
          <Upload className="mr-2 size-4" />
          {busy
            ? tr("Сохраняю…", "Saving…")
            : syncOnImport
              ? tr("Импортировать и синхронизировать", "Import and sync")
              : tr("Импортировать локально", "Import locally")}
          <input
            className="hidden"
            type="file"
            accept="application/json,.json"
            disabled={busy}
            onChange={(event) => {
              const file = event.currentTarget.files?.[0];
              if (file) void importExport(file);
              event.currentTarget.value = "";
            }}
          />
        </label>
      </div>
      {notice && (
        <Alert title={tr("История фарма обновлена", "Farm history updated")}>
          {notice}
        </Alert>
      )}
      {error && (
        <Alert
          title={tr(
            "Не удалось синхронизировать историю фарма",
            "Could not synchronize farm history",
          )}
          action={
            <CopyForLlm
              message={`AION2 Economy farm import: ${error}`}
              context={{ severity: "error", state: "farm-import-failed" }}
            />
          }
        >
          {error}.{" "}
          {tr(
            "Локальная запись выполняется только после проверки SHA-256; серверная запись не считается выполненной.",
            "The local record is stored only after SHA-256 verification; the server record is not treated as completed.",
          )}
        </Alert>
      )}
      {!sessions.length && (
        <Alert
          title={tr("Фарм-сессий пока нет", "No farm sessions yet")}
          action={
            <CopyForLlm
              message="Farm history пуста: импортируйте завершённую сессию из desktop farm tracker."
              context={{
                severity: "warning",
                state: "farm-sessions-empty",
                build: snapshot.build,
              }}
            />
          }
        >
          {tr(
            "Завершите Start/Stop в desktop farm tracker и импортируйте агрегированный JSON. После синхронизации история доступна на любом устройстве этого аккаунта.",
            "Complete Start/Stop in the desktop farm tracker and import the aggregated JSON. After synchronization, history is available on any device for this account.",
          )}
        </Alert>
      )}
      {activityRollups.length > 0 && (
        <div className="grid gap-3 md:grid-cols-3">
          {activityRollups.map((row) => (
            <Card key={row.key}>
              <CardContent className="pt-5">
                <div className="font-medium">{row.label}</div>
                <div className="text-xs text-muted-foreground">
                  {row.kind} · {row.sessions} {tr("сесс.", "sessions")}
                </div>
                <div className="mt-3 text-xl font-semibold text-emerald-300">
                  {row.kinaPerSecond.toFixed(1)} Kina/s
                </div>
                <div className="text-xs text-muted-foreground">
                  {formatKina(row.totalValue)} {tr("за", "over")}{" "}
                  {Math.round(row.durationSeconds / 60)} {tr("мин", "min")}
                </div>
              </CardContent>
            </Card>
          ))}
        </div>
      )}
      {provenanceSessions.map((row) => {
        const activity = row.activity;
        const stalePriceRows = row.loot.filter(
          (loot) =>
            loot.unitPrice != null &&
            marketFreshness(
              loot.priceObservedAtMs == null
                ? null
                : new Date(loot.priceObservedAtMs).toISOString(),
            ) !== "fresh",
        );
        return (
          <Card key={row.payloadId ?? row.sessionId}>
            <CardContent className="space-y-4 pt-5">
              <div className="grid gap-4 md:grid-cols-6">
                <div className="md:col-span-2">
                  <div className="font-medium">
                    {activity?.label || tr("Без метки", "Unlabelled")}
                  </div>
                  <div className="text-xs text-muted-foreground">
                    {activity?.kind ||
                      tr(
                        "активность не указана",
                        "activity not specified",
                      )}{" "}
                    ·{" "}
                    {new Date(row.startedAt).toLocaleString(
                      numberLocale(locale),
                    )}{" "}
                    · {Math.round(row.durationSeconds / 60)} {tr("мин", "min")}{" "}
                    · {row.source}
                  </div>
                </div>
                <div>
                  <span className="text-xs text-muted-foreground">
                    {tr("Изменение кошелька", "Net wallet delta")}
                  </span>
                  <div className="font-semibold text-amber-200">
                    {formatKina(row.directKina)}
                  </div>
                </div>
                <div>
                  <span className="text-xs text-muted-foreground">
                    {tr("Стоимость лута", "Inventory gains")}
                  </span>
                  <div className="font-semibold">
                    {formatKina(row.lootValue)}
                  </div>
                </div>
                <div>
                  <span className="text-xs text-muted-foreground">
                    {tr("Оценка всего", "Estimated total")}
                  </span>
                  <div className="font-semibold">
                    {formatKina(row.totalValue)}
                  </div>
                </div>
                <div>
                  <span className="text-xs text-muted-foreground">
                    {tr("Оценка в секунду", "Estimated value/sec")}
                  </span>
                  <div className="font-semibold text-emerald-300">
                    {row.kinaPerSecond.toFixed(1)}
                  </div>
                </div>
              </div>
              {stalePriceRows.length > 0 && (
                <Alert
                  title={tr(
                    "Оценка лута использует устаревшие цены",
                    "Loot estimate uses stale prices",
                  )}
                  action={
                    <CopyForLlm
                      message="Farm session valuation uses stale or undated market quotes."
                      context={{
                        severity: "warning",
                        state: "farm-prices-stale",
                        sessionId: row.sessionId,
                        staleQuotes: stalePriceRows.length,
                        pricedLoot: row.loot.filter(
                          (loot) => loot.unitPrice != null,
                        ).length,
                      }}
                    />
                  }
                >
                  {tr(
                    "Оценочная стоимость и Kina/с могут не отражать текущий рынок. Возраст каждой котировки указан в таблице лута.",
                    "Estimated value and Kina/s may not reflect the current market. Each quote age is shown in the loot table.",
                  )}
                </Alert>
              )}
              {row.loot.length > 0 && (
                <details className="rounded-md border border-border p-3">
                  <summary className="cursor-pointer text-sm font-medium">
                    {tr("Полученный лут", "Inventory gains")} ·{" "}
                    {row.loot.length}
                  </summary>
                  <div className="mt-3 overflow-auto">
                    <table className="w-full text-sm">
                      <thead>
                        <tr className="text-xs uppercase text-muted-foreground">
                          <th className="p-2 text-left">
                            {tr("Предмет", "Item")}
                          </th>
                          <th className="p-2 text-right">
                            {tr("Кол-во", "Qty")}
                          </th>
                          <th className="p-2 text-right">
                            {tr("Цена", "Price")}
                          </th>
                          <th className="p-2 text-right">
                            {tr("Оценка", "Estimate")}
                          </th>
                        </tr>
                      </thead>
                      <tbody>
                        {row.loot.map((loot) => (
                          <tr
                            key={loot.itemId}
                            className="border-t border-border"
                          >
                            <td className="p-2">
                              {itemNames.get(loot.itemId) ?? loot.name}{" "}
                              <span className="text-xs text-muted-foreground">
                                #{loot.itemId}
                              </span>
                            </td>
                            <td className="p-2 text-right">{loot.quantity}</td>
                            <td className="p-2 text-right">
                              <div>{formatKina(loot.unitPrice)}</div>
                              {loot.unitPrice != null && (
                                <div className="mt-1 text-xs text-muted-foreground">
                                  {marketFreshness(
                                    loot.priceObservedAtMs == null
                                      ? null
                                      : new Date(
                                          loot.priceObservedAtMs,
                                        ).toISOString(),
                                  ) === "fresh"
                                    ? tr("Свежая", "Fresh")
                                    : tr("Устарела", "Stale")}{" "}
                                  ·{" "}
                                  {observationAge(
                                    loot.priceObservedAtMs == null
                                      ? null
                                      : new Date(
                                          loot.priceObservedAtMs,
                                        ).toISOString(),
                                    locale,
                                  )}
                                </div>
                              )}
                            </td>
                            <td className="p-2 text-right">
                              {formatKina(loot.estimatedValue)}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                  {row.loot.some((loot) => loot.unitPrice == null) && (
                    <div className="mt-3">
                      <Alert
                        title={tr(
                          "Не для всего лута есть рыночная цена",
                          "Some loot has no market price",
                        )}
                        action={
                          <CopyForLlm
                            message="Farm session contains inventory gains without a server-specific market quote."
                            context={{
                              severity: "warning",
                              state: "farm-loot-price-missing",
                              sessionId: row.sessionId,
                              payloadId: row.payloadId ?? "unknown",
                            }}
                          />
                        }
                      >
                        {tr(
                          "Такие позиции не включены в оценочную стоимость.",
                          "Those entries are excluded from estimated value.",
                        )}
                      </Alert>
                    </div>
                  )}
                </details>
              )}
            </CardContent>
          </Card>
        );
      })}
    </div>
  );
}

function EconomyApp({
  session,
  onLogout,
  locale,
  onLocaleChange,
}: {
  session: SessionState;
  onLogout: () => Promise<void>;
  locale: AppLocale;
  onLocaleChange: (locale: AppLocale) => void;
}) {
  const { snapshot, itemsSnapshot, error } = useSnapshot(
    session.role === "approved" || session.role === "admin",
  );
  const [route, setRoute] = useState<AppRoute>(() =>
    parseAppRoute(window.location),
  );
  const crawlerApi = useMemo(
    () => createCrawlerApi("/api/v1", fetch, session.csrfToken),
    [session.csrfToken],
  );
  const visibleNav = nav.filter(
    (entry) =>
      !["crawler", "admin"].includes(entry.id) || session.role === "admin",
  );
  useEffect(() => {
    const syncLocation = () => {
      const next = parseAppRoute(window.location);
      if (window.location.hash.startsWith("#/")) {
        window.history.replaceState(null, "", routeHref(next));
      }
      setRoute(next);
    };
    const onClick = (event: MouseEvent) => {
      if (
        event.defaultPrevented ||
        event.button !== 0 ||
        event.metaKey ||
        event.ctrlKey ||
        event.shiftKey ||
        event.altKey
      )
        return;
      const target = event.target;
      const anchor =
        target instanceof Element
          ? target.closest<HTMLAnchorElement>("a[data-app-route]")
          : null;
      if (!anchor || anchor.target || anchor.hasAttribute("download")) return;
      const destination = new URL(anchor.href, window.location.href);
      if (destination.origin !== window.location.origin) return;
      event.preventDefault();
      window.history.pushState(
        null,
        "",
        `${destination.pathname}${destination.search}`,
      );
      syncLocation();
      window.scrollTo({ top: 0, behavior: "auto" });
    };
    syncLocation();
    window.addEventListener("popstate", syncLocation);
    document.addEventListener("click", onClick);
    return () => {
      window.removeEventListener("popstate", syncLocation);
      document.removeEventListener("click", onClick);
    };
  }, []);
  const t = text[locale];
  const requestedView = route.view === "item" ? "items" : route.view;
  const safeView = visibleNav.some((entry) => entry.id === requestedView)
    ? requestedView
    : "overview";
  const currentLabel = route.view === "item" ? t.details : t.nav[safeView];
  const selectedItem =
    route.view === "item"
      ? itemsSnapshot.items.find(
          (item) =>
            (route.serverId === null || item.serverId === route.serverId) &&
            item.itemId === route.itemId,
        )
      : undefined;
  const selectedServer =
    route.view === "item" && selectedItem
      ? snapshot.servers.find(
          (server) => server.serverId === selectedItem.serverId,
        )
      : undefined;
  const snapshotLoaded = snapshot.generatedAt !== new Date(0).toISOString();
  return (
    <div className="min-h-screen bg-background text-foreground">
      <header className="sticky top-0 z-20 flex min-h-14 flex-wrap items-center gap-2 border-b border-border bg-background/90 px-4 py-2 backdrop-blur md:px-7">
        <Database className="size-5 text-emerald-400" />
        <span className="font-semibold">
          AION2 <span className="text-emerald-400">Economy</span>
        </span>
        <div className="ml-auto flex flex-wrap items-center justify-end gap-2 text-xs text-muted-foreground">
          <div
            className="inline-flex rounded-md border border-border p-0.5"
            aria-label="Language"
          >
            <button
              className={cn(
                "rounded px-2 py-1",
                locale === "ru"
                  ? "bg-muted text-foreground"
                  : "hover:text-foreground",
              )}
              onClick={() => onLocaleChange("ru")}
            >
              RU
            </button>
            <button
              className={cn(
                "rounded px-2 py-1",
                locale === "en"
                  ? "bg-muted text-foreground"
                  : "hover:text-foreground",
              )}
              onClick={() => onLocaleChange("en")}
            >
              EN
            </button>
          </div>
          <Badge tone={session.role === "admin" ? "positive" : "neutral"}>
            {session.role}
          </Badge>
          <span className="hidden sm:inline">{session.username}</span>
          <span className="hidden lg:inline">
            Build {snapshot.build ?? "—"}
          </span>
          <Button
            variant="ghost"
            aria-label={t.logout}
            title={t.logout}
            onClick={() => void onLogout()}
          >
            <LogOut className="size-4" />
          </Button>
        </div>
      </header>
      <div className="mx-auto grid max-w-440 md:grid-cols-[230px_minmax(0,1fr)]">
        <aside className="border-b border-border p-3 md:min-h-[calc(100vh-3.5rem)] md:border-b-0 md:border-r md:p-4">
          <nav className="grid grid-cols-2 gap-1 md:grid-cols-1">
            {visibleNav.map((entry) => (
              <a
                data-app-route
                key={entry.id}
                href={appHref(entry.id)}
                className={cn(
                  "flex items-center gap-2 rounded-md px-3 py-2 text-left text-sm transition-colors",
                  safeView === entry.id
                    ? "bg-muted text-foreground"
                    : "text-muted-foreground hover:bg-muted/50 hover:text-foreground",
                )}
              >
                <entry.icon className="size-4" />
                {t.nav[entry.id]}
              </a>
            ))}
          </nav>
          <div className="mt-6 hidden rounded-lg border border-border bg-card p-3 text-xs text-muted-foreground md:block">
            <div className="mb-2 flex items-center gap-2 text-foreground">
              <History className="size-4" />
              {t.latestSnapshot}
            </div>
            {snapshotLoaded
              ? new Date(snapshot.generatedAt).toLocaleString(
                  numberLocale(locale),
                )
              : t.loading}
          </div>
        </aside>
        <main className="min-w-0 p-4 md:p-7">
          <div className="mb-6">
            <div className="text-xs uppercase tracking-[0.14em] text-muted-foreground">
              {t.observedMarket}
            </div>
            <h1 className="mt-1 text-2xl font-semibold">{currentLabel}</h1>
          </div>
          {error && (
            <div className="mb-6">
              <Alert
                title={t.loadFailed}
                action={
                  <CopyForLlm
                    message={`Economy UI: ${error}`}
                    context={{
                      severity: "error",
                      state: "snapshot-load-failed",
                    }}
                  />
                }
              >
                {error}
              </Alert>
            </div>
          )}
          {!error &&
            snapshot.limitations.length > 0 &&
            !["admin", "crawler"].includes(safeView) && (
              <div className="mb-6">
                <Alert
                  title={t.incomplete}
                  action={
                    <CopyForLlm
                      message={snapshot.limitations.join("\n")}
                      context={{
                        severity: "warning",
                        state: "snapshot-degraded",
                        build: snapshot.build,
                      }}
                    />
                  }
                >
                  {snapshot.limitations.join(" · ")}
                </Alert>
              </div>
            )}
          {route.view === "item" && selectedItem && (
            <ItemDetail
              item={selectedItem}
              locale={locale}
              serverLabel={
                selectedServer
                  ? serverName(selectedServer, locale)
                  : `${t.observedServer} · ${selectedItem.serverId}`
              }
              observedAt={selectedItem.lastObservedAt ?? null}
            />
          )}
          {route.view === "item" &&
            !selectedItem &&
            !error &&
            snapshotLoaded && (
              <Alert
                title={t.itemNotFound}
                action={
                  <CopyForLlm
                    message="Economy item route is absent from the current snapshot."
                    context={{
                      severity: "warning",
                      state: "market-item-not-found",
                      itemId: route.itemId,
                      serverId: route.serverId,
                      build: snapshot.build,
                    }}
                  />
                }
              >
                {t.itemNotFoundBody}
              </Alert>
            )}
          {route.view === "item" &&
            !selectedItem &&
            !error &&
            !snapshotLoaded && (
              <div className="text-sm text-muted-foreground">{t.loading}</div>
            )}
          {route.view !== "item" && safeView === "overview" && (
            <Overview snapshot={snapshot} locale={locale} />
          )}
          {route.view !== "item" && safeView === "items" && (
            <Items
              snapshot={itemsSnapshot}
              locale={locale}
              initialSection={
                route.view === "items" ? route.section : undefined
              }
              initialServerId={
                route.view === "items" ? route.serverId : undefined
              }
              initialRegion={
                route.view === "items" ? route.region : undefined
              }
              initialFaction={
                route.view === "items" ? route.faction : undefined
              }
            />
          )}
          {route.view !== "item" && safeView === "movers" && (
            <Movers snapshot={snapshot} locale={locale} />
          )}
          {route.view !== "item" && safeView === "servers" && (
            <Servers snapshot={snapshot} locale={locale} />
          )}
          {route.view !== "item" && safeView === "craft" && (
            <Craft snapshot={snapshot} locale={locale} />
          )}
          {route.view !== "item" && safeView === "farm" && (
            <Farm snapshot={snapshot} session={session} locale={locale} />
          )}
          {route.view !== "item" &&
            safeView === "crawler" &&
            session.role === "admin" && (
              <CrawlerConsole
                role={session.role}
                api={crawlerApi}
                locale={locale}
              />
            )}
          {route.view !== "item" &&
            safeView === "admin" &&
            session.role === "admin" && (
              <AdminUsers session={session} locale={locale} />
            )}
        </main>
      </div>
    </div>
  );
}

export function App() {
  const [session, setSession] = useState<SessionState | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [locale, setLocale] = useState<AppLocale>(() =>
    localStorage.getItem("aion2.economy.locale") === "en" ? "en" : "ru",
  );
  const setLanguage = (next: AppLocale) => {
    setLocale(next);
    localStorage.setItem("aion2.economy.locale", next);
    document.documentElement.lang = next;
  };
  useEffect(() => {
    document.documentElement.lang = locale;
  }, [locale]);
  useEffect(() => {
    economyApi
      .session()
      .then(setSession)
      .catch((reason: unknown) =>
        setError(reason instanceof Error ? reason.message : "session_failed"),
      )
      .finally(() => setLoading(false));
  }, []);
  const authenticate = async (
    mode: "login" | "register",
    username: string,
    password: string,
  ): Promise<"authenticated" | "registered"> => {
    if (mode === "register") {
      await economyApi.register(username, password);
      setError(null);
      return "registered";
    }
    setSession(await economyApi.login(username, password));
    setError(null);
    return "authenticated";
  };
  const logout = async () => {
    const next = await economyApi.logout(session?.csrfToken ?? null);
    setSession(next);
  };
  return (
    <AuthGate
      session={session}
      loading={loading}
      error={error}
      locale={locale}
      onLocaleChange={setLanguage}
      onAuthenticate={authenticate}
      onLogout={logout}
    >
      {session && (
        <EconomyApp
          session={session}
          onLogout={logout}
          locale={locale}
          onLocaleChange={setLanguage}
        />
      )}
    </AuthGate>
  );
}
