#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "usage: run-container.sh NAME LOG [docker run args...]" >&2
  exit 2
fi

name=$1
log=$2
shift 2

# shellcheck disable=SC2329  # invoked from the trap handlers below
cleanup() {
  status=${1:-$?}
  trap - EXIT INT TERM
  docker stop --time 10 "$name" >/dev/null 2>&1 || true
  docker rm -f "$name" >/dev/null 2>&1 || true
  exit "$status"
}

trap 'cleanup 130' INT
trap 'cleanup 143' TERM
trap 'cleanup $?' EXIT

docker rm -f "$name" >/dev/null 2>&1 || true
docker run --name "$name" "$@" >"$log" 2>&1 &
pid=$!
set +e
wait "$pid"
status=$?
set -e
cat "$log"
exit "$status"
