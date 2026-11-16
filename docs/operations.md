# Operations

## Daily forecast

[`daily-forecast.yml`](../.github/workflows/daily-forecast.yml) has three jobs:

- `preflight` runs on a hosted runner, validates configuration, resolves the
  cycle, and recovers an already committed cycle before any expensive work.
- `forecast` runs on an explicit trusted Linux arm64 self-hosted worker. It
  restores the WPS geography archives, builds the solver image, acquires
  forcing, solves, stages, publishes, and uploads diagnostics.
- `notify` runs on a hosted runner and dispatches the consumer webhook after a
  live publication or a committed-cycle recovery.

The workflow targets a self-hosted worker because a personal repository cannot
assign organization-hosted larger runners. Use a trusted self-hosted worker, or
run the workflow from an organization-owned execution repository. Do not
register privileged production workers with pull-request workflows; this
workflow triggers only on a schedule and `workflow_dispatch`.

Set these repository variables and secrets before the first scheduled run:

| Name | Kind | Purpose |
| :--- | :--- | :--- |
| `AV_WRF_PRODUCT` | variable | Product id under `config/products/`; scheduled runs read it. |
| `AV_WRF_RUNNER` | variable | Required label for the trusted Linux arm64 self-hosted worker. |
| `AV_WRF_MPI_RANKS` | variable | MPI ranks frozen in the RunLock; defaults to `4`. |
| `AV_WRF_NATIVE_TIMEOUT_S` | variable | Optional per-native-stage timeout. |
| `AV_WRF_STORE_URI` | secret | Durable destination; must use `s3://bucket/prefix`. |
| `AV_WRF_S3_ENDPOINT` | variable | S3-compatible endpoint for non-AWS stores. |
| `AWS_REGION` | variable | S3 region; defaults to `us-east-1`. |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN` | secrets | Store credentials. |
| `AV_WRF_WEBHOOK_URL`, `AV_WRF_WEBHOOK_TOKEN` | secrets | Consumer dispatch. |

The forecast worker needs Linux on `aarch64`, Docker, `curl`, `tar`, `gzip`,
`bzip2`, `jq`, `awk`, and `df`. The workflow requires at least 80 GiB free
before downloading geography and at least 40 GiB free after extraction for the
solver image, scratch, and store. Each runtime container runs through
`scripts/run-container.sh`, which stops and removes it on cancellation; a
run-scoped cleanup step removes stragglers before artifact upload.

## Execution limits

The forecast job sets `timeout-minutes: 720` as a self-hosted guard. GitHub-
hosted runners cap a job at six hours, so the forecast job belongs on a
self-hosted worker. The preflight and notify jobs stay on hosted runners and
use 30- and 10-minute limits. Measure the cold and warm paths on the selected
worker before relying on the daily schedule.

## Geography cache

The first forecast run downloads `geog_high_res_mandatory.tar.gz` (2.6 GiB
compressed, about 29 GiB extracted) and `lake_depth.tar.bz2` from UCAR. The
workflow restores and saves the compressed archives separately, so a later
solver or publication failure does not force another download. The cache key is
`wps-geog-high-res-v1`; bump it when the archives change. The repository cache
defaults to 10 GB and is shared with the solver BuildKit cache. Raise the limit
in the organization billing settings when the archives and image layers need
more space.

## Recovery and live outcomes

The preflight job runs `av-wrf recover --probe`. The forecast job repeats the
recovery check at the start of every attempt, so a failed-job rerun does not
recompute a committed cycle. It rechecks freshness at worker start and again
before solve. A committed cycle is validated against the published inventory and
advances `latest.json` without rerunning the forecast. A live run requires the
publish result `advanced` or `current`; `outside_live_window` retains the archive
and fails the live outcome, and `newer` reports a superseded cycle.

Webhook delivery is a separate `notify` job. Use
[`notify.yml`](../.github/workflows/notify.yml) to retry delivery for an explicit
`product` and `cycle` without the forecast worker, geography, or solver. The
notification path verifies the committed receipt and diagnostics before
dispatch and leaves `latest.json` unchanged. New forecast attempts use
`gha-<run_id>-<run_attempt>`; a rerun recovers a committed cycle before
recomputing it.

## Manual runs

Use `workflow_dispatch` on the daily workflow to pass a `cycle` (ISO UTC or
`YYYYMMDD/HH`) and an optional `product`. With no `cycle`, the workflow resolves
the latest cycle whose configured schedule trigger has passed and which remains
live. Keep the workflow cron at or after the product's `schedule_cron_utc`.
Logs and receipts are retained as a seven-day artifact, including failed native
attempts under hidden scratch directories.
