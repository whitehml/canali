# Python Worker + D1

Runs prepared event inputs through the existing Python models, saving results in local D1. It supports qualification
EPA/pRidge totals and match forecasts, plus the complete single-event Monte Carlo: standings, alliance selection,
playoffs, judged awards and advancement. A native importer fetches one event's official FIRST qualification data
without Postgres. D1 can replay an imported season corpus to supply pre-event total and component EPA priors.
Fitted simulation models are still prepared separately.
Snapshots can be assembled from the imported event and its D1 component history, including published alliance
selection, playoff progress and current-event playoff EPA updates.

## Run locally

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and Node.js 22 or later. From this directory:

```powershell
npm ci
uv sync --locked
uv run pywrangler d1 migrations apply canali-local --local
uv run pywrangler dev --ip 127.0.0.1 --port 8787
```

In another terminal, also in this directory:

```powershell
uv run python smoke.py
```

The smoke check uploads synthetic fixtures, computes ratings and forecasts, reads persisted results, repeats a
run, corrects a score and checks invalid-input handling. It also resumes a complete event simulation across HTTP
requests and compares the completed forecast with native Python. No source-data credentials, Cloudflare login, Docker or
remote database are needed. `wrangler.toml` contains a local database ID placeholder. Keep the explicit `--local`
flag on database commands.

The Worker has its own Python 3.14 environment; the existing workspace remains on Python 3.12. Pywrangler prepares
the WebAssembly dependencies separately in `python_modules/`, with versions recorded in `pylock.toml`. The custom
build copies the numerical modules and rule-pack parser from their existing source directories; edit those sources
rather than the ignored copies under `src/`. To fit the upload limit, the bundle excludes dependency test suites,
bytecode and unused SciPy subpackages. Model fitting and normalization fitting remain native; the Worker runs
supplied coefficients using NumPy, `scipy.special` and its linear-algebra support. Package and runtime support must also be checked
on Cloudflare before deployment.

## Event input

`fixtures/event.json` is a synthetic prepared event, not an upstream API payload. It supplies:

- Teams' pre-event EPA totals and prior qualification-match counts.
- Qualification matches in increasing ordinal order, with non-foul scores or two null scores for an unplayed match.
- EPA K/M schedules, a ridge penalty and fitted score-model coefficients.
- A random seed and the number of score draws per remaining match.

The EPA and pRidge calculations use the supplied priors; initialization, layoff adjustment and season normalization
must happen before preparing those inputs. One or two rated teams per alliance are supported. The prototype computes
total ratings only. Component EPA is available through the season replay below. Weekly calibration and regional
season simulations have not yet been ported.

## Import a FIRST event

Save `WAREHOUSE_FTC_EVENTS_USERNAME` and `WAREHOUSE_FTC_EVENTS_TOKEN` in `warehouse/.env`. From the repository root,
use the native workspace environment (Python 3.12), with the local Worker running and migrations applied:

```powershell
uv run python worker/import_event.py 2025 USNJCMPPKWY --parameters worker/fixtures/event.json --initial-epa 100
```

This example uses a completed 24-team division, explicit uniform EPA priors and the synthetic fixture's calibration.
It demonstrates ingestion and computation; its ratings are not season-calibrated estimates. For meaningful ratings,
provide a prepared event JSON with the correct pre-event `teams` priors, K/M schedules, ridge penalty and score model,
and omit `--initial-epa`. The importer replaces that file's `matches` with official qualification results.

The native command reuses the warehouse client's authentication, pagination, pacing and retry handling. Credentials
stay in the native process; the Worker receives event data and model parameters. It imports the team's roster and
qualification hybrid schedule, subtracts the opponent's committed foul points from each final alliance score, includes
surrogate teams and excludes no-shows from rating updates. It rejects incomplete scores, missing foul totals, duplicate stations,
roster mismatches and matches with an entirely absent alliance. Unplayed matches retain null scores. For divided
championships, choose the division's event code; the parent event may have no qualification schedule.

D1 atomically retains the latest source payloads and prepared input under a season-qualified key, such as
`2025-USNJCMPPKWY`. Repeating identical data and parameters preserves the revision and published result. A source or
parameter change increments the revision and hides the previous result until recomputed. Directly replacing prepared
inputs through `/events/{code}` clears their previous source attribution. Historical source versions are not retained.

The command also saves `source.json` and `result.json` in the ignored `var/worker-import/{season}-{code}/` directory.
The saved source document can be replayed with `PUT /imports/{season}-{code}` without another FIRST request. There is
no polling or scheduled ingestion yet. Source fetching runs natively; parsing, validation and rating computation run
inside the Python Worker. Complete event Monte Carlo snapshots still require separately prepared component ratings,
event progress and fitted models.

