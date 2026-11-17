# `av_wrf.cli` (`L4`)

Command-line argument router and the repository's sole `os.environ` reader.

```sh
uv run av-wrf check
```

Cycle commands require an explicit store URI. Set `AV_WRF_STORE_URI` for both
staging and publication, or set `AV_WRF_STAGE_STORE_URI` and pass `--store-uri`
to `publish`, `recover`, and `reconcile` for separate destinations.

`recover --probe` reports whether a cycle already has a committed `_ready.json`.
Without `--probe`, it validates the published inventory and advances
`latest.json` without rerunning the forecast.

Set `AV_WRF_WEBHOOK_URL` to an HTTPS endpoint with a plain URL path. For an
authenticated endpoint, set `AV_WRF_WEBHOOK_TOKEN` for Bearer authentication.
`av-wrf reconcile` posts that webhook. The scheduled forecast creates a
`repository_dispatch` with `AV_WRF_DISPATCH_TOKEN`.

- **Allowed imports:** `av_wrf.contracts`, `av_wrf.runner`,
  and Python CLI/env stdlib modules (`argparse`, `os` in `env.py` only).
- **Forbidden:** hardcoded product/region defaults, physics math, file codecs,
  retry loops, or direct storage queries.
