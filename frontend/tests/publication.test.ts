import assert from "node:assert/strict";
import test from "node:test";
import {
  INDEX_URL, catalogForRoute, publicationUrl, forecastChanged, readPublication,
  type PublicationIndex,
} from "../src/publication.ts";
import type { EventListing } from "../src/model.ts";

test("catalog checks revalidate while immutable snapshots use the browser cache", async () => {
  const requests: [string, RequestInit][] = [];
  const fetcher = async (url: string, init: RequestInit) => {
    requests.push([url, init]);
    return Response.json({ ok: true });
  };
  await readPublication(INDEX_URL, undefined, fetcher as typeof fetch);
  await readPublication("/generated/catalogs/2025/USPA.json", undefined, fetcher as typeof fetch);
  await readPublication("/generated/event-abc.json", undefined, fetcher as typeof fetch);
  assert.equal(requests[0][1].cache, "no-cache");
  assert.equal(requests[1][1].cache, "no-cache");
  assert.equal(requests[2][1].cache, "default");
  assert.ok(requests.every(([, init]) => init.credentials === "omit"));
  assert.ok(requests.every(([url]) => url.startsWith("/generated/")));
});

test("new and withdrawn forecasts are noticed without changing the loaded listing", () => {
  const current = { forecast_url: "/generated/event-old.json" } as EventListing;
  const before = structuredClone(current);
  assert.equal(forecastChanged(current, { ...current }), false);
  assert.equal(forecastChanged(current, { ...current, forecast_url: "/generated/event-new.json" }), true);
  assert.equal(forecastChanged(current, { ...current, forecast_url: null }), true);
  assert.equal(forecastChanged(current, null), true);
  assert.equal(forecastChanged({ ...current, forecast_url: null }, current), true);
  assert.deepEqual(current, before);
});

test("failed refreshes reject instead of returning a replacement publication", async () => {
  const fetcher = async () => new Response("unavailable", { status: 503 });
  await assert.rejects(readPublication(INDEX_URL, undefined, fetcher as typeof fetch), /unavailable/);
});

test("routes select a single region without inferring it from event codes", () => {
  const index: PublicationIndex = { regions: [
    { season: 2025, region: "USNJ", region_name: "New Jersey", catalog_url: "/generated/catalogs/2025/USNJ.json", event_codes: ["DIVISION1"] },
    { season: 2025, region: "USPA", region_name: "Pennsylvania", catalog_url: "/generated/catalogs/2025/USPA.json", event_codes: ["Q1"] },
    { season: 2024, region: "USPA", region_name: "Pennsylvania", catalog_url: "/generated/catalogs/2024/USPA.json", event_codes: ["DIVISION1"] },
  ] };
  assert.equal(catalogForRoute("/", index)?.region, "USPA");
  assert.equal(catalogForRoute("/seasons/2025/USNJ/", index)?.region, "USNJ");
  assert.equal(catalogForRoute("/events/2025/DIVISION1", index)?.region, "USNJ");
  assert.equal(catalogForRoute("/events/2024/DIVISION1", index)?.season, 2024);
  assert.equal(catalogForRoute("/events/2026/DIVISION1", index), null);
  assert.equal(catalogForRoute("/seasons/2025/UNKNOWN", index), null);
  assert.equal(catalogForRoute("/", { regions: [] }), null);
  assert.equal(publicationUrl("/generated/engine-abc.tar", "https://data.example/"), "https://data.example/generated/engine-abc.tar");
  assert.equal(publicationUrl("/generated/event-abc.json", ""), "/generated/event-abc.json");
});
