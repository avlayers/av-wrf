# Operations

## Forecast

Each run in [`forecasts.yaml`](../config/operations/forecasts.yaml) owns its
`schedule_cron_utc` and `scheduled` flag. A run with `scheduled: false` is
manual: its cron snaps an empty cycle, and
[`forecast.yml`](../.github/workflows/forecast.yml) does not register that
cron. The workflow lists every scheduled cron because GitHub schedules are
static. A schedule trigger starts only the scheduled operations that declare
that cron. The workflow names no product.

`resolve` runs on a 2 vCPU spot runner, refuses any ref other than `main`, and
emits the runs for the triggering cron or the dispatch input. `gate` uses the
`forecast` environment and skips a cycle whose durable store already has
`_ready.json`. Workflow concurrency is `forecast` with `cancel-in-progress:
false`, so a second dispatch waits and then sees that receipt. Mirror, prepare,
solve, and publish run only when something is still unpublished.

Prepare and publish stay on 2 vCPU spot runners. Each run sets `spot` on
its solver label. A spot interruption fails the job; the next attempt
downloads again. There is no
in-job sleep. The heavy runner is not requested until prepare succeeds.

The solver label lists the 32-vCPU EBS `8xlarge` types `c8g`, `c7g`, `m7g`,
`c9g`, `m8g`, `m9g`, `r7g`, and `r8g`, with `cpu=32` and
`image=ubuntu24-full-arm64`. `spot=true` is price-capacity-optimized.
List order is the on-demand fallback, starting at `c8g.8xlarge`. The
published solver image is `armv8.2-a+fp16+dotprod`, which those CPUs
implement. The first real run is the calibration. Its job summary records
wall time, sampled container memory, MPI ranks, the runner type and
lifecycle, and which cache hit. Replace the planning estimate from that
measurement. Do not raise the 1.5 job-timeout multiplier; change the
instance instead. Solve restores the wrfout cache before `wrf.exe` and
skips the solver when that restore matches its size manifest. Publish
fails when the wrfout cache misses.

## Environment

Restrict the `forecast` environment to the default branch. `gate`, `publish`,
and `dispatch` receive it. `dispatch` runs after a successful publish, and also
when the cycle receipt is already in the store. A failed POST can be retried.
The stack also needs `EnableEphemeralRegistry`
(`enable_ecr = true`). Mirror records the GHCR `sha256` digest and later jobs
pass that exact value as `AV_WRF_OCI_DIGEST`. When that tag is absent, mirror
starts the images workflow on main when no images run is active, then waits
for the push before the pull. Main builds that image on an 8 vCPU runner when
an image input changes. A pull request does not compile it.

The consumer event is `repository_dispatch` type `upstream-cycle-ready` with
`environment`, `model`, and the UTC hour. `AV_WRF_DISPATCH_TOKEN` creates that
event on the repository named in the allowlist. It needs contents write on
that repository. The workflow `GITHUB_TOKEN` cannot. Once GitHub has accepted
an attempt, a later run reports `already_dispatched`. Re-run the consumer
workflow to repeat its work. The consumer applies its own freshness window
after the event arrives. A slow solve can still publish a cycle the consumer
then rejects.

| Name | Purpose |
| :--- | :--- |
| `AV_WRF_STORE_URI` | Durable `s3://` destination. |
| `AV_WRF_S3_ENDPOINT` | Variable, or a secret of the same name. Empty uses AWS. |
| `AWS_REGION` | Variable. Empty uses `us-east-1`. |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | Store credentials. |
| `AWS_SESSION_TOKEN` | Optional secret. Leave empty for an R2 token. |
| `AV_WRF_DISPATCH_TOKEN` | Repository dispatch token for the consumer. |

The publisher stores the cycle with path-style `GetObject` and `PutObject`,
including conditional `If-Match` and `If-None-Match` writes. That is the R2 S3
API. Point `AV_WRF_S3_ENDPOINT` at `https://<accountid>.r2.cloudflarestorage.com`
and use an R2 access-key pair. `auto` and `us-east-1` both mean R2's `auto`
region. Object checksums are attached only when the operation requires them.

`runs-on/action` is the first step of every runs-on job. `extras` replaces the
whole list. Jobs that restore or save a cache and pull the solver image set
`extras=s3-cache+ecr-cache`. Cache objects live in the stack bucket and outlive
the ephemeral runner. A size manifest is written with each handoff; a short
restore fails before solve or post.

## Geography

The inputs cache stores the prepared bundle. Compressed UCAR archives use the
constant key `wps-geog-high-res-v1`: `geog_high_res_mandatory.tar.gz` and
`lake_depth.tar.bz2`. Prepare extracts them on the 2 vCPU runner only when the
inputs cache misses, inside the 45 minute cap. The extracted tree is not the
cache payload.

## Manual runs

`workflow_dispatch` runs one allowlisted operation from the default branch.
The product field is a single-choice menu of every operation, including
manual runs. Add the operation to the allowlist and one matching menu line.
Register a cron only when `scheduled` is true. `notify: false` publishes
without a consumer event, so the receipt keeps no delivered mark. Set
`notify: true` once the consumer registers the model. Until then the forecast
workflow skips the event and
[`notify.yml`](../.github/workflows/notify.yml) refuses it. The operations
cron must equal the product cron.
An empty cycle snaps to that operation's latest live origin. The 3 hour
lateness guard applies to schedule events: a schedule more than 3 hours after
its cron fails before any download. An explicit cycle must still be live. The
default attempt is `att-01`. An empty consumer environment uses the allowlist;
`staging` or `production` overrides it. A cycle that already has `_ready.json`
skips compute and, when the run notifies, sends the consumer event.
[`notify.yml`](../.github/workflows/notify.yml) sends that event by itself.
