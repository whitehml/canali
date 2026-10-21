import test from "node:test";
import assert from "node:assert/strict";
import {
  scenarioSnapshot,
  rankedTeams,
  percent,
  scenarioUrl,
  scenarioFromUrl,
  forecastChange,
  validateComparison,
  hasRemainingQualifications,
  forecastBasis,
  type PublishedEvent,
  type Snapshot,
} from "../src/model.ts";

test("forecast basis identifies the included official match or event stage", () => {
  const snapshot = { last_match: null, progress: { played: [true, false, true] } } as Snapshot;
  snapshot.last_match = { level: "QUALIFICATION", number: 7 };
  assert.equal(forecastBasis(snapshot), "Through qualification match 7");
  snapshot.progress!.alliances = [[1, 2], [3, 4]];
  assert.equal(forecastBasis(snapshot), "After alliance selection");
  snapshot.last_match = { level: "FINAL", number: 3, series: 1 };
  assert.equal(forecastBasis(snapshot), "Through final series 1, match 3");
  snapshot.last_match = null;
  snapshot.progress = null;
  assert.equal(forecastBasis(snapshot), "Before qualification matches");
});

test("LEAVE eligibility uses the selected team's unplayed appearances", () => {
  const snapshot = {
    schedule: { stations: [[[1, 2], [3, 4]], [[3, 5], [4, 6]]] },
    progress: { played: [true, false] },
  } as unknown as Snapshot;
  assert.equal(hasRemainingQualifications(snapshot, "1"), false);
  assert.equal(hasRemainingQualifications(snapshot, "3"), true);
  assert.equal(hasRemainingQualifications(snapshot, "6"), true);
  assert.equal(hasRemainingQualifications(snapshot, "99"), false);
  assert.equal(hasRemainingQualifications({ ...snapshot, progress: null }, "1"), true);
  assert.equal(hasRemainingQualifications({ ...snapshot, schedule: null }, "1"), false);
  assert.equal(hasRemainingQualifications({
    ...snapshot, progress: { played: [true, true] },
  }, "3"), false);
});

test("what-if scales multiple chosen teams and preserves official results and source inputs", () => {
  const source = {
    field: {
      predicted: {
        "1": { auto: 20, teleop: 80 },
        "2": { auto: 10 },
        "3": { auto: 12 },
      },
    },
    progress: { played: [true, false] },
    seed: 42,
  } as unknown as Snapshot;
  const changed = scenarioSnapshot(source, { "1": 110, "2": 50 }, 1000);
  assert.equal(changed.field.predicted["1"].auto, 22);
  assert.equal(changed.field.predicted["1"].teleop, 88);
  assert.equal(changed.field.predicted["2"].auto, 5);
  assert.deepEqual(changed.field.predicted["3"], source.field.predicted["3"]);
  assert.deepEqual(changed.progress, source.progress);
  assert.equal(source.field.predicted["1"].auto, 20);
  assert.equal(changed.seed, source.seed);
  assert.equal(changed.min_iterations, changed.max_iterations);
  assert.throws(() => scenarioSnapshot(source, { "4": 100 }, 1000));
  assert.throws(() => scenarioSnapshot(source, { constructor: 100 }, 1000));
  assert.throws(() => scenarioSnapshot(source, { "1": NaN }, 1000));
  assert.throws(() => scenarioSnapshot(source, { "1": 100 }, 1e6));
  assert.deepEqual(scenarioSnapshot(source, {}, 1000).field, source.field);
});

test("component improvements apply after team scaling, including zero-rated capabilities", () => {
  const source = {
    field: { predicted: { "1": { auto: 0, teleop: 80 }, "2": { auto: 10 } } },
    progress: { played: [true] },
  } as unknown as Snapshot;
  const result = scenarioSnapshot(source, { "1": 150 }, 1000, {
    "1": { auto: 1.25, teleop: 5.5 },
  });
  assert.deepEqual(result.field.predicted["1"], { auto: 1.25, teleop: 125.5 });
  assert.deepEqual(result.field.predicted["2"], source.field.predicted["2"]);
  assert.deepEqual(result.progress, source.progress);
  assert.equal(source.field.predicted["1"].auto, 0);
  assert.throws(() =>
    scenarioSnapshot(source, {}, 1000, { "1": { invented: 5 } }),
  );
  for (const points of [-1, 100.01, NaN, Infinity])
    assert.throws(() =>
      scenarioSnapshot(source, {}, 1000, { "1": { auto: points } }),
    );
});

