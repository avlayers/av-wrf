"""Tell the consumer a published cycle is ready.

The consumer admits `repository_dispatch` events of type `upstream-cycle-ready`
with `environment`, `model`, and an exact UTC hour.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import cast

import httpx

from av_wrf.cli.env import load_runtime_env
from av_wrf.contracts import ReadyReceipt
from av_wrf.core import parse_cycle_arg
from av_wrf.runner import load_bundle, load_ready_receipt, open_store, reconcile_dispatch
from av_wrf.runner.dispatch import dispatch_receipt_key
from scripts.forecast_job import load_targets, repository_parts

EVENT_TYPE = "upstream-cycle-ready"
_HOUR = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:00:00Z$")
_OPS = Path("config/operations/forecasts.yaml")
_API = "https://api.github.com"


def dispatch_envelope(environment: str, model: str, cycle: str) -> dict[str, object]:
    """Build the consumer event. The cycle must be an exact UTC hour."""
    if environment not in {"staging", "production"}:
        raise ValueError("environment must be staging or production")
    if not model or "/" in model or any(char.isspace() for char in model):
        raise ValueError("model must be a single consumer model id")
    if not _HOUR.fullmatch(cycle):
        raise ValueError(f"cycle must be an exact UTC hour, got {cycle!r}")
    return {
        "event_type": EVENT_TYPE,
        "client_payload": {"environment": environment, "model": model, "cycle": cycle},
    }


def post_repository_dispatch(
    token: str,
    repository: str,
    envelope: dict[str, object],
    *,
    client: httpx.Client | None = None,
) -> int:
    """POST one repository dispatch. Return the HTTP status."""
    clean = token.strip()
    if not clean:
        raise ValueError("AV_WRF_DISPATCH_TOKEN is required")
    owner, name = repository_parts(repository)
    url = f"{_API}/repos/{owner}/{name}/dispatches"
    headers = {
        "Authorization": f"Bearer {clean}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "av-wrf/0.1.0",
        "Content-Type": "application/json",
    }
    body = json.dumps(envelope, separators=(",", ":")).encode("utf-8")
    try:
        response = _post(url, headers, body, client)
    except httpx.HTTPError:
        raise RuntimeError("Repository dispatch request failed") from None
    if response.status_code != 204:
        raise RuntimeError(f"Repository dispatch rejected the event: {response.status_code}")
    return response.status_code


def dispatch_published(
    config_dir: Path,
    ops_path: Path,
    *,
    product_id: str,
    cycle: str,
    attempt: str,
    store_uri: str,
    token: str,
    environment: str = "",
    client: httpx.Client | None = None,
) -> dict[str, object]:
    """Dispatch the published receipt's cycle. Fail if that receipt is absent."""
    if not store_uri:
        raise ValueError("AV_WRF_STORE_URI is required")
    target = next((item for item in load_targets(ops_path) if item.product_id == product_id), None)
    if target is None:
        raise ValueError(f"Product {product_id!r} is not an enabled forecast run")
    if not target.notify:
        raise ValueError(f"Product {product_id!r} does not send a consumer event")
    bundle = load_bundle(config_dir, product_id)
    origin, partition = parse_cycle_arg(cycle, bundle.product)
    store = open_store(store_uri, s3=load_runtime_env().s3)
    receipt = load_ready_receipt(store, bundle.product.model_id, partition)
    if receipt.publish_origin_utc != origin:
        raise ValueError("Published receipt origin does not match the requested cycle")
    if attempt and receipt.attempt_id != attempt:
        raise ValueError(f"Published attempt is {receipt.attempt_id!r}")
    chosen = environment.strip() or target.consumer_environment
    envelope = dispatch_envelope(chosen, target.consumer_model, origin)
    repository = target.consumer_repository
    destination = f"github:{repository}"
    if not token.strip():
        raise ValueError("AV_WRF_DISPATCH_TOKEN is required")

    def send(_receipt: ReadyReceipt) -> int:
        return post_repository_dispatch(token, repository, envelope, client=client)

    if reconcile_dispatch(store, receipt, send, destination_id=destination):
        return {"status": "dispatched", "cycle": origin}
    raw = store.get_bytes(dispatch_receipt_key(receipt, destination))
    if raw is None or _stored_dispatch_state(raw) != "delivered":
        raise RuntimeError("Repository dispatch did not finish; retry")
    return {"status": "already_dispatched", "cycle": origin}


def _stored_dispatch_state(raw: bytes) -> str:
    loaded = cast(object, json.loads(raw.decode("utf-8")))
    if not isinstance(loaded, dict):
        raise RuntimeError("Dispatch state is unreadable")
    state = loaded.get("state")
    if not isinstance(state, str) or not state:
        raise RuntimeError("Dispatch state is unreadable")
    return state


def _post(
    url: str, headers: dict[str, str], body: bytes, client: httpx.Client | None
) -> httpx.Response:
    if client is not None:
        return client.post(url, headers=headers, content=body)
    with httpx.Client(timeout=30.0, follow_redirects=False) as owned:
        return owned.post(url, headers=headers, content=body)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-dir", type=Path, default=Path("config"))
    parser.add_argument("--ops", type=Path, default=_OPS)
    parser.add_argument("--product", required=True)
    parser.add_argument("--cycle", required=True)
    parser.add_argument("--attempt", default="")
    parser.add_argument("--environment", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Dispatch one published cycle and print a one-line status."""
    args = _parser().parse_args(argv)
    env = load_runtime_env()
    try:
        result = dispatch_published(
            args.config_dir,
            args.ops,
            product_id=str(args.product),
            cycle=str(args.cycle),
            attempt=str(args.attempt),
            store_uri=env.store_uri,
            token=env.dispatch_token,
            environment=str(args.environment),
        )
    except (FileNotFoundError, ValueError, RuntimeError, OSError) as exc:
        print(f"forecast_dispatch: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
