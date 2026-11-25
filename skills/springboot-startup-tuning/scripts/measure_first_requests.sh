#!/usr/bin/env bash
# Measure latency of the first N requests after a Spring Boot app becomes ready.
#
# Usage:
#   measure_first_requests.sh [-R RUNS] [-n REQUESTS] [-r READY_URL] [-t TIMEOUT_S]
#                             [-l LABEL] [-o CSV] [-d CURVE_CSV] [-H 'Header: v']...
#                             -u URL [-u URL ...] [-- <java args>]
#
# With "-- <java args>" the script starts the app RUNS times, waits until READY_URL
# answers 200 (default: first -u URL), then sends REQUESTS sequential requests that
# rotate through the -u URLs and records each latency. Without java args it measures
# an app that is already running (RUNS is forced to 1).
#
# Output: per-run first/p50/p90/max, the median over runs of request #1, of the
# mean of requests 2-10, and of the last 10 requests, plus the ratio first/steady.
# -o appends one summary row; -d writes every request (run,index,url,status,ms).
set -euo pipefail

RUNS=3; REQ=100; READY=""; TMO=180; LABEL="run"; CSV=""; CURVE=""
URLS=(); HDRS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    -R) RUNS="$2"; shift 2;;
    -n) REQ="$2"; shift 2;;
    -r) READY="$2"; shift 2;;
    -t) TMO="$2"; shift 2;;
    -l) LABEL="$2"; shift 2;;
    -o) CSV="$2"; shift 2;;
    -d) CURVE="$2"; shift 2;;
    -u) URLS+=("$2"); shift 2;;
    -H) HDRS+=(-H "$2"); shift 2;;
    --) shift; break;;
    -h|--help) sed -n '2,17p' "$0"; exit 0;;
    *) echo "unknown option: $1" >&2; exit 2;;
  esac
done
JAVA_ARGS=("$@")
[[ ${#URLS[@]} -gt 0 ]] || { echo "need at least one -u URL" >&2; exit 2; }
[[ -n "$READY" ]] || READY="${URLS[0]}"
[[ ${#JAVA_ARGS[@]} -gt 0 ]] || RUNS=1
command -v curl >/dev/null || { echo "curl not found" >&2; exit 2; }

now_ms() { perl -MTime::HiRes=time -e 'printf "%d\n", time*1000'; }
LOGDIR=$(mktemp -d "${TMPDIR:-/tmp}/firstreq.XXXXXX")
APP_PID=""
cleanup() {
  if [[ -n "$APP_PID" ]]; then kill "$APP_PID" 2>/dev/null || true; wait "$APP_PID" 2>/dev/null || true; fi
}
trap cleanup EXIT

# median of numbers on stdin
median() { sort -n | awk '{a[NR]=$1} END{if(NR==0){print "NA";exit} if(NR%2){print a[(NR+1)/2]} else {printf "%.1f\n",(a[NR/2]+a[NR/2+1])/2}}'; }
pct() { sort -n | awk -v p="$1" '{a[NR]=$1} END{if(NR==0){print "NA";exit} i=int((p/100)*NR+0.999); if(i<1)i=1; print a[i]}'; }

FIRSTS=(); EARLY=(); STEADY=()
[[ -n "$CURVE" ]] && echo "label,run,index,url,status,ms" > "$CURVE"

for ((run=1; run<=RUNS; run++)); do
  if [[ ${#JAVA_ARGS[@]} -gt 0 ]]; then
    java "${JAVA_ARGS[@]}" > "$LOGDIR/app-$run.log" 2>&1 &
    APP_PID=$!
  fi
  start=$(now_ms); ok=0
  while (( $(now_ms) - start < TMO*1000 )); do
    if [[ -n "$APP_PID" ]] && ! kill -0 "$APP_PID" 2>/dev/null; then
      echo "run $run: app exited before ready; log: $LOGDIR/app-$run.log" >&2; exit 1
    fi
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 2 ${HDRS[@]+"${HDRS[@]}"} "$READY" || true)
    [[ "$code" == "200" ]] && { ok=1; break; }
    perl -e 'select(undef,undef,undef,0.1)'
  done
  (( ok )) || { echo "run $run: not ready after ${TMO}s ($READY)" >&2; exit 1; }
  ready_ms=$(( $(now_ms) - start ))

  : > "$LOGDIR/lat-$run.txt"
  for ((i=1; i<=REQ; i++)); do
    u="${URLS[$(( (i-1) % ${#URLS[@]} ))]}"
    out=$(curl -s -o /dev/null -w '%{http_code} %{time_total}' --max-time 60 ${HDRS[@]+"${HDRS[@]}"} "$u" || echo "000 60")
    status=${out% *}; ms=$(awk -v s="${out#* }" 'BEGIN{printf "%.1f", s*1000}')
    echo "$ms" >> "$LOGDIR/lat-$run.txt"
    [[ -n "$CURVE" ]] && echo "$LABEL,$run,$i,$u,$status,$ms" >> "$CURVE"
    [[ "$status" =~ ^[23] ]] || echo "run $run req $i: HTTP $status for $u" >&2
  done

  f=$(head -1 "$LOGDIR/lat-$run.txt")
  e=$(sed -n '2,10p' "$LOGDIR/lat-$run.txt" | awk '{s+=$1} END{if(NR) printf "%.1f", s/NR; else print "NA"}')
  s=$(tail -10 "$LOGDIR/lat-$run.txt" | median)
  p50=$(median < "$LOGDIR/lat-$run.txt"); p90=$(pct 90 < "$LOGDIR/lat-$run.txt"); mx=$(sort -n "$LOGDIR/lat-$run.txt" | tail -1)
  printf 'run %d: ready %d ms | first %s ms | req2-10 mean %s ms | last10 p50 %s ms | all p50 %s p90 %s max %s ms\n' \
    "$run" "$ready_ms" "$f" "$e" "$s" "$p50" "$p90" "$mx"
  FIRSTS+=("$f"); EARLY+=("$e"); STEADY+=("$s")
  cleanup; APP_PID=""
  sleep 1
done

mf=$(printf '%s\n' "${FIRSTS[@]}" | median)
me=$(printf '%s\n' "${EARLY[@]}" | median)
ms=$(printf '%s\n' "${STEADY[@]}" | median)
ratio=$(awk -v a="$mf" -v b="$ms" 'BEGIN{ if(b>0) printf "%.1f", a/b; else print "NA"}')
echo "== $LABEL (runs=$RUNS, requests=$REQ): first ${mf} ms | req2-10 ${me} ms | steady ${ms} ms | first/steady ${ratio}x"

if [[ -n "$CSV" ]]; then
  [[ -s "$CSV" ]] || echo "timestamp,label,runs,requests,first_median_ms,early_mean_median_ms,steady_median_ms,first_over_steady,urls,args" > "$CSV"
  printf '%s,%s,%d,%d,%s,%s,%s,%s,"%s","%s"\n' "$(date +%Y-%m-%dT%H:%M:%S)" "$LABEL" "$RUNS" "$REQ" \
    "$mf" "$me" "$ms" "$ratio" "${URLS[*]}" "${JAVA_ARGS[*]-}" >> "$CSV"
fi
rm -rf "$LOGDIR"
