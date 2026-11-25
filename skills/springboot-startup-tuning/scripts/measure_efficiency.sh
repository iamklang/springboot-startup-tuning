#!/usr/bin/env bash
# Measure steady-state cost per request of a Spring Boot app at a fixed arrival rate.
#
# Usage:
#   measure_efficiency.sh -r RATE [-d SECONDS] [-w WARMUP_S] [-c MAX_IN_FLIGHT] [-l LABEL] [-o CSV]
#                         [-m METRICS_BASE] [-R READY_URL] [-t TIMEOUT_S] [-H 'Name: v']...
#                         -u URL [-u URL ...] (-p PID | -- <java args>)
#
# Starts the app (or attaches to PID), waits for READY_URL (default: first -u URL) to answer
# 200, runs a warm-up phase at the same rate so JIT and caches settle, then a measured phase.
# Reports for the measured phase only:
#   achieved req/s, p50/p99/p99.9 from scheduled send time (open model, scripts/loadgen.py),
#   CPU ms per request      (process user+sys CPU time delta / requests),
#   KB allocated per request (Actuator jvm.gc.memory.allocated delta / requests, if exposed),
#   RSS MB at the end, and heap used MB after a final GC is not forced (Actuator, if exposed).
# METRICS_BASE defaults to http://HOST:PORT/actuator/metrics taken from the first -u URL.
# Use the same RATE, durations and URLs for every configuration you compare.
set -euo pipefail

RATE=""; DUR=60; WARM=30; CONC=64; LABEL="run"; CSV=""; METRICS=""; READY=""; TMO=180; PID=""
URLS=(); HDRS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    -r) RATE="$2"; shift 2;;
    -d) DUR="$2"; shift 2;;
    -w) WARM="$2"; shift 2;;
    -c) CONC="$2"; shift 2;;
    -l) LABEL="$2"; shift 2;;
    -o) CSV="$2"; shift 2;;
    -m) METRICS="$2"; shift 2;;
    -R) READY="$2"; shift 2;;
    -t) TMO="$2"; shift 2;;
    -p) PID="$2"; shift 2;;
    -u) URLS+=("$2"); shift 2;;
    -H) HDRS+=(-H "$2"); shift 2;;
    --) shift; break;;
    -h|--help) sed -n '2,19p' "$0"; exit 0;;
    *) echo "unknown option: $1" >&2; exit 2;;
  esac
