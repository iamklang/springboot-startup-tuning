#!/usr/bin/env bash
# Measure Spring Boot startup time over several runs.
#
# Usage:
#   measure_startup.sh [-n RUNS] [-w WARMUP] [-t TIMEOUT_S] [-l LABEL] [-o results.csv] [-k] -- <java args...>
#
#   -n  measured runs (default 5)
#   -w  warm-up runs that are executed but not counted (default 1)
#   -t  seconds to wait for the "Started" line before declaring a run failed (default 180)
#   -l  label written to the CSV row (default: "run")
#   -o  CSV file to append to (header written if new)
#   -k  keep per-run logs (default: delete logs of successful runs)
#   -u  health URL to poll after start, e.g. http://localhost:8080/actuator/health/readiness
#       Records wall time from launch until it returns HTTP 200 ("ready" column). This is
#       what a readiness probe sees. Needs a fixed port, so do not combine with --server.port=0.
#   JAVA_BIN env var overrides the java executable.
#
# Example:
#   measure_startup.sh -n 5 -l baseline -o .startup-tuning/results.csv -- -jar target/app.jar --spring.profiles.active=local
#   measure_startup.sh -n 5 -l cds -o .startup-tuning/results.csv -- -XX:SharedArchiveFile=app.jsa -jar target/extracted/app.jar
#
# Parses both Boot 3 ("process running for") and Boot 2 ("JVM running for") log lines.
set -u
RUNS=5; WARM=1; TIMEOUT=180; LABEL="run"; CSV=""; KEEP=0; HEALTH=""
while getopts "n:w:t:l:o:ku:" opt; do
  case $opt in
    n) RUNS=$OPTARG;; w) WARM=$OPTARG;; t) TIMEOUT=$OPTARG;; l) LABEL=$OPTARG;; o) CSV=$OPTARG;; k) KEEP=1;; u) HEALTH=$OPTARG;;
    *) echo "bad option" >&2; exit 2;;
  esac
done
shift $((OPTIND-1))
[ "${1:-}" = "--" ] && shift
[ $# -eq 0 ] && { echo "usage: $0 [-n N] [-w W] [-t T] [-l LABEL] [-o CSV] -- <java args>" >&2; exit 2; }
JAVA="${JAVA_BIN:-java}"
LOGDIR="$(mktemp -d "${TMPDIR:-/tmp}/startup-measure.XXXXXX")"

now_ms() { perl -MTime::HiRes=time -e 'printf "%d", time*1000'; }

one_run() {  # $1 = run index, rest = java args; prints "spring process" seconds or "FAIL reason"
  local idx=$1; shift
  local log="$LOGDIR/run-$idx.log" pid start line="" t0 ready="NA"
  start=$(date +%s); t0=$(now_ms)
  "$JAVA" "$@" >"$log" 2>&1 &
  pid=$!
  while :; do
    line=$(grep -m1 -E 'Started .* in [0-9.]+ seconds \((process|JVM) running for [0-9.]+\)' "$log" 2>/dev/null)
    [ -n "$line" ] && break
    if ! kill -0 "$pid" 2>/dev/null; then
      echo "FAIL process exited before Started line (see $log)"; return
    fi
    if [ $(( $(date +%s) - start )) -ge "$TIMEOUT" ]; then
      kill -9 "$pid" 2>/dev/null; wait "$pid" 2>/dev/null
      echo "FAIL timeout after ${TIMEOUT}s (see $log)"; return
    fi
    sleep 0.2
  done
  if [ -n "$HEALTH" ]; then
    while :; do
      [ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 2 "$HEALTH")" = "200" ] && { ready=$(awk -v a="$(now_ms)" -v b="$t0" 'BEGIN{printf "%.3f",(a-b)/1000}'); break; }
      kill -0 "$pid" 2>/dev/null || break
      [ $(( $(date +%s) - start )) -ge "$TIMEOUT" ] && break
      sleep 0.05
    done
  fi
  # SIGTERM first so the app shuts down cleanly, escalate after 5s
  kill "$pid" 2>/dev/null
  for _ in 1 2 3 4 5 6 7 8 9 10; do kill -0 "$pid" 2>/dev/null || break; sleep 0.5; done
  kill -9 "$pid" 2>/dev/null; wait "$pid" 2>/dev/null
  local spring process
  spring=$(echo "$line" | sed -E 's/.* in ([0-9.]+) seconds.*/\1/')
  process=$(echo "$line" | sed -E 's/.*running for ([0-9.]+)\).*/\1/')
  echo "$spring $process $ready"
  [ "$KEEP" -eq 1 ] || rm -f "$log"
}

printf 'label=%s  java=%s\nargs: %s\n' "$LABEL" "$("$JAVA" -version 2>&1 | head -1)" "$*"
for ((i=1; i<=WARM; i++)); do
  r=$(one_run "w$i" "$@")
  case "$r" in FAIL*) echo "warm-up $i: $r"; tail -30 "$LOGDIR"/run-w$i.log 2>/dev/null; exit 1;; esac
  read -r ws wp wr <<<"$r"
  echo "warm-up $i: spring=${ws}s process=${wp}s ready=${wr} (discarded)"
