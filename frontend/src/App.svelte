<script lang="ts">
  import { onMount } from "svelte";
  import EventPage from "./EventPage.svelte";
  import SeasonPage from "./SeasonPage.svelte";
  import {
    INDEX_URL, CATALOG_REFRESH_MS, readPublication, forecastChanged, publicationUrl, catalogForRoute,
    type PublicationIndex,
  } from "./publication";
  import {
    formatDate,
    routeFor,
    statusLabel,
    type Catalog,
    type EventListing,
    type PublishedEvent,
  } from "./model";

  let catalog = $state<Catalog | null>(null);
  let listing = $state<EventListing | null>(null);
  let event = $state<PublishedEvent | null>(null);
  let ready = $state(false);
  let error = $state("");
  let notFound = $state(false);
  let updateAvailable = $state(false);
  let index = $state<PublicationIndex>({ regions: [] });
  const seasonUrl = $derived(
    catalog ? `/seasons/${catalog.season}/${catalog.region}` : "/",
  );

  onMount(() => {
    const controller = new AbortController();
    let timer: number | undefined;
    let checking = false;
    let catalogUrl: string | null = null;
    async function load() {
      index = await readPublication<PublicationIndex>(INDEX_URL, controller.signal);
      const region = catalogForRoute(location.pathname, index);
      if (!region) {
        notFound = true;
        return;
      }
      catalogUrl = region.catalog_url;
      catalog = await readPublication<Catalog>(catalogUrl, controller.signal);
      if (catalog.season !== region.season || catalog.region !== region.region)
        throw new Error("The published catalog does not match this region.");
      const selected = routeFor(location.pathname, catalog!);
      if (!selected) {
        notFound = true;
        return;
      }
      if (selected === "season") return;
      listing = selected;
      if (selected.forecast_url) {
        const published = await readPublication<PublishedEvent>(selected.forecast_url, controller.signal);
        if (
          published.code !== selected.code ||
          published.season !== selected.season
        )
          throw new Error("The published event does not match this URL.");
        event = { ...published, engine_url: publicationUrl(published.engine_url) };
      }
    }
    async function refresh() {
      if (!catalogUrl || checking || document.visibilityState === "hidden") return;
      checking = true;
      try {
        const next = await readPublication<Catalog>(catalogUrl, controller.signal);
        if (next.season !== catalog?.season || next.region !== catalog.region)
          throw new Error("The published catalog does not match this region.");
        const selected = routeFor(location.pathname, next);
        if (listing) {
          updateAvailable = forecastChanged(listing, selected === "season" ? null : selected);
          if (!event && selected && selected !== "season" && !selected.forecast_url) listing = selected;
        } else if (selected === "season") {
          catalog = next;
        } else if (selected) {
          updateAvailable = true;
        }
      } catch {
        // Keep the loaded publication and any running scenario through a failed refresh.
      } finally {
        checking = false;
      }
    }
    load()
      .then(() => {
        if (controller.signal.aborted) return;
        timer = window.setInterval(refresh, CATALOG_REFRESH_MS);
        document.addEventListener("visibilitychange", refresh);
      })
      .catch((e) => {
        if (e.name !== "AbortError") error = e.message;
      })
      .finally(() => (ready = true));
    return () => {
      controller.abort();
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", refresh);
    };
  });
</script>

<svelte:head
  ><title
    >{listing ? `${listing.name} · Canali` : `${catalog?.region_name ?? "FIRST Tech Challenge"} · Canali`}</title
  ></svelte:head
>
<header class="site-header">
  <a class="brand" href={seasonUrl} aria-label="Canali season home"
    ><span class="brand-mark" aria-hidden="true">c/</span><span
      >canali<span class="brand-dot">.</span></span
    ></a
  >
  <nav aria-label="Main navigation">
    <a class="active" href={seasonUrl}>{catalog?.region_name ?? "Season"} events</a>{#if event}<a
        href="#notes">How to read this</a
      >{/if}
  </nav>
  <span class="header-caption">FIRST TECH CHALLENGE</span>
</header>
{#if updateAvailable}
  <aside class="publication-update" role="status">
    <span>An updated publication is available.{#if event}{" "}Your current scenario is unchanged.{/if}</span>
    <button onclick={() => location.reload()}>Load update</button>
  </aside>
{/if}
{#if error}
  <main class="empty">
    <p class="eyebrow">DATA UNAVAILABLE</p>
    <h1>We couldn’t load this page.</h1>
    <p role="alert">{error}</p>
    <button onclick={() => location.reload()}>Try again</button>
  </main>
{:else if !ready}
  <main class="empty" aria-busy="true">
    <p class="eyebrow">CANALI / FIRST TECH CHALLENGE</p>
    <h1>Loading the season…</h1>
  </main>
{:else if notFound || !catalog}
  <main class="empty">
    <p class="eyebrow">PAGE NOT FOUND</p>
    <h1>This event isn’t in the published season.</h1>
    <a href={seasonUrl}>Browse events →</a>
  </main>
{:else if event}
  <EventPage {event} region={catalog.region} regionName={catalog.region_name} />
{:else if listing}
  <main class="empty">
    <p class="eyebrow">{listing.type} / {formatDate(listing.date_start)}</p>
    <h1>{listing.name}</h1>
    <p class="status-pill">{statusLabel(listing.status)}</p>
    <p>{listing.city} · {listing.venue}</p>
    <p>
      A forecast has not been published for this event. Check the official
      source for competition results.
    </p>
    <div class="event-links">
      <a href={seasonUrl}>← All events</a>{#if listing.source_url}<a
          href={listing.source_url}
          target="_blank"
          rel="noreferrer">Official event results ↗</a
        >{/if}
    </div>
  </main>
{:else}
  {#if index.regions.length > 1}
    <nav class="region-navigation" aria-label="Published regions">
      {#each index.regions as region}
        <a href={`/seasons/${region.season}/${region.region}`}
          aria-current={catalog.season === region.season && catalog.region === region.region ? "page" : undefined}
          >{region.region_name} / {region.season}–{String(region.season + 1).slice(-2)}</a>
      {/each}
    </nav>
  {/if}
  <SeasonPage {catalog} />
{/if}
<footer>
  <span
    >canali<span class="brand-dot">.</span>
    <span class="footer-description">A clearer view of an uncertain field.</span
    ></span
  ><span>Independent FTC statistics</span>
</footer>

<style>
  .region-navigation {
    display: flex;
    flex-wrap: wrap;
    gap: 1rem;
    padding-top: 1rem;
    padding-bottom: 1rem;
  }
  .publication-update {
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 1rem;
    padding: 1rem;
    border-bottom: 1px solid currentColor;
  }
</style>
