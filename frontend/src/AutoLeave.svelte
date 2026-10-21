<script lang="ts">
  import { onDestroy, onMount } from "svelte";
  import { hasRemainingQualifications, percent, type PublishedEvent } from "./model";

  let {
    event,
    disabled = false,
    onbusy,
  }: {
    event: PublishedEvent;
    disabled?: boolean;
    onbusy: (busy: boolean) => void;
  } = $props();
  let team = $state("");
  const hasRemaining = $derived(hasRemainingQualifications(event.snapshot, team));
  onMount(() => {
    team = Object.keys(event.teams)[0];
  });
  let current = $state(0);
  let improved = $state(100);
  let iterations = $state(1000);
  let running = $state(false);
  let message = $state("");
  let error = $state("");
  type Result = {
    iterations: number;
    mean_rank: [number, number];
    rank_delta_se: number;
    matches: {
      match: number;
      partner: number;
      surrogate: boolean;
      current: number[];
      improved: number[];
    }[];
  };
  let result = $state<{
    report: Result;
    team: string;
    current: number;
    improved: number;
    key: string;
  } | null>(null);
  const key = $derived(JSON.stringify([team, current, improved, iterations]));
  let worker: Worker | null = null;
  onDestroy(() => worker?.terminate());

  function stop() {
    worker?.terminate();
    worker = null;
    running = false;
    onbusy(false);
  }

  function run() {
    if (disabled || running || !hasRemaining) return;
    error = "";
    if (
      ![current, improved].every(
        (v) => Number.isFinite(v) && v >= 0 && v <= 100,
      ) ||
      improved < current
    ) {
      error =
        "Use success rates from 0 to 100%, with the improved rate at least the current rate.";
      return;
    }
    const chosen = { team, current, improved, key };
    running = true;
    onbusy(true);
    message = "Preparing Python…";
    worker = new Worker(new URL("./simulation.worker.ts", import.meta.url), {
      type: "module",
    });
    worker.onerror = () => {
      error =
        "The simulation stopped. Try again on a device with more available memory.";
      stop();
    };
    worker.onmessage = ({ data }) => {
      if (data.kind === "loading") message = data.message;
      if (data.kind === "error") {
        error = data.message;
        stop();
      }
      if (data.kind === "progress") {
        message = `${data.iterations.toLocaleString()} of ${iterations.toLocaleString()} paired simulations`;
        if (data.status === "complete") {
          result = { ...chosen, report: data.result };
          stop();
        }
      }
    };
    worker.postMessage({
      engineUrl: event.engine_url,
      engineHash: event.engine_sha256,
      snapshot: {
        ...$state.snapshot(event.snapshot),
        min_iterations: iterations,
        max_iterations: iterations,
      },
      leave: {
        team: Number(team),
        current: current / 100,
        improved: improved / 100,
      },
    });
  }

  function delta(before: number, after: number, digits = 2) {
    const value = after - before;
    return `${value > 0 ? "+" : ""}${value.toFixed(digits)}`;
  }
</script>

