# Operations

## Daily forecast

[`daily-forecast.yml`](../.github/workflows/daily-forecast.yml) runs the configured
product on a daily schedule and through `workflow_dispatch`. It builds the pinned
solver image with the shared `solver-arm64` BuildKit cache, restores the WPS
geography archives, plans an immutable cycle, acquires forcing, solves, stages,
publishes, and optionally dispatches the consumer webhook.

Set these repository variables and secrets before the first scheduled run:

| Name | Kind | Purpose |
| :--- | :--- | :--- |
| `AV_WRF_PRODUCT` | variable | Product id under `config/products/`; scheduled runs read it. |
| `AV_WRF_RUNNER` | variable | Larger-runner label. The workflow rejects runners with less than 50 GiB free. |
| `AV_WRF_MPI_RANKS` | variable | MPI ranks frozen in the RunLock; defaults to `4`. |
| `AV_WRF_NATIVE_TIMEOUT_S` | variable | Optional per-native-stage timeout. |
| `AV_WRF_STORE_URI` | secret | Destination store, for example `s3://bucket/prefix` or `file:///store`. |
| `AV_WRF_S3_ENDPOINT` | variable | S3-compatible endpoint for non-AWS stores. |
| `AWS_REGION` | variable | S3 region; defaults to `us-east-1`. |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN` | secrets | Store credentials. |
| `AV_WRF_WEBHOOK_URL`, `AV_WRF_WEBHOOK_TOKEN` | secrets | Optional consumer dispatch. |

Enable larger runners in the organization or enterprise settings and assign the
runner group to the repository. Use the label your organization created, for
example `ubuntu-24.04-arm-32core`. The workflow needs at least 50 GiB of free
disk for the extracted geography, solver image, scratch, and store.

## Geography cache

The first run downloads `geog_high_res_mandatory.tar.gz` (2.6 GiB compressed,
about 29 GiB extracted) and `lake_depth.tar.bz2` from UCAR, then extracts both.
Later runs restore the compressed archives from the Actions cache and extract
them again. The cache key is `wps-geog-high-res-v1`; bump it when the archives
change. The default repository cache is 10 GB, shared with the solver BuildKit
cache. Raise the limit in the organization billing settings when the archives and
image layers need more space; excess storage is billed.

## Cold and hot starts

A cold start downloads and extracts the geography and builds the solver image
without a BuildKit cache. A hot start restores the archives and reuses the
BuildKit cache, so extraction and the forecast run remain. Both paths run the
same commands and produce the same immutable cycle receipt.

## Manual runs

Use `workflow_dispatch` to pass a `cycle` (ISO UTC or `YYYYMMDD/HH`) and an
optional `product`. With no `cycle`, the workflow resolves the latest cycle whose
configured schedule trigger has passed and which remains live. Keep the workflow
cron at or after the product's `schedule_cron_utc`; a later run still resolves
the latest due cycle. Logs and receipts are retained as a seven-day artifact.