## Replay season EPA priors

From the repository root, using the native workspace environment and a running local Worker:

```powershell
uv run python worker/replay_season.py 2025 --events USNJUCM1,USNJUCM2,USNJCMPPKWY
uv run python worker/import_event.py 2025 USNJCMPPKWY --parameters worker/fixtures/event.json --season-priors
```

Use `--region USPA` instead of `--events` to select Pennsylvania's rated competitions. Region imports skip events
with fewer than four teams or no published qualification schedule, reporting each skip. Unrated scrimmages and
off-season events can still appear in the public catalog, without forecasts.

The first command imports the selected events' metadata, teams and qualification schedules, then calls
`POST /seasons/2025/step` until replay finishes. Repeating the command updates changed sources and resumes from the
remaining cursor. An interrupted replay can also continue by calling `/step` directly, without fetching FIRST again.
Source documents are saved in the ignored `var/worker-season/2025/` directory.

This is a replay of the **imported qualification corpus**. The example includes three events and omits the rest of
those teams' season histories; it is a parity check, not a complete season rating. Supply every relevant event code
for a full qualification corpus. Playoff contributions and end-of-season percentile normalization remain in the native
pipeline. Event ordering matches the warehouse: start date, then event code, with match numbers in order.

The Worker calls the existing native `replay_season` engine with the versioned EPA constants. Without a rule pack it
uses a total-only partition; a supplied rule pack enables its default component partition.
It computes initialization from the first 4,000 scored alliance rows in the imported corpus. Until that window fills,
it uses the same provisional scale as the native pipeline: the average measured initialization scale. Rookie seeding,
K/M schedules, qualification match counts and the capped layoff boost all use the shared implementation.

For carryover, pass `--config path/to/config.json` containing optional `previous_norm` and `second_norm` objects, mapping
team numbers to normalized prior-season ratings. Without those inputs, every team enters without prior-season history.
Producing those normalized prior-season ratings is not automated here. Configuration changes restart replay.

Each step rates one event, retaining at most 128 teams and 1,000 matches in Worker memory. Initialization reads at most
4,000 alliance scores; D1 performs that prefix query. Team history stays in indexed D1 rows, and the Worker reads only
the current event's teams' preceding ratings. An event step saves its ratings and cursor in one D1 transaction. This
bounds buffering independently of the total number of season matches; production memory and CPU still need measurement.

Changed event data deletes ratings from the affected event onward. A changed initialization scale restarts the whole
corpus because every team's starting rating depends on it. Engine changes also restart replay. Concurrent stale steps
return HTTP 409. Unchanged uploads preserve the revision and completed prefix.

`--season-priors` reads each team's rating **before** the target event, including its layoff adjustment, even if the
event has already been replayed. The event must be registered in the season corpus, and preceding events must be
processed. These priors replace the parameter file's teams and K/M schedules; the file still supplies the ridge penalty
and score-model coefficients, which must be calibrated separately. The synthetic fixture's coefficients remain for
demonstration only. EPA priors can be negative, as in the native engine.

Results computed with season priors carry a season revision. Any season revision or engine change hides those results
and prevents recomputation with stale priors. Finish the season replay and rerun `import_event.py --season-priors` to
refresh them. The revision check is conservative: adding a later event also invalidates existing linked results.

### Component EPA

Supply the season's rule pack to fetch score breakdowns and replay its component partition:

```powershell
uv run python worker/replay_season.py 2025 --events USNJUCM1,USNJUCM2,USNJCMPPKWY --rule-pack warehouse/rule_packs/2025_decode.toml
```

The rule pack is saved in the season configuration; its default partition is usually `leaf`. The importer fetches
`/scores/{code}/qual` in addition to the teams and hybrid schedule, and retains those payloads in D1 and the local
source archive. It converts scoring fields to the rule pack's column names and applies declared robot-state scoring.
Qualification non-foul totals come from the official final score minus the opponent's committed fouls; DECODE's
published zero `preFoulTotal` is not used for reconciliation.

Every scored match must have both breakdowns, every selected component must be present and finite, and each alliance's
component sum must agree with its non-foul score within the native reconciliation tolerance. Invalid imports return
HTTP 400 and preserve the last valid event and revision. This is stricter than the native batch pipeline's ability to
exclude individual unratable matches: when upstream totals and breakdowns arrive at different times, retry the import
once they agree. Unplayed matches do not require breakdowns.

