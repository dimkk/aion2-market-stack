import { describe, expect, it } from "vitest";

import { appHref, parseAppRoute, routeHref } from "./routes";

describe("canonical economy routes", () => {
  it("parses main views and item deep links from pathname", () => {
    expect(parseAppRoute({ pathname: "/items", search: "?section=Gear" })).toEqual({
      view: "items",
      section: "Gear",
      serverId: undefined,
      region: undefined,
      faction: undefined,
    });
    expect(parseAppRoute({ pathname: "/items/2301/516150001", search: "" })).toEqual({
      view: "item",
      serverId: 2301,
      itemId: 516150001,
    });
    expect(parseAppRoute({ pathname: "/items/516150001", search: "" })).toEqual({
      view: "item",
      serverId: null,
      itemId: 516150001,
    });
  });

  it("maps legacy hash bookmarks to a canonical route", () => {
    const route = parseAppRoute({
      pathname: "/",
      search: "",
      hash: "#/items/2301/516150001",
    });
    expect(routeHref(route)).toBe("/items/2301/516150001");
  });

  it("does not accept malformed or unsafe item identifiers", () => {
    expect(parseAppRoute({ pathname: "/items/not-a-number", search: "" })).toEqual({
      view: "items",
      section: undefined,
      serverId: undefined,
      region: undefined,
      faction: undefined,
    });
    expect(parseAppRoute({ pathname: "/items/9999999999999999/1", search: "" })).toEqual({
      view: "items",
      section: undefined,
      serverId: undefined,
      region: undefined,
      faction: undefined,
    });
  });

  it("builds one canonical URL for every main view", () => {
    expect(appHref("overview")).toBe("/");
    expect(appHref("items", "Materials")).toBe("/items?section=Materials");
    expect(
      appHref("items", undefined, {
        serverId: 12301,
        region: "Europe",
        faction: "Elyos",
      }),
    ).toBe("/items?server=12301&region=Europe&faction=Elyos");
    expect(appHref("crawler")).toBe("/crawler");
  });

  it("parses canonical item-list filters", () => {
    expect(
      parseAppRoute({
        pathname: "/items",
        search: "?server=12301&region=Europe&faction=Elyos",
      }),
    ).toEqual({
      view: "items",
      section: undefined,
      serverId: 12301,
      region: "Europe",
      faction: "Elyos",
    });
  });
});
