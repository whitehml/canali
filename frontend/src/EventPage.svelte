<script lang="ts">
  import { onMount } from "svelte";
  import AutoLeave from "./AutoLeave.svelte";
  import {
    percent,
    rankedTeams,
    scenarioSnapshot,
    scenarioUrl,
    scenarioFromUrl,
    forecastChange,
    componentLabel,
    allianceCount,
    validateComparison,
    type ComponentChanges,
    type PartnerComparison,
    type PartnerResults,
    formatDate,
    forecastBasis,
    type Forecast,
    type PublishedEvent,
  } from "./model";

  let { event, region, regionName }: { event: PublishedEvent; region: string; regionName: string } = $props();
  let team = $state("");
  let strengths = $state<Record<string, number>>({});
  let components = $state<ComponentChanges>({});
  let captain = $state("");
  let partnerA = $state("");
  let partnerB = $state("");
  let allianceSeed = $state(1);
  let runKind = $state<"forecast" | "partners">("forecast");
  let partnerResult = $state<
    | (PartnerResults & {
        comparison: PartnerComparison;
        label: string;
        key: string;
      })
    | null
  >(null);
  let iterations = $state(1000);
  let running = $state(false);
  let leaveRunning = $state(false);
  let progress = $state(0);
  let message = $state("");
  let simulationError = $state("");
  let custom = $state<Forecast | null>(null);
  let customLabel = $state("");
  let viewingCustom = $state(false);
  let customKey = $state("");
  let sharedLink = $state("");
  let shareMessage = $state("");
  let linkMessage = $state("");
  let shareTarget = $state<"forecast" | "partners">("forecast");
  const draftKey = $derived(
    JSON.stringify([strengths, components, iterations]),
  );
  const editedTeams = $derived(
    [...new Set([...Object.keys(strengths), ...Object.keys(components)])].sort(
      (a, b) => Number(a) - Number(b),
    ),
  );
  const edits = $derived(
    editedTeams.map(
      (number) => [number, strengths[number] ?? 100] as [string, number],
    ),
  );
  const comparisonInput = $derived({
    captain,
    partners: [partnerA, partnerB] as [string, string],
    seed: allianceSeed,
  });
  const comparisonKey = $derived(JSON.stringify([draftKey, comparisonInput]));
  const comparisonUnavailable = $derived(
    event.snapshot.field.event_type === "FIRST Championship" ||
      !!event.snapshot.progress?.decided?.length,
  );
  const stale = $derived(custom !== null && customKey !== draftKey);
  let worker: Worker | null = null;
  const shown = $derived(viewingCustom && custom ? custom : event.forecast);
  const rows = $derived(shown ? rankedTeams(shown) : []);
  const total = $derived(
    event ? Object.keys(event.snapshot.field.predicted).length : 0,
  );
  const played = $derived(
    event?.snapshot.progress?.played.filter(Boolean).length ?? 0,
  );
  const scheduled = $derived(event?.snapshot.schedule?.stations.length ?? null);

  onMount(() => {
    team = rankedTeams(event.forecast)[0][0];
    const ordered = rankedTeams(event.forecast).map(([number]) => number);
    captain = ordered[0];
    partnerA = ordered[1];
    partnerB = ordered[2];
    try {
      const shared = scenarioFromUrl(location.search, event);
      if (shared) {
        strengths = shared.strengths;
        components = shared.components ?? {};
        iterations = shared.iterations;
        team = Object.keys(strengths)[0] ?? Object.keys(components)[0] ?? team;
        if (shared.comparison) {
          captain = shared.comparison.captain;
          [partnerA, partnerB] = shared.comparison.partners;
          allianceSeed = shared.comparison.seed;
        }
        linkMessage = shared.comparison
          ? "Shared partner comparison loaded. Review the adjustments, then compare partners below."
          : "Shared scenario loaded. Review the adjustments, then run it on your device.";
      }
    } catch (error) {
      simulationError = error instanceof Error ? error.message : String(error);
    }
    return () => {
      worker?.terminate();
    };
  });

  function inputLabel() {
    return editedTeams.length
      ? editedTeams
          .map((number) => {
            const changes = Object.entries(components[number] ?? {}).map(
              ([name, points]) => `${componentLabel(name)} +${points} points`,
            );
            return `Team ${number}: ${strengths[number] ?? 100}%${changes.length ? `, ${changes.join(", ")}` : ""}`;
          })
          .join(" · ")
      : "Published team strengths";
  }

  function runScenario(comparison?: PartnerComparison) {
    if (!event || running || leaveRunning) return;
    simulationError = "";
    runKind = comparison ? "partners" : "forecast";
    try {
      if (comparison)
        comparison = validateComparison(event.snapshot, comparison);
      const snapshot = scenarioSnapshot(
        $state.snapshot(event.snapshot),
        $state.snapshot(strengths),
        iterations,
        $state.snapshot(components),
      );
      const label = inputLabel();
      const key = comparison ? comparisonKey : draftKey;
      const chosen = comparison ? structuredClone(comparison) : null;
      running = true;
      progress = 0;
      message = worker
        ? "Starting your simulation…"
        : "Preparing your first simulation…";
      worker ??= new Worker(
        new URL("./simulation.worker.ts", import.meta.url),
        { type: "module" },
      );
      worker.onerror = () => {
        running = false;
        simulationError =
          "The simulation worker stopped. Try again, or use a device with more available memory.";
        worker?.terminate();
        worker = null;
      };
      worker.onmessage = ({ data }) => {
        if (data.kind === "loading") message = data.message;
        if (data.kind === "error") {
          running = false;
          simulationError = data.message;
        }
        if (data.kind === "progress") {
          progress = data.iterations;
          message = `${progress.toLocaleString()} of ${iterations.toLocaleString()} simulations`;
          if (data.status === "complete") {
            if (chosen) {
              partnerResult = {
                ...data.result,
                comparison: chosen,
                label,
                key,
              };
            } else {
              custom = data.result;
              customLabel = label;
              customKey = key;
              viewingCustom = true;
            }
            running = false;
          } else if (data.status === "exhausted") {
            simulationError =
              "The simulation reached its limit without a result.";
            running = false;
          }
        }
      };
      worker.postMessage({
        snapshot,
        engineUrl: event.engine_url,
        engineHash: event.engine_sha256,
        comparison: chosen,
      });
    } catch (error) {
      running = false;
      simulationError = error instanceof Error ? error.message : String(error);
    }
  }

  function cancel() {
    worker?.terminate();
    worker = null;
    running = false;
    message = "Simulation cancelled.";
  }

  function setStrength(number: string, value: number) {
    const updated = { ...strengths };
    if (value === 100) delete updated[number];
    else updated[number] = value;
    strengths = updated;
    sharedLink = "";
    shareMessage = "";
  }

  function setComponent(number: string, component: string, value: number) {
    const values = { ...(components[number] ?? {}) };
    if (value === 0) delete values[component];
    else values[component] = value;
    const updated = { ...components };
    if (Object.keys(values).length)
      updated[number] = Object.fromEntries(
        Object.entries(values).sort(([a], [b]) => a.localeCompare(b)),
      );
    else delete updated[number];
    components = updated;
    sharedLink = "";
    shareMessage = "";
  }

  function resetTeam(number: string) {
    setStrength(number, 100);
    const updated = { ...components };
    delete updated[number];
    components = updated;
  }

  function resetAll() {
    strengths = {};
    components = {};
    partnerResult = null;
    const ordered = rankedTeams(event.forecast).map(([number]) => number);
    captain = ordered[0];
    partnerA = ordered[1];
    partnerB = ordered[2];
    allianceSeed = 1;
    iterations = 1000;
    custom = null;
    customKey = "";
    viewingCustom = false;
    message = "";
    simulationError = "";
    linkMessage = "";
    sharedLink = "";
    shareMessage = "";
  }

  async function shareScenario(comparison?: PartnerComparison) {
    simulationError = "";
    try {
      shareTarget = comparison ? "partners" : "forecast";
      sharedLink = scenarioUrl(location.href, event, {
        strengths,
        components,
        iterations,
        ...(comparison ? { comparison } : {}),
      });
      try {
        await navigator.clipboard.writeText(sharedLink);
        shareMessage = "Scenario link copied.";
      } catch {
        shareMessage = "Select and copy the link below.";
      }
    } catch (error) {
      runKind = comparison ? "partners" : "forecast";
      simulationError = error instanceof Error ? error.message : String(error);
    }
  }

  function clearShare() {
    sharedLink = "";
    shareMessage = "";
  }