test("shared links retain component improvements and valid playoff comparisons", () => {
  const event = {
    source_snapshot_sha256: "snapshot",
    engine_sha256: "engine",
    snapshot: {
      field: {
        event_type: "Qualifier",
        predicted: {
          "1": { auto: 10 },
          "2": { auto: 20 },
          "3": { auto: 30 },
          "4": { auto: 40 },
        },
      },
      rule_pack: {
        structure: {
          alliance_brackets: [{ min_teams: 4, max_teams: 10, alliances: 2 }],
        },
      },
      progress: null,
    },
  } as unknown as PublishedEvent;
  const scenario = {
    strengths: {},
    components: { "1": { auto: 1.25 } },
    iterations: 1000,
    comparison: {
      captain: "1",
      partners: ["2", "3"] as [string, string],
      seed: 2,
    },
  };
  const url = new URL(
    scenarioUrl("https://example.test/events/2025/A", event, scenario),
  );
  assert.deepEqual(scenarioFromUrl(url.search, event), scenario);
  assert.throws(() =>
    validateComparison(event.snapshot, {
      ...scenario.comparison,
      partners: ["1", "2"],
    }),
  );
  assert.throws(() =>
    validateComparison(event.snapshot, { ...scenario.comparison, seed: 3 }),
  );
  assert.throws(() =>
    validateComparison(
      { ...event.snapshot, progress: { played: [], decided: [{}] } },
      scenario.comparison,
    ),
  );
});

test("scenario links round-trip inputs and reject malformed or mismatched publications", () => {
  const event = {
    source_snapshot_sha256: "snapshot1",
    engine_sha256: "engine1",
    snapshot: {
      field: { predicted: { "1": { auto: 10 }, "2": { auto: 20 } } },
    },
  } as unknown as PublishedEvent;
  const url = new URL(
    scenarioUrl("https://example.test/events/2025/A#notes", event, {
      strengths: { "1": 110, "2": 100 },
      iterations: 5000,
    }),
  );
  assert.equal(url.pathname, "/events/2025/A");
  assert.equal(url.hash, "");
  assert.throws(
    () =>
      scenarioUrl(`https://example.test/?padding=${"x".repeat(16000)}`, event, {
        strengths: {},
        iterations: 1000,
      }),
    /too large/,
  );
  assert.deepEqual(scenarioFromUrl(url.search, event), {
    strengths: { "1": 110 },
    iterations: 5000,
  });
  assert.equal(scenarioFromUrl("", event), null);
  assert.throws(() => scenarioFromUrl("?scenario=%7Bbad", event), /invalid/);
  assert.throws(
    () => scenarioFromUrl(url.search, { ...event, engine_sha256: "engine2" }),
    /different/,
  );
  assert.throws(
    () =>
      scenarioFromUrl(url.search, {
        ...event,
        source_snapshot_sha256: "snapshot2",
      }),
    /different/,
  );
  const packet = JSON.parse(url.searchParams.get("scenario")!);
  for (const strengths of [
    null,
    [],
    { "1": 155 },
    { "1": "110" },
    { "1": 107 },
    { "99": 110 },
  ]) {
    url.searchParams.set("scenario", JSON.stringify({ ...packet, strengths }));
    assert.throws(() => scenarioFromUrl(url.search, event));
  }
  url.searchParams.set(
    "scenario",
    JSON.stringify({ ...packet, iterations: 1e9 }),
  );
  assert.throws(() => scenarioFromUrl(url.search, event));
  url.searchParams.set("scenario", JSON.stringify({ ...packet, v: 2 }));
  assert.throws(() => scenarioFromUrl(url.search, event), /invalid/);
});

test("comparison labels distinguish rank and percentage-point changes without negative zero", () => {
  assert.equal(forecastChange(2, 3.2), "-1.2");
  assert.equal(forecastChange(0.35, 0.2, true), "+15.0 pp");
  assert.equal(forecastChange(0.199999, 0.2, true), "0.0 pp");
  assert.equal(forecastChange(null, 2), "—");
});

test("ranking handles absent means, deterministic ties and unavailable probabilities", () => {
  const entries = rankedTeams({
    teams: {
      "10": { mean_rank: 2 },
      "2": { mean_rank: 2 },
      "1": { mean_rank: null },
    },
  } as any);
  assert.deepEqual(
    entries.map(([team]) => team),
    ["2", "10", "1"],
  );
  assert.equal(percent(null), "—");
  assert.equal(percent(0), "0.0%");
});
