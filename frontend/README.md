# Canali frontend

Vite + Svelte 5, served entirely as static assets. Public requests do not invoke computation Workers or D1. The event
page reads a published forecast and snapshot; custom scenarios run on the visitor's device. Each region's season page lists competitions
from FIRST, with dates, venues, event filters and forecast availability. Stable URLs use `/seasons/2025/USPA` and
`/events/2025/USPAHAQ1`. A custom scenario loads Pyodide in a module
Web Worker and runs the same Python simulator on the visitor's device.

The event header identifies the loaded forecast by its last included official match, or by alliance selection before
playoffs begin. Publication time is secondary. This describes the loaded snapshot, including while a newer
publication is available.

## Run locally

From the repository root, prepare the synthetic demonstration and shared engine archive:

```powershell
uv run python frontend/prepare.py
cd frontend
npm ci
npm run dev
```

Open the local address printed by Vite. The default event has synthetic team numbers, model coefficients and
published progress, visibly labeled as demonstration data. It is not a calibrated prediction of a real event.
The root Python environment needs the existing project dependencies. Use Node.js 22.18 or later.

## Publish from D1

The native publisher fetches public competition metadata from FIRST and reads forecasts through the private Worker's
authenticated `/forecasts/{season}-{code}/export` route. FIRST credentials remain in the existing warehouse settings.
Set the benchmark token in the command terminal, using the same value as the Worker's secret. Hosted export also
requires a Cloudflare Access service token authorized for the backend:

```powershell
$env:CANALI_BENCHMARK_TOKEN = Read-Host 'Benchmark token' -MaskInput
$env:CANALI_ACCESS_CLIENT_ID = Read-Host 'Access client ID'
$env:CANALI_ACCESS_CLIENT_SECRET = Read-Host 'Access client secret' -MaskInput
uv run python frontend/publish.py 2025 --region USPA --base-url https://canali-benchmark.mjhanson.workers.dev --label "Historical replay · demo models, uniform priors, no advancement assumptions"
```