done
JAVA_ARGS=("$@")
[[ -n "$RATE" ]] || { echo "need -r RATE" >&2; exit 2; }
[[ ${#URLS[@]} -gt 0 ]] || { echo "need at least one -u URL" >&2; exit 2; }
[[ -n "$PID" || ${#JAVA_ARGS[@]} -gt 0 ]] || { echo "need -p PID or -- <java args>" >&2; exit 2; }
[[ -n "$READY" ]] || READY="${URLS[0]}"
if [[ -z "$METRICS" ]]; then
  METRICS=$(python3 -c 'import sys,urllib.parse as u; p=u.urlsplit(sys.argv[1]); print(f"{p.scheme}://{p.netloc}/actuator/metrics")' "${URLS[0]}")
fi
HERE=$(cd "$(dirname "$0")" && pwd)
LOADGEN="$HERE/loadgen.py"

STARTED=""
LOG=$(mktemp "${TMPDIR:-/tmp}/eff.XXXXXX")
cleanup() {
  if [[ -n "$STARTED" ]]; then kill "$PID" 2>/dev/null || true; wait "$PID" 2>/dev/null || true; fi
}
trap cleanup EXIT

if [[ -z "$PID" ]]; then
  java "${JAVA_ARGS[@]}" > "$LOG" 2>&1 &
  PID=$!; STARTED=1
fi

# process CPU seconds (user+sys), Linux /proc or ps elsewhere
cpu_s() {
  if [[ -r /proc/$PID/stat ]]; then
    awk -v hz="$(getconf CLK_TCK)" '{printf "%.3f\n", ($14+$15)/hz}' "/proc/$PID/stat"
  else
    ps -o time= -p "$PID" | awk '{n=split($1,a,":"); s=0; for(i=1;i<=n;i++){s=s*60+a[i]} printf "%.3f\n", s}'
  fi
}
rss_mb() { ps -o rss= -p "$PID" | awk '{printf "%.0f\n", $1/1024}'; }
metric() { # metric name -> first measurement value, or NA
  curl -s --max-time 3 ${HDRS[@]+"${HDRS[@]}"} "$METRICS/$1" 2>/dev/null \
    | python3 -c 'import sys,json
try: print(json.load(sys.stdin)["measurements"][0]["value"])
except Exception: print("NA")'
}

start=$(date +%s); ok=0
while (( $(date +%s) - start < TMO )); do
  kill -0 "$PID" 2>/dev/null || { echo "app exited before ready; log: $LOG" >&2; exit 1; }
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 2 ${HDRS[@]+"${HDRS[@]}"} "$READY" || true)
  [[ "$code" == "200" ]] && { ok=1; break; }
  sleep 0.2
done
(( ok )) || { echo "not ready after ${TMO}s ($READY)" >&2; exit 1; }

LG_HDRS=()
for ((i=1; i<${#HDRS[@]}; i+=2)); do LG_HDRS+=(-H "${HDRS[$i]}"); done

if (( WARM > 0 )); then
  echo "warm-up: ${WARM}s at ${RATE} req/s"
  python3 "$LOADGEN" -r "$RATE" -d "$WARM" -c "$CONC" ${LG_HDRS[@]+"${LG_HDRS[@]}"} "${URLS[@]}" | sed 's/^/  /'
fi

cpu0=$(cpu_s); alloc0=$(metric jvm.gc.memory.allocated)
echo "measure: ${DUR}s at ${RATE} req/s"
RES=$(python3 "$LOADGEN" --json -r "$RATE" -d "$DUR" -c "$CONC" ${LG_HDRS[@]+"${LG_HDRS[@]}"} "${URLS[@]}")
cpu1=$(cpu_s); alloc1=$(metric jvm.gc.memory.allocated); rss=$(rss_mb); heap=$(metric "jvm.memory.used?tag=area:heap")

python3 - "$RES" "$cpu0" "$cpu1" "$alloc0" "$alloc1" "$rss" "$heap" "$LABEL" "$CSV" "${URLS[*]}" "${JAVA_ARGS[*]-attached pid $PID}" <<'PY'
import json, sys, os, datetime
res = json.loads(sys.argv[1]); c0, c1 = float(sys.argv[2]), float(sys.argv[3])
a0, a1, rss, heap, label, csv, urls, args = sys.argv[4:12]
n = max(res["requests"], 1)
cpu_ms = (c1 - c0) * 1000.0 / n
try:
    alloc_kb = (float(a1) - float(a0)) / 1024.0 / n
    alloc_s = f"{alloc_kb:.1f}"
except ValueError:
    alloc_s = "NA"
heap_s = f"{float(heap)/1048576:.0f}" if heap != "NA" else "NA"
cores = (c1 - c0) / max(res["requests"] / max(res["achieved_rps"], 0.001), 0.001)
print(f"== {label}: {res['achieved_rps']} req/s (target {res['target_rps']}) errors={res['errors']} dropped={res['dropped']}")
print(f"   latency ms p50 {res['p50_ms']} p99 {res['p99_ms']} p99.9 {res['p999_ms']} max {res['max_ms']}")
print(f"   cpu {cpu_ms:.2f} ms/req (~{cores:.2f} cores busy) | alloc {alloc_s} KB/req | rss {rss} MB | heap used {heap_s} MB")
if res["dropped"] or res["errors"]:
    print("   warning: dropped or errors > 0; this run is not a valid comparison", file=sys.stderr)
if csv:
    new = not os.path.exists(csv) or os.path.getsize(csv) == 0
    with open(csv, "a") as f:
        if new:
            f.write("timestamp,label,target_rps,achieved_rps,errors,dropped,p50_ms,p99_ms,p999_ms,max_ms,cpu_ms_per_req,alloc_kb_per_req,rss_mb,heap_used_mb,urls,args\n")
        f.write(f"{datetime.datetime.now().isoformat(timespec='seconds')},{label},{res['target_rps']},{res['achieved_rps']},{res['errors']},{res['dropped']},"
                f"{res['p50_ms']},{res['p99_ms']},{res['p999_ms']},{res['max_ms']},{cpu_ms:.3f},{alloc_s},{rss},{heap_s},\"{urls}\",\"{args}\"\n")
PY
rm -f "$LOG"
