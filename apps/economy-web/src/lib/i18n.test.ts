import { describe, expect, it } from "vitest";

import { itemHref, localizedName } from "./i18n";

describe("localized economy labels", () => {
  it("prefers the requested locale and accepts long locale keys", () => {
    const names = { "en-US": "Courage Bow", "ru-RU": "Лук отваги" };
    expect(localizedName(names, "fallback", "ru")).toBe("Лук отваги");
    expect(localizedName(names, "fallback", "en")).toBe("Courage Bow");
  });

  it("falls back to another supplied locale before the legacy label", () => {
    expect(localizedName({ en: "Known item" }, "Item 123", "ru")).toBe(
      "Known item",
    );
    expect(localizedName(undefined, "Item 123", "ru")).toBe("Item 123");
  });

  it("builds a stable canonical item route", () => {
    expect(itemHref(2301, 101)).toBe("/items/2301/101");
  });
});
