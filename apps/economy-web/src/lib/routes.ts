import type { EconomySection } from "./economy";

export type View =
  | "overview"
  | "items"
  | "movers"
  | "servers"
  | "craft"
  | "farm"
  | "crawler"
  | "admin";

export type ItemRoute = {
  view: "item";
  serverId: number | null;
  itemId: number;
};

export type ViewRoute = {
  view: View;
  section?: EconomySection;
  serverId?: number;
  region?: string;
  faction?: string;
};

export type AppRoute = ViewRoute | ItemRoute;

export type RouteLocation = {
  pathname: string;
  search: string;
  hash?: string;
};

export const appViews: readonly View[] = [
  "overview",
  "items",
  "movers",
  "servers",
  "craft",
  "farm",
  "crawler",
  "admin",
];

const sections: readonly EconomySection[] = [
  "Materials",
  "Refining",
  "Crafting",
  "Gear",
  "Consumables",
  "Other",
];

const identifier = (value: string | undefined): number | null => {
  if (!value || !/^[0-9]{1,16}$/.test(value)) return null;
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) ? parsed : null;
};

const routeSource = (location: RouteLocation): { pathname: string; search: string } => {
  if (location.pathname === "/" && location.hash?.startsWith("#/")) {
    const legacy = location.hash.slice(1);
    const separator = legacy.indexOf("?");
    return separator === -1
      ? { pathname: legacy, search: "" }
      : { pathname: legacy.slice(0, separator), search: legacy.slice(separator) };
  }
  return { pathname: location.pathname, search: location.search };
};

const dimension = (value: string | null): string | undefined => {
  const normalized = value?.trim();
  return normalized && normalized.length <= 80 ? normalized : undefined;
};

export function parseAppRoute(location: RouteLocation): AppRoute {
  const source = routeSource(location);
  const segments = source.pathname.split("/").filter(Boolean);
  if (segments[0] === "items" && (segments.length === 2 || segments.length === 3)) {
    const serverId = segments.length === 3 ? identifier(segments[1]) : null;
    const itemId = identifier(segments.at(-1));
    if (itemId !== null && (segments.length === 2 || serverId !== null)) {
      return { view: "item", serverId, itemId };
    }
  }
  const known = appViews.includes(segments[0] as View)
    ? (segments[0] as View)
    : "overview";
  const search = new URLSearchParams(source.search);
  const requestedSection = search.get("section");
  const serverId = known === "items" ? identifier(search.get("server") ?? undefined) : null;
  return {
    view: known,
    section:
      known === "items" && sections.includes(requestedSection as EconomySection)
        ? (requestedSection as EconomySection)
        : undefined,
    serverId: known === "items" && serverId !== null ? serverId : undefined,
    region: known === "items" ? dimension(search.get("region")) : undefined,
    faction: known === "items" ? dimension(search.get("faction")) : undefined,
  };
}

export function appHref(
  view: View,
  section?: EconomySection,
  filters?: { serverId?: number; region?: string; faction?: string },
): string {
  const pathname = view === "overview" ? "/" : `/${view}`;
  const search = new URLSearchParams();
  if (section) search.set("section", section);
  if (view === "items" && filters?.serverId != null)
    search.set("server", String(filters.serverId));
  if (view === "items" && filters?.region) search.set("region", filters.region);
  if (view === "items" && filters?.faction)
    search.set("faction", filters.faction);
  const query = search.toString();
  return query ? `${pathname}?${query}` : pathname;
}

export function routeHref(route: AppRoute): string {
  if (route.view !== "item")
    return appHref(route.view, route.section, {
      serverId: route.serverId,
      region: route.region,
      faction: route.faction,
    });
  const item = encodeURIComponent(String(route.itemId));
  return route.serverId === null
    ? `/items/${item}`
    : `/items/${encodeURIComponent(String(route.serverId))}/${item}`;
}
