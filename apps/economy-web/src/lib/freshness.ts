import type { EconomyServer } from "../types";

export const STALE_AFTER_MS = 6 * 60 * 60 * 1000;

export type MarketFreshness = "fresh" | "stale" | "unknown";

export function marketFreshness(
  observedAt: string | null | undefined,
  nowMs = Date.now(),
): MarketFreshness {
  if (!observedAt) return "unknown";
  const observedMs = Date.parse(observedAt);
  if (!Number.isFinite(observedMs)) return "unknown";
  return Math.max(0, nowMs - observedMs) > STALE_AFTER_MS ? "stale" : "fresh";
}

export function observationAge(
  observedAt: string | null | undefined,
  locale: "ru" | "en",
  nowMs = Date.now(),
): string {
  if (!observedAt || !Number.isFinite(Date.parse(observedAt)))
    return locale === "ru" ? "возраст неизвестен" : "age unknown";
  const minutes = Math.max(
    0,
    Math.floor((nowMs - Date.parse(observedAt)) / 60_000),
  );
  if (minutes < 60)
    return locale === "ru" ? `${minutes} мин назад` : `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return locale === "ru" ? `${hours} ч назад` : `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return locale === "ru" ? `${days} дн назад` : `${days}d ago`;
}

export function freshnessTotals(servers: EconomyServer[], nowMs = Date.now()) {
  return servers.reduce(
    (totals, server) => {
      const bucket = marketFreshness(server.lastObservedAt, nowMs);
      totals[bucket].activeItems += server.activeItems;
      totals[bucket].listings += server.listings;
      return totals;
    },
    {
      fresh: { activeItems: 0, listings: 0 },
      stale: { activeItems: 0, listings: 0 },
      unknown: { activeItems: 0, listings: 0 },
    },
  );
}