For local D1, use a loopback `--base-url`, such as `http://127.0.0.1:8787`. The token is unnecessary with the default
local Worker, and local requests never receive hosted credentials. The Access pair can instead be stored in the
ignored `worker/.dev.vars.access.json` file described in the [backend setup](../worker/README.md#cloudflare-access).
The publisher does not ingest matches, rate events, assemble snapshots or start simulations; those
inputs and completed results must already be in D1. Simulations can run in the Worker or through the
[native runner](../worker/README.md#compute-locally-and-publish-to-hosted-d1), which uploads local results to D1.
The publication label is required so provisional model assumptions and historical cutoffs stay visible.

The export query joins each input to its matching revision and engine checkpoint in one D1 statement, with the same
season-revision guards used by result reads. Running or exhausted runs expose status without a publishable snapshot.
The publisher validates the local engine version, snapshot, result roster and FIRST team roster, then publishes team
names with the frozen inputs and forecast. It records the source revision and snapshot hash. Missing forecasts remain
listed as not published; a source or engine correction removes them from the next catalog until recomputed.
The same static publication is used by Vite previews and the hosted site.

`public/generated/catalogs/{season}/{region}.json` points to content-addressed event JSON and engine TAR files.
`public/generated/index.json` lists the available season/region catalogs and their event codes so direct event URLs
can find the correct catalog without assuming that the event code starts with its region. The index changes only
when catalog membership changes. Existing catalogs for other regions and seasons remain available.
Each regional catalog is replaced
only after the export succeeds; authentication, engine or roster failures leave the existing catalog intact. Immutable
asset URLs keep a browser's custom simulation tied to the published forecast it loaded. Re-exporting never recomputes
the Monte Carlo. Old hashed assets can be removed from the local generated directory when no retained catalog uses
them. The publisher includes non-advancement events, league meets and tournaments, qualifiers, super qualifiers,
championships, FIRST Championship divisions, premier events and off-season events, excluding kickoffs.
Repeat `--region` to publish several regions in one cycle, for example `--region USPA --region USNJ`.

## Automatic static publication

The application and forecast data have separate Cloudflare Static Assets deployments: `canali-frontend` serves
the compiled application, and `canali-data` serves the index, regional catalogs, snapshots and simulation engines.
Both have no request handler or backend bindings. Public reads invoke neither Python Workers nor D1.
The data host permits cross-origin reads of these public assets; no credentials accompany browser data requests.

Run the native publisher on the same always-on host as the importer. From the repository root, with FIRST
credentials, backend Access/bearer credentials and Cloudflare deployment
credentials available to that process:

```powershell
uv run python frontend/publish.py 2025 --region USPA --base-url https://canali-benchmark.mjhanson.workers.dev --label "Historical replay · demo models, uniform priors, no advancement assumptions" --watch --deploy --interval-s 60
```

Each cycle exports all current forecasts into a staging asset set, coalescing changes across events. Unchanged event
documents keep their timestamps and hashed URLs, and an unchanged catalog keeps its bytes and modification time.
Publication timestamps use computation time when available. The publisher deploys only the data asset set when
its content changes, using `data.wrangler.toml`. It does not read `dist/`, build client code, deploy the application,
or rerun Monte Carlo. Changes across all requested regions are coalesced into one atomic data deployment.

`var/forecast-publication/site/` retains published snapshots and engine archives so cached catalogs and open pages can
resolve older URLs. `deployed.json` records only the last successful deployment. Failed source reads, validation or
deployments leave the hosted publication available. Consecutive failed cycles use exponential backoff: with the
default interval, waits are approximately 1, 2, 4, 8, 16, then at most 30 minutes. Jitter varies waits between 80%
and 100% of each delay, never below the normal polling interval. A configured interval longer than 30 minutes is
also respected. An HTTP `Retry-After` response from the backend can extend the wait, whether expressed as seconds
or an HTTP date. A successful cycle, including an unchanged publication, resets the failure count.

`var/forecast-publication/retry.json` persists the failure count and next allowed attempt across process restarts.
A one-shot invocation during that cooldown logs that publication is deferred and exits without fetching or deploying;
a failed one-shot invocation records the cooldown and exits with an error. Keep this file with the staging directory
and deployment marker. After fixing a failure, an operator can remove `retry.json` to request an immediate attempt.
The FIRST client retains its existing bounded request retries; the publication backoff follows a failed cycle.
Publication retries do not start or retry simulations.

An OS file lock permits one
publisher at a time and releases automatically when the process exits. The interval starts after a cycle finishes;
cycles do not overlap. Use a process supervisor to restart the publisher after a host or process failure.

`--deploy` without `--watch` performs one export and deploys only if changed, suitable for an external scheduler.
`--watch` without `--deploy` repeatedly exports into `public/generated/` for local use. Stop a watcher with Ctrl+C.
Keep the staging directory and deployment marker together between runs, including when publishing another region.
Use the same host and staging directory for every publisher targeting `canali-data`: separate staging directories
would replace each other's region catalogs. After changing the engine, restart the publisher with sources matching
the backend; older snapshots retain their corresponding engine archives. Client-only changes need only an application
deployment. Apply all backend migrations, including
`0007_forecast_timestamps.sql`, before deploying the updated computation backend.

The browser reads `/generated/index.json` on page load and selects the catalog for the URL's season and region.
The root page uses Pennsylvania in the latest available season when present, otherwise that season's first region.
The browser revalidates only that regional catalog every 60 seconds while visible and when returning to
the tab. `Cache-Control: no-cache` permits the browser to use its cached catalog after an ETag/Last-Modified check.
Content-addressed snapshots and engines use long-lived immutable caching. The season page refreshes its listings;
an event page offers **Load update** when its snapshot URL changes or the forecast is withdrawn. The current snapshot,
custom results and running scenario remain untouched until the visitor chooses to reload. Failed checks keep the
loaded publication. Both catalog checks and snapshot downloads stay on the static asset path.

## Prepare a standalone demonstration

The exporter can use a frozen snapshot and its completed Worker report, such as the files produced by
`worker/assemble_forecast.py --run`. From the repository root:

```powershell
uv run python frontend/prepare.py --snapshot var/worker-forecast/2025-USNJCMPPKWY/snapshot.json --result var/worker-forecast/2025-USNJCMPPKWY/result.json --name "Parkway Division" --label "Historical replay · sample model coefficients"
```

Provide the report for that exact snapshot; the exporter validates the snapshot and checks that result teams match,
but the current Worker report does not carry a snapshot hash. Omitting `--result` computes a matching result natively.
Keep the label explicit about historical cutoffs and provisional or demonstration models. The exporter clears the
snapshot's local season-revision dependency, bundles only the simulator and rule-pack modules, and writes
`public/generated/index.json`, a regional catalog and content-addressed event/engine assets. Generated assets are ignored by Git. Rebuild them when Python sources
or published inputs change; the archive hash prevents silently running a different engine against a cached snapshot.

Engine packaging writes a deterministic, uncompressed `engine-<hash>.tar`. Cloudflare's static `_headers` rule sets
`Content-Encoding: br` and `Content-Type: application/x-tar`, causing Cloudflare to apply HTTP Brotli compression.
The browser decodes the response before JavaScript reads it; the SHA-256 hash covers the decoded TAR bytes,
which Pyodide extracts directly. Vite dev/preview serves the same TAR without HTTP compression. No build-time
encoder, custom server handler or browser decoder is needed. The production delivery path requires a browser
that supports Brotli HTTP decoding. Deployment checks should verify both the wire encoding and decoded hash.

## Deploy

The public deployment is [canali-frontend.mjhanson.workers.dev](https://canali-frontend.mjhanson.workers.dev).

First publish data with `frontend/publish.py --deploy` as described above. Find the resulting `canali-data` URL in
Wrangler's deployment output. Then build and deploy the application from `frontend/`:

```powershell
npm ci
npm run check
npm test
$env:VITE_PUBLICATION_BASE_URL = 'https://canali-data.mjhanson.workers.dev'
npm run build
node ../worker/node_modules/wrangler/bin/wrangler.js deploy --config wrangler.toml
```

Use your own data deployment's origin if it differs. `VITE_PUBLICATION_BASE_URL` is a public data origin, never a
private backend URL or credential. Builds with this setting omit `public/`, so the application deployment contains
only compiled client assets and the SPA entry point. It uses a single-page application fallback for event URLs;
the data deployment returns 404 for missing files, without an HTML fallback. With the setting unset, local demos
and standalone builds use `public/generated/` on the same origin.

The deployment is named `canali-frontend`; it has no request handler, backend binding, D1 binding or credentials.
Each data deployment publishes its catalogs, snapshots and browser engine files together so their references stay
consistent, independently of application deployments. Forecasts
are public; mutation and export HTTP endpoints on the Python backend remain private. No Cloudflare account token,
benchmark bearer token or FIRST credential belongs in frontend code, Vite environment variables or published JSON.
The native publisher holds those credentials and its metered work is independent of public visitor traffic.

For the first rollout of this layout, publish all desired regions to `canali-data` before deploying the updated
application. The former combined deployment can keep serving existing visitors during that transition. This layout
does not read the old single-catalog format. To retain older snapshot URLs on the new data host, copy the previous
publication's `generated/event-*.json` and `generated/engine-*.tar` files into the new staging directory before its
first deployment. Do not copy application files or the old catalog. Preserve that staging directory thereafter.

## Custom simulations

The first run downloads Pyodide 314.0.7, NumPy, SciPy and Pydantic from the pinned jsDelivr distribution. The page
remains responsive during downloads and computation. Each team-strength adjustment scales that team's scoring components
proportionally between 50% and 150% in steps of 5%. Adjustments accumulate across teams; individual resets restore a
team to 100%, and resetting the scenario clears all adjustments and custom results. Official played results and
unadjusted team ratings remain fixed. The caller chooses
1,000 or 5,000 iterations. Fixed-size exploratory runs report the simulator's standard error rather than promising a
particular convergence tolerance. The random seed stays fixed to support comparisons.

"Improve individual components" adds 0–100 predicted points, including decimals, to selected scoring components after the team's
overall percentage adjustment. It can improve a component whose published rating is zero. Component changes accumulate
across teams and are included in resets, labels and shared links. They affect the existing score, RP and award models;
the models are not refitted. Qualification tiebreaker components still use the score model's season-wide proportions.

The custom table shows changes in average rank, captain probability and event-win probability relative to the shared
forecast. Lower ranks are better; probability changes use percentage points. Editing inputs leaves the last completed
result visible with a notice until another run completes, and its label retains the adjustments used for that run.

"Share these inputs" copies a URL containing the team adjustments and simulation count, with a selectable link if
clipboard access is unavailable. Links identify the exact source snapshot and simulation engine. Opening one loads
the inputs for review without downloading Python or starting a run. A link for a changed snapshot or engine is rejected
with an explanation; forecasts remain available for browsing. Links share inputs, not computed results, and may stop
working after a publication changes. Scenario links are public and contain no credentials.

## Playoff partner comparisons

Choose a captain, two possible partners and an assumed alliance seed. Each trial simulates the remaining qualification
matches with the event model, keeping played results fixed. For each candidate it reserves the proposed pair, forms
the other alliances from the remaining teams in simulated qualification order using the pick model, inserts the pair
at the chosen seed, then plays the event's full playoff bracket using its score and foul models. The alternative partner
can therefore join an opposing alliance. Published alliance selection is replaced for this hypothetical comparison.

The results estimate winning the playoff, reaching the final and average playoff finish, conditional on that pair
and seed. They do not estimate whether a team becomes a captain, whether the partner is available at its pick, or
advancement and awards. Playoff scores add team ratings; robot-role compatibility and scoring synergy are not modeled.
Opposing alliances can change with the proposed partner. The display retains the exact inputs
used for the last completed run and marks results when the draft changes. The iteration count and all component and
team adjustments apply to both alternatives. "Share this comparison" includes these inputs and the pairing setup.

Comparisons support two-team playoff alliances and snapshots from before any playoff matches were played. Refusing
later snapshots avoids discarding actual playoff results. Both alternatives reuse the same qualification trial and
start their selection/playoff draws with the same per-trial random seed. Each probability reports its Monte Carlo
standard error; small differences can still be noise.

`partner_comparison.py` is included in the immutable browser TAR and runs through the same lazy-loaded Web Worker as
event forecasts. Its counters and RNG state advance in bounded batches. There is no additional server compute or
backend route. It currently obtains qualification trials through the complete event simulator, which also performs
an unused baseline playoff; a shared qualification helper would reduce work if larger comparisons become slow.

The Python Worker and browser use the same `snapshot.py`, `forecast.advance` and simulator sources. Python is loaded
only after the visitor requests a custom simulation. Progress arrives after each bounded batch. Cancel terminates the
Web Worker, including an in-progress Python call; a later run reloads the runtime. Completed scenarios stay in page
memory, separate from the shared forecast, and are discarded on reload. Browser memory and speed depend on the device;
mobile performance is not yet characterized. No cross-origin-isolation headers or shared-memory interrupt support are
required. Network access is needed for the first runtime download.

The table supplies the first useful view without a chart dependency. Use TanStack Charts if a later slice adds
rank distributions or rating histories.

## Autonomous LEAVE comparisons

For DECODE events with a published schedule, the autonomous LEAVE panel compares an entered current success rate
with an improved rate for one team. Both rates are assumptions supplied by the visitor; current reliability is not
inferred from EPA. This panel uses published team strengths independently of the generic component adjustments.
It reports expected added points per remaining appearance, paired average qualification ranks and their difference's
Monte Carlo standard error. Remaining matches are sorted by Movement RP probability gain and show alliance points,
Movement RP probability and total RP, including win/tie points. Surrogate appearances are marked and excluded from
the team's ranking score. Played results remain fixed. For a team with no remaining qualification matches,
the page disables the comparison and explains why before downloading the simulation runtime.

`auto_leave.py` samples robot LEAVE outcomes explicitly (3 points each). Other robots' probabilities are their
leave component EPA divided by 3, clipped to 0–1. It estimates alliance BASE outcomes by interpolating between
adjacent legal totals, 0/5/10/15/30, with the mean equal to the summed base EPA clipped to 0–30. This is a simple
assumed distribution, not a fit to observed robot states. BASE and LEAVE draws are independent. The Movement RP
is awarded by comparing sampled BASE plus LEAVE points with the event tier's rule-pack threshold.
See [DECODE manual section 10.5.4](https://ftc-resources.firstinspires.org/ftc/archive/2026/game/cm-html/DECODE_Competition_Manual_TU32.htm).

Other points use the existing score distribution with leave and base components removed, then the sampled action
points are added back. AUTO tiebreaker points use the remaining autonomous share plus sampled LEAVE points;
BASE is accounted for separately. Other bonus RPs retain their existing models. Every trial shares all other
outcomes, including random ranking tiebreakers, between the two alternatives. Identical rates give identical
results. These comparisons use a different action model from the shared forecast, so their current-rate baseline
can differ from it. They estimate qualification impact only; playoff and alliance-selection probabilities are not
part of this comparison. Model assumptions can matter more than the displayed sampling error.

The helper is packaged in the browser TAR and runs in bounded batches through Pyodide. The page permits one
simulation at a time and releases the comparison's runtime on completion or cancellation. This first version has
no shareable LEAVE-scenario links and supports only DECODE.

## Checks

```powershell
npm run check
npm test
npm run build
```

From the repository root, Python publication and D1 export tests can be run with:

```powershell
uv run pytest -c worker/pyproject.toml worker/tests frontend/tests --basetemp var/frontend/test-tmp
```

Model tests check multi-team/component isolation, bounds, URL round trips, snapshot and engine matching, comparison labels,
stable ranking and missing values. Browser verification should include an unchanged 100% scenario, multiple changed
teams, individual and whole-scenario resets, sharing and reopening inputs, stale links, cancellation, another run
after cancellation, and a narrow viewport. Partner tests check reserved teams and seeds, unique alliance membership,
checkpoint continuation, candidate-order invariance and invalid requests. Browser verification also covers component
and partner link round trips and comparison results against the native helper.
After packaging changes, also compare browser results with the native result for an identical snapshot and seed.
Route tests distinguish season, event and unknown URLs. Publication tests check deterministic engine bundles,
matching revisions and rosters, missing forecasts and preservation of the existing catalog on failure. D1 export
tests reject obsolete inputs, engines and season dependencies, and withhold unfinished snapshots.
Publication tests cover unchanged exports, deployment deduplication, retry after failure, exclusive publisher locking
and static-only hosting configuration. Browser helper tests cover cache modes and changed or withdrawn snapshot URLs.