Component ratings use the native initialization, simultaneous match updates and proportional layoff adjustment.
D1 stores each team's component series with its post-event total and match count. The priors endpoint includes a
`components` object on each team, keyed by column name, such as `auto_artifact_points`; the sum is that team's `epa`.
Components and totals can be negative after updates, as in the native engine. The prototype permits up to 64 series.

Enabling components, changing the rule pack or returning to total-only mode requires reimporting **all registered
events** for that season. Replay and priors return HTTP 409 until their partition fingerprints match the configuration.
This prevents mixing component definitions or replaying old total-only inputs as component inputs. Changing only
prior-season normalized ratings restarts replay without requiring a source reimport.

`import_event.py --season-priors` also carries component priors into its saved input, while its qualification calculation
still updates totals only. Use the snapshot assembler below to connect component ratings to the full event simulator.

## Assemble a forecast from official progress

After importing and replaying a component corpus, run this from the repository root:

```powershell
uv run python worker/assemble_forecast.py 2025 USNJCMPPKWY --models worker/fixtures/forecast.json --context worker/fixtures/forecast-context.json --through 6 --run
```

The Worker builds a snapshot from the target event in D1. It starts with the teams' pre-event component priors,
applies the native EPA updates through the chosen qualification cutoff, and combines the resulting ratings with the
official schedule, final scores, bonus RPs, ranking tiebreakers, surrogate flags and disqualifications. Cities, team
ages and rookie status come from the official team roster. Rule-pack recoveries reconstruct derived fields such as
DECODE's `preFoulTotal` instead of trusting its published zero.

`--through` counts scored qualification matches in increasing match-number order. The example keeps the first six
of the division's 30 qualifications fixed and simulates the remainder. Later results, bonus RPs and DQs are masked
out of the snapshot and do not update its current-event ratings. `--through 0` starts before qualifications; before a
schedule is published, the simulator generates one. For historical backtests, supply models, context and season
initialization appropriate to the cutoff date; this option only truncates current-event match updates and progress.

Omitting `--through` uses the latest event stage. The native command fetches FIRST's seated alliances and, once selection
is published, the playoff hybrid schedule and score breakdowns. The Worker fixes those alliances in seed order and
retains the winning seed of each decisive playoff game. Ties count as played matches and update ratings, but do not
increment the decisive-game number. Completed qualifications are required before selection can be included.

Current-event playoff scores update component EPA with the native elimination weight, without increasing qualification
match counts. Both final scores and reconciled breakdowns are required; no-shows receive no rating update. Match
participants must match the seated alliances, and results must form a valid bracket prefix, including best-of-three
finals and the double-elimination final reset. Incomplete or inconsistent upstream data is rejected before replacing a
valid snapshot; retry when FIRST's endpoints agree. Backup substitutions are not represented by the current simulator;
stations that differ from the seated alliance are rejected.

An explicit `--through N` excludes all alliance selection and playoff data. Use `--after-qualifications` to retain all
qualification results and reconstruct the event immediately before selection. The season corpus still replays
qualifications only; the new playoff updates apply to the target snapshot, rather than later events' season priors.
Direct assembly callers supply FIRST's raw `alliances`, `playoff_schedule` and `playoff_scores` payloads alongside
`selection_published: true`; the native command collects those automatically. Historical `through` requests ignore
those later-stage payloads.

`--models` accepts a `ModelsInput` JSON object or a prepared snapshot containing `models`. With a prepared snapshot it
also reads its seed, convergence settings and matches-per-team setting; its field, schedule and results are ignored.
Score, RP, selection and award coefficients still need external calibration. The example's coefficients are demo
inputs, and its three-event corpus is incomplete.

`--context` is required and supplies `allied`, decayed award `history`, `inspire_won_before` and `slots`, using the same
formats as `EventField` and `Slots`. Prepare these attributes for the event's start date; history from later events
would bias the forecast. The supplied `forecast-context.json` explicitly assumes no previous alliance/award history,
no prior Inspire winners and no advancement slots. Its empty assumptions demonstrate the pipeline and should be
replaced with real context for meaningful advancement forecasts. Context team keys must belong to the event roster;
already-advanced teams and historical alliance partners may include teams outside the field.

The snapshot is saved under `/forecasts/2025-USNJCMPPKWY`, with its played-match count and season revision, and
archived in `var/worker-forecast/2025-USNJCMPPKWY/snapshot.json`. Add `--run` to resume bounded Monte Carlo requests until
convergence or exhaustion and save `result.json` alongside it. Identical snapshots preserve their input revision,
checkpoint and result, so repeating an interrupted command can resume its computation.

