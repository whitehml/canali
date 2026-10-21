<script lang="ts">
  import { formatDate, statusLabel, type Catalog } from "./model";
  let { catalog }: { catalog: Catalog } = $props();
  let query = $state("");
  let type = $state("All events");
  const filtered = $derived(
    catalog.events.filter(
      (event) =>
        (type === "All events" || type === event.type) &&
        `${event.name} ${event.city}`
          .toLowerCase()
          .includes(query.toLowerCase().trim()),
    ),
  );
  const available = $derived(
    catalog.events.filter((e) => e.status === "complete").length,
  );
</script>

<main>
  <section class="event-heading">
    <div>
      <p class="eyebrow">
        FTC / {catalog.season}–{String(catalog.season + 1).slice(2)} / SEASON EXPLORER
      </p>
      <h1>{catalog.region_name}</h1>
      <p class="intro">Follow the season, one field at a time.</p>
    </div>
    <div class="published">
      Catalog published<br /><span
        >{new Date(catalog.published_at).toLocaleString(undefined, {
          dateStyle: "medium",
          timeStyle: "short",
        })}</span
      >
    </div>
  </section>
  <section class="metrics" aria-label="Season summary">
    <div>
      <span class="eyebrow">ON THE CALENDAR</span><strong
        >{catalog.events.length}<small>events</small></strong
      >
    </div>
    <div>
      <span class="eyebrow">PUBLISHED FORECASTS</span><strong
        >{available}<small>available to explore</small></strong
      >
    </div>
    <div>
      <span class="eyebrow">SEASON</span><strong
        >{catalog.season}–{String(catalog.season + 1).slice(2)}</strong
      >
    </div>
  </section>
  <div class="season-toolbar">
    <div>
      <p class="eyebrow">THE CALENDAR</p>
      <h2>Events & forecasts</h2>
    </div>
    <div class="season-filters">
      <label class="sr-only" for="event-search">Search events or cities</label
      ><input
        id="event-search"
        type="search"
        bind:value={query}
        placeholder="Search events or cities"
      /><label class="sr-only" for="event-type">Event type</label><select
        id="event-type"
        bind:value={type}
        ><option>All events</option
        >{#each [...new Set(catalog.events.map((e) => e.type))] as kind}<option
            >{kind}</option
          >{/each}</select
      >
    </div>
  </div>
  <div class="event-list">
    {#each filtered as event (event.code)}
      <a class="event-card" href={`/events/${event.season}/${event.code}`}>
        <div class="event-date">
          {formatDate(
            event.date_start,
          )}{#if event.date_end && event.date_end !== event.date_start}<span
              >through {formatDate(event.date_end)}</span
            >{/if}
        </div>
        <div class="event-card-title">
          <span class="eyebrow">{event.type}</span>
          <h3>{event.name}</h3>
          <p>{event.city}{event.venue ? ` · ${event.venue}` : ""}</p>
          {#if event.forecast_url}<p class="forecast-cutoff">
              {event.label} · {event.played} of {event.scheduled ?? "?"} qualification
              matches included
            </p>{/if}
        </div>
        <span class="status-pill" class:available={event.status === "complete"}
          >{statusLabel(event.status)}</span
        ><span aria-hidden="true">↗</span>
      </a>
    {:else}<p class="no-events">No events match these filters.</p>{/each}
  </div>
  <p class="season-note">
    Dates and venues are from FIRST. Forecast availability refers to this
    published snapshot, not the official competition status. Historical replays
    and provisional models are labeled on their event pages.
  </p>
</main>