{#if event.snapshot.rule_pack.season === 2025 && event.snapshot.schedule}
  <section class="partner-panel leave-panel" aria-labelledby="leave-title">
    <div class="panel-heading">
      <div>
        <p class="eyebrow">SMALL CHANGE, MEASURABLE PAYOFF</p>
        <h2 id="leave-title">What if we reliably leave in auto?</h2>
      </div>
    </div>
    <p class="partner-intro">
      Compare a team's current LEAVE success rate with an attainable
      improvement. DECODE awards 3 points for leaving; these also count toward
      the Movement RP threshold. Other autonomous capabilities and every teleop
      outcome stay the same across each paired trial.
    </p>
    <p class="field-note">
      {event.label}. This uses published strengths, independently of the
      adjustments above. Enter your observed current rate; it is not inferred
      from EPA.
    </p>
    <form
      class="partner-form"
      onsubmit={(e) => {
        e.preventDefault();
        run();
      }}
    >
      <div>
        <label for="leave-team">Team</label><select
          id="leave-team"
          bind:value={team}
          disabled={disabled || running}
        >
          {#each Object.keys(event.teams) as number}<option value={number}
              >{number} · {event.teams[number]}</option
            >{/each}
        </select>
      </div>
      <div>
        <label for="leave-current">Current LEAVE success (%)</label><input
          id="leave-current"
          type="number"
          min="0"
          max="100"
          step="1"
          required
          bind:value={current}
          disabled={disabled || running}
        />
      </div>
      <div>
        <label for="leave-improved">Improved LEAVE success (%)</label><input
          id="leave-improved"
          type="number"
          min="0"
          max="100"
          step="1"
          required
          bind:value={improved}
          disabled={disabled || running}
        />
      </div>
      <div>
        <label for="leave-iterations">Paired simulations</label><select
          id="leave-iterations"
          bind:value={iterations}
          disabled={disabled || running}
          ><option value={1000}>1,000</option><option value={5000}>5,000</option
          ></select
        >
      </div>
      <button class="run-button" type="submit" disabled={disabled || running || !hasRemaining}
        >Compare LEAVE improvement<span aria-hidden="true">↗</span></button
      >
    </form>
    {#if team && !hasRemaining}
      <p role="status" class="field-note">
        Team {team} has no remaining qualification matches in this snapshot.
        This comparison only simulates remaining qualification matches;
        playoff LEAVE comparisons are not supported yet.
      </p>
    {/if}
    <p class="field-note">
      Model assumption: other robots' LEAVE rates use their leave EPA divided by
      3, clipped to 0–100%. Alliance BASE points are drawn between the two
      nearest legal totals (0, 5, 10, 15 or 30) to match the published
      base-point estimate, clipped to 0–30. These are estimates, not observed
      robot-state frequencies. Movement RP uses the game's actual threshold;
      other bonus RPs retain their existing models.
    </p>
    <p class="table-note">
      Qualifications only. Played results stay fixed. Surrogate matches affect
      alliance outcomes but do not count toward this team's ranking score. The
      two scenarios use this action model for a fair comparison; their
      current-rate result can differ from the shared forecast.
    </p>
    {#if message}<p role="status" class="progress-message">{message}</p>{/if}
    {#if running}<button
        class="cancel-button"
        type="button"
        onclick={() => {
          stop();
          message = "Comparison cancelled.";
        }}>Cancel LEAVE comparison</button
      >{/if}
    {#if error}<p role="alert" class="error-message">{error}</p>{/if}
    {#if result}
      <p class="scenario-label">
        Team {result.team} · LEAVE {result.current}% → {result.improved}% · {result.report.iterations.toLocaleString()}
        paired simulations
      </p>
      {#if result.key !== key}<p class="draft-notice">
          These results are from your last run. Compare again to apply edited
          inputs.
        </p>{/if}
      <p class="leave-summary">
        Expected added points per remaining appearance: <strong
          >{((3 * (result.improved - result.current)) / 100).toFixed(2)}</strong
        >. Average qualification rank:
        <strong
          >{result.report.mean_rank[0].toFixed(2)} → {result.report.mean_rank[1].toFixed(
            2,
          )}</strong
        >
        (lower is better). Rank-change sampling error: {result.report.rank_delta_se.toFixed(
          3,
        )} ranks.
      </p>
      <div class="table-scroll">
        <table class="partner-results">
          <caption>Remaining matches · current → improved</caption>
          <thead
            ><tr
              ><th scope="col">Match / partner</th><th scope="col"
                >Alliance points</th
              ><th scope="col">Movement RP chance</th><th scope="col"
                >Movement RP gain</th
              ><th scope="col">Total RP</th></tr
            ></thead
          >
          <tbody
            >{#each [...result.report.matches].sort((a, b) => b.improved[1] - b.current[1] - (a.improved[1] - a.current[1]) || a.match - b.match) as row}
              <tr
                ><th scope="row"
                  >Q{row.match} · {row.partner}{row.surrogate
                    ? " (surrogate)"
                    : ""}</th
                ><td
                  >{row.current[0].toFixed(1)} → {row.improved[0].toFixed(
                    1,
                  )}</td
                ><td>{percent(row.current[1])} → {percent(row.improved[1])}</td
                ><td
                  >{delta(row.current[1] * 100, row.improved[1] * 100, 1)} pp</td
                ><td
                  >{row.current[2].toFixed(2)} → {row.improved[2].toFixed(
                    2,
                  )}</td
                ></tr
              >
            {/each}</tbody
          >
        </table>
      </div>
      <p class="table-note">
        Matches with the largest Movement RP improvement appear first. Total RP
        includes wins, ties and all bonus RPs. Sampling error measures
        simulation noise; it does not measure uncertainty in the model
        assumptions.
      </p>
    {/if}
  </section>
{/if}