A changed season revision or engine hides linked forecast checkpoints and results and rejects stale publication.
Finish season replay and reassemble to refresh them. This conservatively invalidates a snapshot even when a correction
was to a qualification after its cutoff. Prepared snapshots without a season dependency remain supported.

## Complete event forecasts

`fixtures/forecast.json` contains synthetic teams and published progress, with the repository's 2025 rules and fitted
model coefficients. It illustrates the snapshot accepted by `/forecasts/{code}`:

- `rule_pack`: the season's rules, ranking points, tiebreakers, playoff brackets and advancement point tables.
- `field`: component predictions, prior alliance partners, cities, ages, award history and eligibility.
- `models`: fitted score, RP, selection and award coefficients; no season-wide calibration is performed by the Worker.
- `schedule`: qualification stations and optional surrogate flags, or null to generate a schedule each iteration.
- `progress`: played-match flags, official scores, bonus RPs, tiebreaker components and optional disqualifications,
  seated alliances and decided playoff games. Omit it for an unplayed event.
- `slots`: advancement counts and already-advanced teams, or null if the event offers no slots.
- `seed`, `tolerance`, `min_iterations`, `max_iterations`: reproducibility, convergence and the total work budget.

The assembler supplies component ratings and published progress throughout the event, and callers can also upload prepared
snapshots directly. Integer team keys are represented as strings in JSON. RP curve recoveries use
`{"kind": "identity"}`, `{"kind": "ratio", "value": 0.25}`, or `{"kind": "floor_divide", "value": 20}`.

Each `/step` request advances at most 32 event iterations and checks a two-second elapsed-time budget between
iterations. A single iteration is not interrupted. D1 stores counts and the exact random-generator state, so a new
request or restarted process can resume the run. Individual simulated outcomes are discarded.

The response has `status`, `iterations`, `standard_error` and `result`. A running forecast returns HTTP 202 with no
published result. Once the shared simulator's convergence criterion is met it returns `complete` with HTTP 200 and
the same team summary as the native runner. Hitting `max_iterations` first returns `exhausted`, also with HTTP 200,
and no published result. Repeating a terminal step returns the saved report without further computation.

`tolerance` is an absolute Monte Carlo standard-error target for the tracked cumulative advancement-order
probabilities, not relative error or model accuracy. The maximum Bernoulli standard error after `n` simulations is
`0.5 / sqrt(n)`. Because convergence uses a strict inequality, `floor(1 / (4 * tolerance²)) + 1` simulations suffice
for any tracked probability, subject to `min_iterations`. A 1-percentage-point target therefore needs at most 2,501
simulations; the 10,000-iteration default budget is larger than necessary for that target. An iteration budget bounds
successful simulation work, not CPU consumed by failed attempts or retries, and a CPU limit is not a spending cap.

Results include rank distributions and captain, selection, playoff-win and advancement probabilities. Judged awards
are simulated as part of advancement; the existing summary does not expose separate award-win probabilities.

Input revisions invalidate previous checkpoints and results. The build also fingerprints the engine sources and
WebAssembly dependency lock so changed code or packages cannot resume incompatible counts. Atomic step checks reject concurrent stale writes with
HTTP 409; clients can reread the status and retry. Only the latest run for an event is retained.

## HTTP surface

| Request | Result |
| --- | --- |
| `GET /health` | Runtime package versions |
| `PUT /seasons/{year}` | Configure normalized prior-season inputs and an optional rule pack; changes restart replay |
| `GET /seasons/{year}` | Read the revision, event cursor and initialization scale |
| `PUT /seasons/{year}/events/{code}` | Import event metadata, teams, qualification schedule and optional score breakdowns |
| `POST /seasons/{year}/step` | Rate one event and checkpoint, or report the imported corpus complete |
| `GET /seasons/{year}/priors/{code}` | Read roster total/component priors, K/M schedules and their season revision |
| `POST /seasons/{year}/forecasts/{code}` | Assemble and save an event snapshot from models, context and official progress |
| `PUT /imports/{season}-{code}` | Atomically import FIRST payloads and explicit model parameters |
| `GET /imports/{season}-{code}` | Read the current imported source payloads and parameters, or 404 |
| `PUT /events/{code}` | Validate and save prepared inputs at a new revision |
| `POST /events/{code}/run` | Recompute from the supplied priors and save the current revision's result |
| `GET /events/{code}` | Read the current revision's completed result, or 404 |
| `PUT /forecasts/{code}` | Validate and save a complete snapshot; identical documents preserve the revision |
| `POST /forecasts/{code}/step` | Advance or resume one bounded Monte Carlo batch |
| `POST /forecasts/{code}/dispatch` | Dispatch or reconcile this event's durable Workflow without starting other events |
| `POST /forecasts/{code}/retry` | Explicitly release a failed event and dispatch recovery from its checkpoint |
| `GET /forecasts/{code}` | Read current progress or the completed event forecast, or 404 |
| `PUT /forecasts/{code}/result` | Save a converged native result for the exact input revision, snapshot hash and engine |
| `GET /forecasts/{code}/export` | Authenticated atomic read of a matching current snapshot, completed result and engine version |

