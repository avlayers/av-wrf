#!/usr/bin/env bash
# One forecast stage. A spot interruption fails the job; the next attempt downloads again.
set -euo pipefail

mode=${1:?usage: forecast_run.sh MODE}
scratch=${SCRATCH:?}
bundle="$scratch/bundle"
tiles="$scratch/geog-tiles"
archives="$scratch/geog-archives"
logs="$scratch/logs"
image=${IMAGE:-}
product=${PRODUCT:?}
cycle=${CYCLE:?}
attempt=${ATTEMPT:?}
partition=${PARTITION:?}
cleanup_name=""

cleanup() {
  if [[ -n "$cleanup_name" ]]; then
    docker stop --time 10 "$cleanup_name" >/dev/null 2>&1 || true
    docker rm -f "$cleanup_name" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

last_json() {
  uv run python -c 'import pathlib,sys
lines = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace").splitlines()
hits = [ln for ln in lines if ln.startswith("{") and ln.endswith("}")]
if not hits:
    raise SystemExit("no JSON status in " + sys.argv[1])
print(hits[-1])' "$1"
}

docker_av() {
  cleanup_name=$1
  local log=$2
  shift 2
  mkdir -p "$logs"
  docker rm -f "$cleanup_name" >/dev/null 2>&1 || true
  docker run --name "$cleanup_name" "$@" >"$log" 2>&1 &
  local pid=$!
  local sampler=""
  local status=0
  if [[ "${SAMPLE_MEMORY:-}" == "1" ]]; then
    sample_peak "$cleanup_name" "$scratch/solve-peak-bytes.txt" &
    sampler=$!
  fi
  wait "$pid" || status=$?
  if [[ -n "$sampler" ]]; then
    wait "$sampler" || true
  fi
  cat "$log"
  cleanup_name=""
  return "$status"
}

sample_peak() {
  local name=$1 dest=$2
  local max=0
  local _wait
  for _wait in 1 2 3 4 5 6 7 8 9 10; do
    if docker inspect -f '{{.State.Status}}' "$name" 2>/dev/null | grep -qx running; then
      break
    fi
    sleep 1
  done
  while docker inspect -f '{{.State.Status}}' "$name" 2>/dev/null | grep -qx running; do
    local usage bytes
    usage=$(docker stats --no-stream --format '{{.MemUsage}}' "$name" 2>/dev/null | awk '{print $1}')
    bytes=$(python3 -c 'import sys
raw = sys.argv[1] if len(sys.argv) > 1 else ""
units = {"B": 1, "KB": 10**3, "MB": 10**6, "GB": 10**9, "TB": 10**12,
 "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3, "TiB": 1024**4}
num = "".join(ch for ch in raw if ch.isdigit() or ch == ".")
suf = "".join(ch for ch in raw if ch.isalpha())
scale = units.get(suf, 0)
print(int(float(num) * scale) if num and scale else 0)' "$usage" 2>/dev/null || echo 0)
    if [[ "$bytes" =~ ^[0-9]+$ ]] && ((bytes > max)); then
      max=$bytes
    fi
    sleep 20
  done
  echo "$max" >"$dest"
}

base_mount=(
  --user "$(id -u):$(id -g)"
  -e HOME=/tmp
  -e AV_WRF_STAGE_STORE_URI=file:///scratch/bundle/stage
  -v "$bundle:/scratch/bundle"
)

require_image() {
  if [[ -z "$image" ]]; then
    echo "IMAGE is required" >&2
    exit 2
  fi
}

extract_geog() {
  mkdir -p "$archives" "$tiles"
  local high="$archives/geog_high_res_mandatory.tar.gz"
  local lake="$archives/lake_depth.tar.bz2"
  local high_url="https://www2.mmm.ucar.edu/wrf/src/wps_files/geog_high_res_mandatory.tar.gz"
  local lake_url="https://www2.mmm.ucar.edu/wrf/src/wps_files/lake_depth.tar.bz2"
  if [[ ! -s "$high" || ! -s "$lake" ]]; then
    curl -fL --retry 5 --retry-delay 10 --retry-all-errors -o "$high.part" "$high_url"
    curl -fL --retry 5 --retry-delay 10 --retry-all-errors -o "$lake.part" "$lake_url"
    mv "$high.part" "$high"
    mv "$lake.part" "$lake"
  fi
  rm -rf "$tiles"
  mkdir -p "$tiles"
  tar -xzf "$high" -C "$tiles" --strip-components=1
  tar -xjf "$lake" -C "$tiles"
  test -d "$tiles/topo_gmted2010_30s"
  test -d "$tiles/lake_depth"
  local available_kb
  available_kb=$(df -Pk "$scratch" | awk 'NR==2 {print $4}')
  if (( available_kb < 40 * 1024 * 1024 )); then
    echo "Need at least 40 GiB free after geography extraction" >&2
    exit 1
  fi
}

run_geog() {
  require_image
  local name="av-wrf-${GITHUB_RUN_ID:-local}-${attempt}-geog"
  local log="$logs/geog.out"
  rm -rf "$bundle/geog-work"
  docker_av "$name" "$log" --network none \
    "${base_mount[@]}" \
    -e "AV_WRF_NATIVE_TIMEOUT_S=${NATIVE_TIMEOUT_S:?}" \
    -v "$tiles:/opt/av-wrf/geog:ro" \
    "$image" geog --product "$product" --work-dir /scratch/bundle/geog-work
  last_json "$log" | uv run python -c 'import json,sys; print(json.load(sys.stdin)["geo_em"])' \
    >"$bundle/geog-file.path"
}

run_plan() {
  require_image
  local name="av-wrf-${GITHUB_RUN_ID:-local}-${attempt}-plan"
  local log="$logs/plan.out"
  mkdir -p "$bundle/stage"
  docker_av "$name" "$log" --network none \
    "${base_mount[@]}" \
    -e "AV_WRF_OCI_DIGEST=${OCI_DIGEST:?}" \
    -e "AV_WRF_MPI_RANKS=${MPI_RANKS:?}" \
    "$image" plan --product "$product" --cycle "$cycle" --attempt "$attempt" \
    --geog-file "$(cat "$bundle/geog-file.path")"
  last_json "$log" >/dev/null
}

run_acquire() {
  require_image
  local name="av-wrf-${GITHUB_RUN_ID:-local}-${attempt}-acquire"
  local log="$logs/acquire.out"
  mkdir -p "$bundle/grib" "$scratch/icon-cache"
  docker_av "$name" "$log" \
    "${base_mount[@]}" \
    -e "AV_WRF_MPI_RANKS=${MPI_RANKS:?}" \
    -v "$scratch/icon-cache:/scratch/icon-cache" \
    "$image" acquire --product "$product" --cycle "$cycle" --attempt "$attempt" \
    --grib-dir /scratch/bundle/grib --cache /scratch/icon-cache --workers 8
  last_json "$log" >/dev/null
}

run_prepare() {
  mkdir -p "$logs" "$bundle"
  if [[ "${INPUTS_HIT:-false}" == "true" ]]; then
    uv run python -m scripts.forecast_select verify-manifest "$bundle"
    run_plan
    return 0
  fi
  run_geog
  run_plan
  run_acquire
  uv run python -m scripts.forecast_select manifest "$bundle"
}

run_solve() {
  require_image
  local name="av-wrf-${GITHUB_RUN_ID:-local}-${attempt}-solve"
  local log="$logs/solve.out"
  local started=$SECONDS
  uv run python -m scripts.forecast_select verify-manifest "$bundle"
  SAMPLE_MEMORY=1
  set +e
  docker_av "$name" "$log" --network none --shm-size=2g \
    "${base_mount[@]}" \
    -e OMP_NUM_THREADS=1 \
    -e "AV_WRF_OCI_DIGEST=${OCI_DIGEST:?}" \
    -e "AV_WRF_MPI_RANKS=${MPI_RANKS:?}" \
    -e "AV_WRF_NATIVE_TIMEOUT_S=${NATIVE_TIMEOUT_S:?}" \
    "$image" solve --product "$product" --cycle "$cycle" --attempt "$attempt" \
    --work-dir /scratch/bundle/solve --grib-dir /scratch/bundle/grib \
    --geog-file "$(cat "$bundle/geog-file.path")"
  local status=$?
  set -e
  SAMPLE_MEMORY=0
  echo $((SECONDS - started)) >"$scratch/solve-seconds.txt"
  if [[ "$status" -ne 0 ]]; then
    return "$status"
  fi
  last_json "$log" >/dev/null
  uv run python -m scripts.forecast_select manifest "$bundle"
}

cycle_dir() {
  printf '%s\n' "${partition//\//_}"
}

run_post() {
  require_image
  local name="av-wrf-${GITHUB_RUN_ID:-local}-${attempt}-post"
  local log="$logs/post.out"
  local wrfout="/scratch/bundle/solve/$product/$(cycle_dir)/$attempt"
  uv run python -m scripts.forecast_select verify-manifest "$bundle"
  docker_av "$name" "$log" --network none \
    "${base_mount[@]}" \
    "$image" post --product "$product" --cycle "$cycle" --attempt "$attempt" \
    --wrfout-dir "$wrfout"
  last_json "$log" >/dev/null
}

pass_set_env() {
  docker_env=()
  local key
  for key in "$@"; do
    if [[ -n "${!key:-}" ]]; then
      docker_env+=(-e "$key")
    fi
  done
}

run_publish() {
  require_image
  local name="av-wrf-${GITHUB_RUN_ID:-local}-${attempt}-publish"
  local log="$logs/publish.out"
  pass_set_env AV_WRF_STORE_URI AV_WRF_S3_ENDPOINT AWS_REGION \
    AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
  docker_av "$name" "$log" \
    "${base_mount[@]}" \
    "${docker_env[@]}" \
    "$image" publish --product "$product" --cycle "$cycle" --attempt "$attempt"
  last_json "$log" | uv run python -c 'import json,sys
doc = json.load(sys.stdin)
latest = doc.get("latest")
ok = doc.get("status") == "published" and doc.get("destinations") == 1
ok = ok and isinstance(latest, list) and len(latest) == 1 and latest[0] in {"advanced", "current"}
if not ok:
    raise SystemExit("publication did not advance or confirm the live pointer: " + str(latest))
'
}

case "$mode" in
  extract-geog) extract_geog ;;
  prepare) run_prepare ;;
  solve) run_solve ;;
  post) run_post ;;
  publish) run_publish ;;
  *)
    echo "unknown mode: $mode" >&2
    exit 2
    ;;
esac
