import test from "node:test";
import assert from "node:assert/strict";
import {
  routeFor,
  formatDate,
  statusLabel,
  type Catalog,
} from "../src/model.ts";

test("stable routes distinguish season, events, unavailable forecasts, and unknown URLs", () => {
  const listing = {
    season: 2025,
    code: "USPAHAQ1",
    forecast_url: null,
    status: "unpublished",
  };
  const catalog = {
    season: 2025,
    region: "USPA",
    events: [listing],
  } as Catalog;
  assert.equal(routeFor("/", catalog), "season");
  assert.equal(routeFor("/seasons/2025/USPA/", catalog), "season");
  assert.equal(routeFor("/events/2025/USPAHAQ1", catalog), listing);
  assert.equal(routeFor("/events/2026/USPAHAQ1", catalog), null);
  assert.equal(routeFor("/unknown", catalog), null);
  assert.equal(statusLabel("unpublished"), "Not published");
  assert.equal(formatDate(null), "Date not set");
  assert.match(formatDate("2026-01-10"), /10/);
});
