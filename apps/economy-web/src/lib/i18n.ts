import type { EconomyItem, EconomyServer, LocalizedNames } from "../types";

export type AppLocale = "ru" | "en";

const localeKeys: Record<AppLocale, Array<keyof LocalizedNames>> = {
  ru: ["ru", "ru-RU"],
  en: ["en", "en-US"],
};

export function localizedName(
  names: LocalizedNames | undefined,
  fallback: string,
  locale: AppLocale,
): string {
  for (const key of localeKeys[locale]) {
    const value = names?.[key];
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  for (const key of localeKeys[locale === "ru" ? "en" : "ru"]) {
    const value = names?.[key];
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return fallback;
}

export function itemName(
  item: Pick<EconomyItem, "name" | "names">,
  locale: AppLocale,
): string {
  return localizedName(item.names, item.name, locale);
}

export function serverName(
  server: Pick<EconomyServer, "name" | "names" | "serverId">,
  locale: AppLocale,
): string {
  return localizedName(
    server.names,
    server.name || `Server ${server.serverId}`,
    locale,
  );
}

export function itemHref(serverId: number, itemId: number): string {
  return `/items/${encodeURIComponent(String(serverId))}/${encodeURIComponent(String(itemId))}`;
}

const ruRegions: Record<string, string> = {
  Asia: "Азия",
  Europe: "Европа",
  "North America - East": "Северная Америка — Восток",
  "North America - West": "Северная Америка — Запад",
  "South America": "Южная Америка",
};

const ruFactions: Record<string, string> = {
  Elyos: "Элийцы",
  Asmodians: "Асмодиане",
};

const ruCategories: Record<string, string> = {
  Equip: "Экипировка",
  Misc: "Разное",
  Usable: "Расходники",
};

export function regionName(value: string, locale: AppLocale): string {
  return locale === "ru" ? (ruRegions[value] ?? value) : value;
}

export function factionName(value: string, locale: AppLocale): string {
  return locale === "ru" ? (ruFactions[value] ?? value) : value;
}

export function categoryName(value: string, locale: AppLocale): string {
  return locale === "ru" ? (ruCategories[value] ?? value) : value;
}

export const numberLocale = (locale: AppLocale) =>
  locale === "ru" ? "ru-RU" : "en-US";