Recomputing starts from the input priors so scores are not applied twice. An input correction invalidates the old
result. Publication checks the input revision in the write statement so an older computation cannot overwrite a
new revision. A concurrent input change returns 409 and requires another run.

Inputs are capped at 128 teams, 1,000 matches and 512,000 bytes. Rule-pack bracket coverage can impose a smaller team
limit. Independent match forecasts use score-draw batches of 256. Event input is buffered as one bounded JSON document.
Season replay keeps a bounded initialization prefix and one event in memory, rather than buffering all season matches.

The default configuration permits unauthenticated requests only on loopback hostnames. The separate benchmark
environment requires a bearer token for every request, including reads and health checks. Local success does not
establish that the engine fits the production 128 MB memory limit.

## Unattended forecast computation

The `ForecastWorkflow` advances stored snapshots without a client driving `/step` requests. The local configuration
includes a minute-based Cron Trigger; the hosted benchmark keeps `crons = []` so benchmarks remain operator-driven.
To enable hosted dispatch, apply migration `0006_forecast_jobs.sql`, set `env.benchmark.triggers.crons` to
`["* * * * *"]` and deploy the matching backend. This schedules every current stored snapshot that has no completed
or exhausted result. It does not fetch FIRST data, replay seasons or assemble snapshots. Those stages still need their
existing native commands. Competition-day automation must supply updated
snapshots, including calibrated models and current event context, before this dispatcher can compute them.
The [native static publisher](../frontend/README.md#automatic-static-publication) exports changed forecasts and deploys
them to the separate `canali-data` Static Assets deployment, with one catalog per season and region. Application
deployments are independent of forecast publication; public reads invoke neither computation Workers nor D1.

The latest `forecast_input` is the desired revision. An atomic D1 claim records one active job per event, and its
Workflow instance ID is derived from the event, input revision, engine and continuation generation. Identical snapshot
uploads preserve the revision. Repeated or concurrent ticks recover the same instance, including when a dispatcher
stops between saving the claim and creating the Workflow. Ownership has no timeout: a slow run retains its claim until
Cloudflare reports that it has stopped. Paused instances continue holding their event.

New inputs coalesce while that job runs. If revisions 43 and 44 arrive during revision 42, the next job uses revision
44. Each batch checks ownership, expected revision and engine; existing atomic checkpoint writes reject stale inputs,
season dependencies and duplicate writes. The dispatcher never makes an old job compute a newer snapshot. Completed
and exhausted outcomes remain in D1 after Workflow retention expires. Exhaustion requires revised simulation settings
or inputs before another automatic run; it is not treated as a converged forecast.

Each instance executes at most 256 bounded batches. If more work remains, the next tick starts a serial continuation
with a new generation and the existing D1 checkpoint. Simulation steps explicitly disable automatic retries. Any step
failure, including a CPU or memory limit, ends the instance instead of repeating charged computation. An instance that
ends errored or terminated is recorded as `errored` and blocks automatic computation for that event, even after new
match results, input revisions or engine changes. Other events continue. Failed events require explicit operator
recovery; there is no automatic exception classification or added I/O retry loop. Repeating an interrupted dispatch
can still recover creation of the same deterministic instance without creating concurrent simulations. Dispatch
errors are logged by event and cause the scheduled invocation to fail after attempting the other events.

Inspect instance status in Cloudflare Workflows and ownership with a D1 query:

```powershell
uv run pywrangler d1 execute canali-benchmark --env benchmark --remote --command "SELECT * FROM forecast_job"
```

After correcting the failure's cause, send an authenticated `POST /forecasts/{code}/retry`. This route atomically
releases only a recorded failed job and dispatches a fresh instance using the current deployment. Unchanged inputs
and engine resume the existing D1 checkpoint, including a checkpoint whose write succeeded before its response was
lost. Changed inputs or engine start the latest forecast with a fresh checkpoint. Recovery also works after the old
Workflow instance expires. Concurrent recovery requests cannot create two active instances. Repeating a request after
recovery has started returns 409; read status with `/dispatch` before retrying again. If dispatch is interrupted after
the explicit release, subsequent ticks recover the authorized dispatch. Resolving a source dependency can also be
necessary before released work becomes eligible. For a local runtime check, start `pywrangler dev --test-scheduled`,
upload a snapshot and invoke Wrangler's local scheduled API:

```powershell
Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8787/cdn-cgi/local/explorer/api/local/scheduled?worker=canali-local' -ContentType 'application/json' -Body '{"cron":"* * * * *"}'
```

Read `/forecasts/{code}` until it reports `complete` or `exhausted`. Calling the scheduled API repeatedly preserves
the same active instance. Apply local migrations before starting the server, as in the local setup above.

## Compute locally and publish to hosted D1

The native runner uses the same event simulator as the Worker. D1 retains the frozen inputs and completed forecast;
the existing frontend publisher exports them without running another simulation. Deploy matching backend sources
before uploading results. Hosted requests use the same benchmark and Cloudflare Access credentials as the publisher.

For the 2025–26 DECODE season, prepare Pennsylvania component ratings in a running local Worker:

```powershell
uv run python worker/replay_season.py 2025 --region USPA --rule-pack warehouse/rule_packs/2025_decode.toml
uv run python worker/assemble_forecast.py 2025 USPAHAQ1 --models worker/fixtures/forecast.json --context worker/fixtures/forecast-context.json --after-qualifications
uv run python worker/local_forecast.py var/worker-forecast/2025-USPAHAQ1/snapshot.json --event 2025-USPAHAQ1 --base-url https://canali-benchmark.mjhanson.workers.dev
```

Repeat assembly and native computation for the other events. `--after-qualifications` requires a complete scored
qualification schedule and reconstructs the event before alliance selection and playoffs. Played qualifications stay
fixed; later alliances and playoff results are excluded. An explicit `--through N` also permits a historical snapshot
at the end of qualifications; omitting a cutoff includes published selection and playoff results.

The fixture's score/RP coefficients and empty award history are demo assumptions, and its null advancement slots
produce no advancement estimates. Pennsylvania-only replay also omits teams' competition history outside the region
and prior-season carryover unless supplied separately. State these limitations in the publication label.

`local_forecast.py` saves counts, RNG state and the report beside the snapshot in `native-result.json`, replacing the
checkpoint atomically after each batch. Repeating the command resumes matching inputs and engine, or retries an upload
without recomputing a completed run. Changed inputs or engine start a fresh native checkpoint. An exhausted run is
saved locally but never published as complete. The command clears the local `season_prior` dependency because revision
numbers belong to their originating database; hosted inputs are frozen publications, refreshed by repeating the command.
This uploads simulation inputs and results, rather than copying the local season corpus or rating-history tables.

The authenticated result endpoint validates roster, rank distributions, probability bounds, iteration limits and
convergence. Its atomic write rejects changed inputs or linked season revisions. Results carry the source fingerprint
from the native runner; the backend does not rerun the simulation to prove the operator's numerical output.

Prepare the catalog and matching browser engine with `frontend/publish.py`, then build/deploy the frontend as described
in the [frontend README](../frontend/README.md#automatic-static-publication). Its watcher exports and deploys later
completed forecasts automatically while reusing the compiled frontend. Public visitors read only static assets.

## Hosted benchmark

See the [October 2026 convergence and execution measurements](../docs/forecast-benchmark-2026-10-21.md) for
local iteration counts, the hosted CPU-cap retry behavior and the remaining limits of the cost estimate.

The project's deployed benchmark is `https://canali-benchmark.mjhanson.workers.dev`; it requires both a Cloudflare
Access service token and the benchmark bearer token.

The `benchmark` environment is an opt-in deployment with its own D1 database, a required `BENCHMARK_TOKEN` secret,
and invocation logs. It uses a workers.dev address, with preview URLs disabled. It is configured for Workers Paid,
with `cpu_ms = 300000` to allow up to five CPU minutes per invocation. A five-second cap caused repeated CPU-limit
interruptions during hosted Workflow execution across multiple batches, despite each batch's elapsed-time budget.
The free plan can be used for deployment and basic checks
after removing that setting, but its 10 ms CPU limit may stop simulation requests. The configured cap bounds one invocation;
it is not an account spending cap.
Neither local development nor a dry-run build requires a paid subscription or creates remote resources.

First check the bundle locally, from `worker/`:

```powershell
uv run pywrangler deploy --env benchmark --dry-run --outdir ../var/worker-benchmark/build
```

The checked-in benchmark database ID belongs to the project's Cloudflare account. Reuse it for this account.
For another account, log in and create a separate database:

```powershell
npx wrangler login
uv run pywrangler d1 create canali-benchmark
```

For that new account, replace `database_id` under `env.benchmark.d1_databases` in `wrangler.toml` with the returned UUID.
Update `BACKEND_HOST` in `remote_access.py` to that account's backend hostname before using the hosted clients.
Leave the local binding unchanged. Apply migrations to the benchmark database and configure a random secret of at
least 32 characters, kept in your password manager. PowerShell's masked prompt avoids putting it in command history:

```powershell
uv run pywrangler d1 migrations apply canali-benchmark --env benchmark --remote
$env:CANALI_BENCHMARK_TOKEN = Read-Host 'Benchmark token' -MaskInput
$env:CANALI_BENCHMARK_TOKEN | uv run pywrangler secret put BENCHMARK_TOKEN --env benchmark
uv run pywrangler deploy --env benchmark
```

The Worker rejects all requests until the secret is configured correctly. FIRST credentials stay in the native
importer; they are not needed by the deployed benchmark. This environment is for private testing, not public traffic.
If a benchmark token is already configured, reuse it rather than replacing it. The local `.dev.vars.benchmark` file
is ignored by Git and can hold the same token for local testing. Keep a copy in your password manager; Cloudflare
does not return the saved secret's value.

### Cloudflare Access

Protect only `canali-benchmark` with a Worker-level Access application for **all traffic**. This covers its production
and preview addresses, routes and future custom domains. Keep `canali-frontend` public; browsers read static forecasts
and perform custom simulations locally. The native publisher uses authenticated private exports and holds deployment
credentials. Access is configured separately from Wrangler deployments and requires an
initial Zero Trust Free organization in the Cloudflare account.

The project's active policy is **Canali native tools only**, with Service Auth restricted to the **Canali native tools**
token. The token expires October 20, 2027. Its current credentials are stored locally in the ignored file below;
copy them to your password manager or CI secret store when needed, without adding them to Git.

Create a service token for native Canali tools and a **Service Auth** policy that includes only that token. Do not
use an Everyone or Bypass policy. Retain the Worker's existing benchmark bearer token as a second check. Access
rejects unauthenticated requests before they invoke Python; rejecting a request only inside Python still meters the
invocation. The per-invocation CPU limit remains useful for authorized work and is not a monthly spending cap.

Both `benchmark.py` and `frontend/publish.py` send `CF-Access-Client-Id` and `CF-Access-Client-Secret`, as well as
`Authorization: Bearer ...`, to the project's exact HTTPS backend hostname. Redirects are disabled. For hosted
commands, set the existing `CANALI_BENCHMARK_TOKEN` and the two service-token environment variables:

```powershell
$env:CANALI_ACCESS_CLIENT_ID = Read-Host 'Access client ID'
$env:CANALI_ACCESS_CLIENT_SECRET = Read-Host 'Access client secret' -MaskInput
```

Alternatively, save the Access pair as JSON in `worker/.dev.vars.access.json`, which Git ignores:

```json
{
  "CANALI_ACCESS_CLIENT_ID": "your-client-id.access",
  "CANALI_ACCESS_CLIENT_SECRET": "your-client-secret"
}
```

Both environment variables must be set together to override the file. The benchmark bearer token still comes from
`CANALI_BENCHMARK_TOKEN`. Keep all credentials out of generated assets and Vite variables. Service tokens expire;
renew or replace the stored pair before expiry and update the application's Service Auth policy if its token ID changes.
Local loopback commands do not read this file or send credentials. The import, replay and assembly CLIs remain local-only.

Verify protection with anonymous requests (including `/health` and POST routes), a request with only the benchmark
bearer token, one with only the Access pair, and one with both. Only the last should reach the protected backend
successfully. Check that the public frontend still loads. Missing or invalid Access credentials must be rejected by
Access, while a valid Access pair with no benchmark token must receive the Worker's own JSON `access denied` response.

### Run a hosted benchmark

Use the real snapshot produced by the local assembly command, or `fixtures/forecast.json` for a synthetic trial:

```powershell
uv run python benchmark.py ../var/worker-forecast/2025-USNJCMPPKWY/snapshot.json --base-url https://canali-benchmark.mjhanson.workers.dev
```

The benchmark uploads a frozen snapshot without its local D1 season-revision dependency. It runs sequential batches,
stops after at most 64 step requests by default, and saves timings, HTTP statuses, Cloudflare ray IDs, progress and the
final forecast under `var/worker-benchmark/report.json`. Failures also leave a report. It does not retry requests
automatically. To resume after a failure or request budget, pass the report's event key with `--event`; uploading the
same snapshot preserves its checkpoint. Omitting `--event` creates a fresh run. Use `--output` to keep separate reports.
An `exhausted` result means the iteration budget was reached before convergence, rather than a runtime failure.

Elapsed timings include network and D1 waits; they are not CPU measurements. While running, use another terminal:

```powershell
uv run pywrangler tail --env benchmark --format json
```

Inspect the benchmark Worker's invocation logs and metrics in Workers & Pages for CPU time, wall time and resource
errors, and the benchmark D1 database's metrics for rows read and written. Use the report's timestamps, event path and
ray IDs to identify the test traffic. Compare total deployed CPU and D1 usage across a fresh complete run, then scale
by the expected number of event refreshes when estimating cost. The frozen-snapshot benchmark measures forecast
execution and checkpointing; season replay, assembly and ingestion need separate deployed measurements.
Cloudflare does not expose a reliable per-request peak-memory counter here: successful execution tests whether this
case fits, while resource-limit errors require further diagnosis. Repeated requests are warm-runtime measurements;
the first request may include initialization, but is not guaranteed to be a cold start.

For an authenticated local trial, put `BENCHMARK_TOKEN="your-random-token"` in the ignored `.dev.vars.benchmark`,
set `CANALI_BENCHMARK_TOKEN` to the same value in the client terminal, and run:

```powershell
uv run pywrangler d1 migrations apply canali-benchmark --env benchmark --local
uv run pywrangler dev --env benchmark --ip 127.0.0.1 --port 8788
```

Then use `benchmark.py --base-url http://127.0.0.1:8788` with either snapshot. This verifies authentication and database
isolation locally; configured CPU limits are enforced only on Cloudflare.
See Cloudflare's [configuration](https://developers.cloudflare.com/workers/wrangler/configuration/),
[metrics](https://developers.cloudflare.com/workers/observability/metrics-and-analytics/) and
[D1 pricing and usage](https://developers.cloudflare.com/d1/platform/pricing/) documentation.

## Tests

```powershell
uv run python prepare.py
uv run pytest
uv run ruff check src/entry.py src/access.py src/compute.py src/snapshot.py src/forecast.py src/forecast_store.py src/first_event.py src/season.py src/components.py src/assemble.py prepare.py tests smoke.py import_event.py replay_season.py assemble_forecast.py benchmark.py
```

The unit tests check ratings against a closed-form ridge solution, simultaneous EPA updates, deterministic reruns
and invalid inputs. Event tests compare JSON checkpoint/resume with the native runner, generated schedules and fixed
published alliances. SQL tests reject duplicate writes and obsolete revisions and engines. `smoke.py` additionally
exercises the real local Worker and D1 binding, including unchanged imports, source corrections and clearing obsolete
source attribution. Import tests cover foul subtraction, missing results, station order, no-shows and surrogates.
Season tests compare event checkpoints with one native replay, exercise the initialization window, and verify suffix
invalidation, configuration and engine resets, stale-write rejection and prior revision checks. The smoke check also
replays a synthetic season through real local D1, compares native pre-event and post-event EPA, and corrects an earlier score.
Component tests compare individual series with native replay, cover state-scored and boolean fields, reject incomplete
or inconsistent breakdowns without replacing valid data, and prevent mixed rule-pack fingerprints. The D1 smoke check
exercises both total-only and component replay.
Assembly tests verify native ratings at the cutoff, recovered scoring fields, station flags, roster attributes,
masking of later outcomes, generated schedules, explicit context, identical-input resume, and stale-season rejection
on snapshot writes, checkpoint writes and result reads. A SQLite-backed assembled forecast is compared with native
simulation.
Full-event tests cover seated alliances, tied-game replay, completed finals, playoff rating updates, no-shows, foul
subtraction, invalid bracket prefixes and preserving valid inputs after rejected corrections. Export tests verify
that only completed matching revisions and engines are exposed and that season corrections hide stale results.
Access tests cover fail-closed hosted authentication and loopback-only local access. Benchmark client tests cover
progress, request budgets and HTTP failures.
Dispatcher tests cover concurrent ticks, interrupted creation, revision coalescing, persistent completion, explicit
failure recovery, serial continuation and stale-job rejection, using the actual migration and claim SQL against SQLite.