</script>

<svelte:head
  ><title>{event ? `${event.name} · Canali` : "Canali · FTC forecasts"}</title
  ></svelte:head
>

<main>
  <p class="breadcrumb">
    <a href={`/seasons/${event.season}/${region}`}
      >{regionName} / {event.season}–{String(event.season + 1).slice(2)}</a
    >
    / {event.type}
  </p>
  <section class="event-heading" aria-labelledby="event-title">
    <div>
      <p class="eyebrow">
        FTC / {event.snapshot.rule_pack.season}–{String(
          event.snapshot.rule_pack.season + 1,
        ).slice(2)} / {event.snapshot.rule_pack.game}
      </p>
      <h1 id="event-title">{event.name}</h1>
      <p class="intro">
        {event.city}{event.venue ? ` · ${event.venue}` : ""} · {formatDate(
          event.date_start,
        )}
      </p>
    </div>
    <div class="published">
      <span class="status-dot" aria-hidden="true"></span> {forecastBasis(event.snapshot)}<br
      /><span
        >Published {new Date(event.published_at).toLocaleString(undefined, {
          dateStyle: "medium",
          timeStyle: "short",
        })}</span
      >
    </div>
  </section>

  <p class="data-label"><span aria-hidden="true">◈</span> {event.label}</p>

  <section class="metrics" aria-label="Event summary">
    <div>
      <span class="eyebrow">THE FIELD</span><strong
        >{total}<small>teams</small></strong
      >
    </div>
    <div>
      <span class="eyebrow">QUALIFICATION PROGRESS</span><strong
        >{played}<small
          >{scheduled === null
            ? "schedule not published"
            : `of ${scheduled} matches played`}</small
        ></strong
      >
    </div>
    <div>
      <span class="eyebrow">SIMULATIONS</span><strong
        >{shown.iterations.toLocaleString()}<small
          >{viewingCustom ? "your scenario" : "shared forecast"}</small
        ></strong
      >
    </div>
  </section>

  <div class="workspace">
    <section
      id="forecast"
      class="forecast-panel"
      aria-labelledby="forecast-title"
    >
      <div class="panel-heading">
        <div>
          <p class="eyebrow">THE OUTLOOK</p>
          <h2 id="forecast-title">Qualification & beyond</h2>
        </div>
        <span class="count-badge">{total} teams</span>
      </div>
      <div class="view-tabs" aria-label="Forecast view">
        <button
          class:chosen={!viewingCustom}
          aria-pressed={!viewingCustom}
          onclick={() => (viewingCustom = false)}>Shared forecast</button
        >
        <button
          class:chosen={viewingCustom}
          aria-pressed={viewingCustom}
          disabled={!custom}
          onclick={() => (viewingCustom = true)}
          >Your scenario {#if custom}<span aria-hidden="true">↗</span
            >{/if}</button
        >
      </div>
      {#if viewingCustom}<p class="scenario-label">
          {customLabel} · calculated on your device
        </p>
        {#if stale}<p class="draft-notice">
            These results are from your last run. Run the edited inputs to
            update them.
          </p>{/if}
      {/if}
      <div class="table-scroll">
        <table>
          <caption class="sr-only"
            >{viewingCustom ? "Custom" : "Shared"} team forecast, ordered by average
            qualification rank</caption
          >
          <thead
            ><tr
              ><th scope="col">Team</th><th scope="col">Avg. rank</th><th
                scope="col">Captain</th
              ><th scope="col">Picked</th><th scope="col">Win event</th><th
                scope="col">Advance</th
              ></tr
            ></thead
          >
          <tbody>
            {#each rows as [number, summary] (number)}
              <tr class:selected={team === number}>
                <th scope="row"
                  ><button
                    class="team-button"
                    title={event.teams[number]}
                    disabled={running || leaveRunning}
                    onclick={() => (team = number)}
                    ><span class="team-symbol" aria-hidden="true">↗</span>
                    {number}<span class="team-name">{event.teams[number]}</span
                    ></button
                  ></th
                >
                <td class="rank-value"
                  ><span class="metric-value"
                    >{summary.mean_rank?.toFixed(1) ?? "—"}</span
                  >
                  {#if viewingCustom}<span
                      class="comparison-delta"
                      aria-label="Rank change"
                      >{forecastChange(
                        summary.mean_rank,
                        event.forecast.teams[number].mean_rank,
                      )}</span
                    >{/if}</td
                >
                <td
                  ><span class="metric-value">{percent(summary.p_captain)}</span
                  >
                  {#if viewingCustom}<span
                      class="comparison-delta"
                      aria-label="Captain probability change"
                      >{forecastChange(
                        summary.p_captain,
                        event.forecast.teams[number].p_captain,
                        true,
                      )}</span
                    >{/if}</td
                ><td>{percent(summary.p_selected)}</td>
                <td class="win-value"
                  ><span class="metric-value">{percent(summary.p_win)}</span>
                  {#if viewingCustom}<span
                      class="comparison-delta"
                      aria-label="Win probability change"
                      >{forecastChange(
                        summary.p_win,
                        event.forecast.teams[number].p_win,
                        true,
                      )}</span
                    >{/if}</td
                ><td>{percent(summary.p_advance)}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
      <p class="table-note">
        Average rank is a simulation average, not a current standing. Select a
        team to explore a scenario.
        {#if viewingCustom}Changes are scenario minus shared forecast; lower
          ranks are better. Probability changes are in percentage points (pp).{/if}
      </p>
    </section>

    <aside class="scenario-panel" aria-labelledby="scenario-title">
      <div class="scenario-icon" aria-hidden="true">↗</div>
      <p class="eyebrow">CHANGE AN ASSUMPTION</p>
      <h2 id="scenario-title">What if?</h2>
      <p class="scenario-intro">
        Adjust several teams to explore how their performance could change the
        event.
      </p>
      {#if linkMessage}<p class="scenario-link-notice" role="status">
          {linkMessage}
        </p>{/if}
      <form
        onsubmit={(e) => {
          e.preventDefault();
          runScenario();
        }}
      >
        <label for="team">Team</label>
        <select id="team" bind:value={team} disabled={running || leaveRunning}
          >{#each Object.keys(event.snapshot.field.predicted).sort((a, b) => Number(a) - Number(b)) as number}<option
              value={number}>{number} · {event.teams[number]}</option
            >{/each}</select
        >
        <div class="strength-label">
          <label for="strength">Predicted strength</label><output for="strength"
            >{strengths[team] ?? 100}%</output
          >
        </div>
        <input
          id="strength"
          type="range"
          min="50"
          max="150"
          step="5"
          value={strengths[team] ?? 100}
          oninput={(e) => setStrength(team, Number(e.currentTarget.value))}
          disabled={running || leaveRunning}
        />
        <div class="range-labels">
          <span>50%</span><span>Published: 100%</span><span>150%</span>
        </div>
        <p class="field-note">
          Scales this team’s scoring components equally. Played matches stay
          fixed. Select another team to add an adjustment.
        </p>
        <details class="component-editor">
          <summary>Improve individual components</summary>
          <p class="field-note">
            Add predicted points after the overall strength adjustment. Each
            change applies to this team's future performance.
          </p>
          {#each Object.entries(event.snapshot.field.predicted[team] ?? {}) as [name, base] (name)}
            <div class="component-row">
              <label for={`component-${name}`}
                >{componentLabel(name)}<small
                  >Published {base.toFixed(1)} → Scenario {(
                    (base * (strengths[team] ?? 100)) / 100 +
                    (components[team]?.[name] ?? 0)
                  ).toFixed(1)}</small
                ></label
              >
              <div>
                <input
                  id={`component-${name}`}
                  type="number"
                  min="0"
                  max="100"
                  step="any"
                  value={components[team]?.[name] ?? 0}
                  disabled={running || leaveRunning}
                  oninput={(e) =>
                    setComponent(
                      team,
                      name,
                      e.currentTarget.value === ""
                        ? 0
                        : e.currentTarget.valueAsNumber,
                    )}
                /><span>extra points</span>
              </div>
            </div>
          {/each}
        </details>
        <div class="adjustment-heading">
          <span>Adjusted teams · {edits.length}</span>
          <button
            type="button"
            class="text-button"
            onclick={resetAll}
            disabled={running || leaveRunning}>Reset scenario</button
          >
        </div>
        {#if edits.length}<ul class="adjustments" aria-label="Adjusted teams">
            {#each edits as [number, value] (number)}<li>
                <span title={event.teams[number]}
                  ><strong>{number}</strong> · {value}%
                  {#each Object.entries(components[number] ?? {}) as [name, points]}<small
                      class="adjusted-component"
                      >{componentLabel(name)} +{points}</small
                    >{/each}</span
                >
                <button
                  type="button"
                  class="text-button"
                  aria-label={`Reset team ${number}`}
                  disabled={running || leaveRunning}
                  onclick={() => resetTeam(number)}>Reset</button
                >
              </li>{/each}
          </ul>{:else}<p class="field-note">
            All teams use their published strength.
          </p>{/if}
        <label for="iterations">Simulations</label>
        <select
          id="iterations"
          bind:value={iterations}
          disabled={running || leaveRunning}
          onchange={() => {
            sharedLink = "";
            shareMessage = "";
          }}
          ><option value={1000}>1,000 · quick exploration</option><option
            value={5000}>5,000 · more precision</option
          ></select
        >
        <button
          class="run-button"
          type="submit"
          disabled={running || leaveRunning}
          >{running ? "Simulating…" : "Run on my device"}<span
            aria-hidden="true">↗</span
          ></button
        >
        {#if running && runKind === "forecast"}<progress
            max={iterations}
            value={progress}
            aria-label="Simulation progress"
          ></progress><button
            class="cancel-button"
            type="button"
            onclick={cancel}>Cancel simulation</button
          >{/if}
      </form>
      <div class="share-scenario">
        <button
          class="share-button"
          type="button"
          disabled={running || leaveRunning}
          onclick={() => shareScenario()}>Share these inputs</button
        >
        <p>
          Links share adjustments and simulation count. Open a link to review
          the inputs before running.
        </p>
        {#if sharedLink && shareTarget === "forecast"}<label
            class="sr-only"
            for="scenario-link">Scenario link</label
          ><input
            id="scenario-link"
            readonly
            value={sharedLink}
            onclick={(e) => e.currentTarget.select()}
          />{/if}
        <p role="status">{shareTarget === "forecast" ? shareMessage : ""}</p>
      </div>
      <p class="progress-message" role="status" aria-live="polite">
        {runKind === "forecast" ? message : ""}
      </p>
      {#if simulationError && runKind === "forecast"}<p
          class="error-message"
          role="alert"
        >
          {simulationError}
        </p>{/if}
      <div class="device-note">
        <span aria-hidden="true">⊙</span>
        <p>
          Calculations stay on this device. Your first run downloads Python and
          its numerical libraries; this may take a moment.
        </p>
      </div>
    </aside>
  </div>

  <section class="partner-panel" aria-labelledby="partner-title">
    <div class="panel-heading">
      <div>
        <p class="eyebrow">PLAN AN ALLIANCE</p>
        <h2 id="partner-title">Compare playoff partners</h2>
      </div>
      <span class="count-badge">Runs on your device</span>
    </div>
    <p class="partner-intro">
      Compare two partners assuming your team captains the chosen seed. Your
      pair is reserved first; the pick model forms the other alliances from the
      remaining teams in simulated qualification order. This does not estimate
      whether a partner will be available at your pick. Playoff scoring adds the
      teams' predicted contributions; robot-role compatibility is not modeled.
    </p>
    <p class="field-note">{event.label}</p>
    {#if comparisonUnavailable}<p class="field-note">
        Use a two-team-alliance event snapshot from before playoff matches were
        played.
      </p>{:else}
      <form
        class="partner-form"
        onsubmit={(e) => {
          e.preventDefault();
          runScenario(comparisonInput);
        }}
      >
        <div>
          <label for="partner-captain">Your captain</label><select
            id="partner-captain"
            bind:value={captain}
            onchange={clearShare}
            disabled={running || leaveRunning}
            >{#each Object.keys(event.teams) as number}<option value={number}
                >{number} · {event.teams[number]}</option
              >{/each}</select
          >
        </div>
        <div>
          <label for="partner-a">Partner A</label><select
            id="partner-a"
            bind:value={partnerA}
            onchange={clearShare}
            disabled={running || leaveRunning}
            >{#each Object.keys(event.teams) as number}<option value={number}
                >{number} · {event.teams[number]}</option
              >{/each}</select
          >
        </div>
        <div>
          <label for="partner-b">Partner B</label><select
            id="partner-b"
            bind:value={partnerB}
            onchange={clearShare}
            disabled={running || leaveRunning}
            >{#each Object.keys(event.teams) as number}<option value={number}
                >{number} · {event.teams[number]}</option
              >{/each}</select
          >
        </div>
        <div>
          <label for="alliance-seed">Assumed alliance seed</label><select
            id="alliance-seed"
            bind:value={allianceSeed}
            onchange={clearShare}
            disabled={running || leaveRunning}
            >{#each Array.from({ length: allianceCount(event.snapshot) }, (_, i) => i + 1) as seed}<option
                value={seed}>Seed {seed}</option
              >{/each}</select
          >
        </div>
        <p class="field-note partner-settings">
          Uses the team and component adjustments above and {iterations.toLocaleString()}
          simulations per partner. Changing partners redraws the opposing alliances.
          Published alliance selection is replaced for this hypothetical comparison;
          played qualification results stay fixed.
        </p>
        <button
          class="run-button"
          type="submit"
          disabled={running || leaveRunning}
          >Compare partners<span aria-hidden="true">↗</span></button
        >
        <button
          class="share-button"
          type="button"
          disabled={running || leaveRunning}
          onclick={() => shareScenario(comparisonInput)}
          >Share this comparison</button
        >
      </form>
      {#if runKind === "partners"}<p class="progress-message" role="status">
          {message}
        </p>{/if}
      {#if running && runKind === "partners"}<progress
          max={iterations}
          value={progress}
          aria-label="Partner comparison progress"
        ></progress><button class="cancel-button" type="button" onclick={cancel}
          >Cancel partner comparison</button
        >{/if}
      {#if simulationError && runKind === "partners"}<p
          class="error-message"
          role="alert"
        >
          {simulationError}
        </p>{/if}
      {#if sharedLink && shareTarget === "partners"}<div
          class="comparison-share"
        >
          <label for="comparison-link">Comparison link</label><input
            id="comparison-link"
            readonly
            value={sharedLink}
            onclick={(e) => e.currentTarget.select()}
          />
          <p role="status">{shareMessage}</p>
        </div>{/if}
      {#if partnerResult}
        <p class="scenario-label">
          Captain {partnerResult.comparison.captain}, seed {partnerResult
            .comparison.seed} · {partnerResult.label} · {partnerResult.iterations.toLocaleString()}
          simulations per partner
        </p>
        {#if partnerResult.key !== comparisonKey}<p class="draft-notice">
            These comparison results are from your last run. Compare again to
            apply the edited inputs.
          </p>{/if}
        <div class="table-scroll">
          <table class="partner-results">
            <caption class="sr-only"
              >Playoff partner comparison conditional on the chosen pairing and
              seed</caption
            ><thead
              ><tr
                ><th scope="col">Partner</th><th scope="col">Win playoff</th><th
                  scope="col">Reach final</th
                ><th scope="col">Avg. finish</th><th scope="col"
                  >Sampling error</th
                ></tr
              ></thead
            ><tbody>
              {#each partnerResult.partners as result (result.partner)}<tr
                  ><th scope="row"
                    >{result.partner}<span class="team-name"
                      >{event.teams[result.partner]}</span
                    ></th
                  ><td>{percent(result.p_win)}</td><td
                    >{percent(result.p_final)}</td
                  ><td>{result.mean_place.toFixed(2)}</td><td
                    >{(result.standard_error * 100).toFixed(1)} pp</td
                  ></tr
                >{/each}
            </tbody>
          </table>
        </div>
        <p class="table-note">
          These are hypothetical playoff chances given your pairing and seed,
          distinct from the shared event forecast. Sampling error is the larger
          standard error of the win and final probabilities; small differences
          may be noise.
        </p>
      {/if}
    {/if}
  </section>

  <AutoLeave
    {event}
    disabled={running}
    onbusy={(busy) => {
      leaveRunning = busy;
      if (busy) {
        worker?.terminate();
        worker = null;
      }
    }}
  />

  <section id="notes" class="reading-notes" aria-labelledby="notes-title">
    <div>
      <p class="eyebrow">READING THE FORECAST</p>
      <h2 id="notes-title">Probabilities, not promises.</h2>
    </div>
    <div>
      <p>
        <strong>Captain / Picked</strong> estimate alliance selection.
        <strong>Win event</strong>
        estimates a playoff victory. <strong>Advance</strong> is shown only when advancement
        slots are supplied.
      </p>
      <p>
        Judged awards and selection behavior are modeled assumptions. Smaller
        differences may be simulation noise: this result’s largest tracked
        standard error is <strong
          >{(shown.standard_error * 100).toFixed(1)} percentage points</strong
        >.
      </p>
    </div>
  </section>
</main>