done

SPRING=(); PROC=(); READY=()
for ((i=1; i<=RUNS; i++)); do
  r=$(one_run "$i" "$@")
  case "$r" in FAIL*) echo "run $i: $r"; tail -30 "$LOGDIR"/run-$i.log 2>/dev/null; exit 1;; esac
  read -r rs rp rr <<<"$r"
  SPRING+=("$rs"); PROC+=("$rp"); [ "$rr" != "NA" ] && READY+=("$rr")
  echo "run $i: spring=${rs}s process=${rp}s ready=${rr}"
done

stats() {  # median min max mean of args
  printf '%s\n' "$@" | sort -n | awk '{a[NR]=$1; s+=$1} END {
    m = (NR%2) ? a[(NR+1)/2] : (a[NR/2]+a[NR/2+1])/2;
    printf "%.3f %.3f %.3f %.3f", m, a[1], a[NR], s/NR }'
}
read -r sm smin smax smean <<<"$(stats "${SPRING[@]}")"
read -r pm pmin pmax pmean <<<"$(stats "${PROC[@]}")"
echo "----"
printf '%-8s %8s %8s %8s %8s\n' "" median min max mean
printf '%-8s %8s %8s %8s %8s\n' spring "$sm" "$smin" "$smax" "$smean"
printf '%-8s %8s %8s %8s %8s\n' process "$pm" "$pmin" "$pmax" "$pmean"
rm_="NA"; rmin="NA"; rmax="NA"
if [ ${#READY[@]} -gt 0 ]; then read -r rm_ rmin rmax _ <<<"$(stats "${READY[@]}")"; printf '%-8s %8s %8s %8s\n' ready "$rm_" "$rmin" "$rmax"; fi
echo "spread(process)=$(awk -v a="$pmax" -v b="$pmin" 'BEGIN{printf "%.3f", a-b}')s  n=$RUNS"
if [ -n "$CSV" ]; then
  mkdir -p "$(dirname "$CSV")"
  [ -s "$CSV" ] || echo "timestamp,label,n,spring_median,spring_min,spring_max,process_median,process_min,process_max,ready_median,ready_min,ready_max,args" >"$CSV"
  printf '%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"%s"\n' "$(date -u +%FT%TZ)" "$LABEL" "$RUNS" "$sm" "$smin" "$smax" "$pm" "$pmin" "$pmax" "$rm_" "$rmin" "$rmax" "$(echo "$*" | tr '"' "'")" >>"$CSV"
  echo "appended to $CSV"
fi
[ "$KEEP" -eq 1 ] && echo "logs: $LOGDIR" || rmdir "$LOGDIR" 2>/dev/null
