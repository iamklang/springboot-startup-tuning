#!/usr/bin/env bash
# Measure Spring Boot startup under several CPU limits and find the knee point.
#
# Docker mode (real CFS quota, also reports CPU-seconds consumed by the start):
#   cpu_sweep.sh --image myapp:latest [--cpus "0.5 1 2 4"] [-n 3] [--mem 1g] [--env-file .env] [--port 8080] [-- extra docker run args]
#
# JVM-only mode (no Docker; approximates by capping the processor count the JVM sees, no throttling):
#   cpu_sweep.sh --jar target/app.jar --jvm-only [--cpus "1 2 4"] [-n 3] [-- extra java args]
#
# Options:
#   -n N            runs per CPU value (default 3); median is used
#   -t SECONDS      timeout per run (default 240)
#   --knee PCT      improvement threshold that defines the knee (default 15)
#   -o results.csv  append rows: mode,cpus,run,spring_s,process_s,cpu_seconds
#
# Startup at limit L can never be faster than cpu_seconds / L; the table prints that floor.
set -u
IMAGE=""; JAR=""; JVM_ONLY=0; CPUS=""; N=3; MEM="1g"; ENVFILE=""; TIMEOUT=240; KNEE=15; CSV=""; PORT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --image) IMAGE=$2; shift 2;;
    --jar) JAR=$2; shift 2;;
    --jvm-only) JVM_ONLY=1; shift;;
    --cpus) CPUS=$2; shift 2;;
    -n) N=$2; shift 2;;
    -t) TIMEOUT=$2; shift 2;;
    --mem) MEM=$2; shift 2;;
    --env-file) ENVFILE=$2; shift 2;;
    --port) PORT=$2; shift 2;;
    --knee) KNEE=$2; shift 2;;
    -o) CSV=$2; shift 2;;
    --) shift; break;;
    *) echo "unknown option $1" >&2; exit 2;;
  esac
done
EXTRA=("$@")
STARTED_RX='Started .* in [0-9.]+ seconds \((process|JVM) running for [0-9.]+\)'

if [ -n "$IMAGE" ] && [ "$JVM_ONLY" -eq 0 ]; then
  MODE=docker
  docker info >/dev/null 2>&1 || { echo "docker daemon not reachable; use --jar ... --jvm-only instead" >&2; exit 1; }
  [ -z "$CPUS" ] && CPUS="0.5 1 2 4"
  # An image built for another architecture runs under emulation and every number is meaningless
  img_arch=$(docker image inspect "$IMAGE" --format '{{.Architecture}}' 2>/dev/null)
  host_arch=$(docker info --format '{{.Architecture}}' 2>/dev/null)
  case "$host_arch" in aarch64) host_arch=arm64;; x86_64) host_arch=amd64;; esac
  if [ -z "$img_arch" ]; then echo "image $IMAGE not found locally" >&2; exit 1; fi
  if [ "$img_arch" != "$host_arch" ]; then
    echo "WARNING: image is $img_arch but the Docker host is $host_arch; it will run under emulation." >&2
    echo "         Rebuild for $host_arch for the sweep (docker build --platform linux/$host_arch ...) or sweep on the cluster." >&2
    [ "${ALLOW_EMULATION:-0}" = "1" ] || { echo "         Set ALLOW_EMULATION=1 to run anyway." >&2; exit 1; }
  fi
  NCPU_VM=$(docker info --format '{{.NCPU}}' 2>/dev/null)
  for c in $CPUS; do
    awk -v c="$c" -v n="$NCPU_VM" 'BEGIN{exit !(c>n)}' && echo "WARNING: --cpus $c exceeds the $NCPU_VM CPUs the Docker VM has; that row will be capped." >&2
  done
elif [ -n "$JAR" ]; then
  MODE=jvm
  [ -z "$CPUS" ] && CPUS="1 2 4"
  JVM_ONLY=1
else
  echo "need --image IMG (docker mode) or --jar JAR --jvm-only" >&2; exit 2
fi

LOGDIR="$(mktemp -d "${TMPDIR:-/tmp}/cpu-sweep.XXXXXX")"
parse_line() { # $1 line -> "spring process"
  echo "$(echo "$1" | sed -E 's/.* in ([0-9.]+) seconds.*/\1/') $(echo "$1" | sed -E 's/.*running for ([0-9.]+)\).*/\1/')"
}

run_docker() { # $1 cpus $2 idx -> "spring process cpu_seconds" | FAIL...
  local c=$1 idx=$2 start line cid
  local name="cpusweep-$$-$idx" log="$LOGDIR/docker-$c-$idx.log"
  local args=(run -d --name "$name" --cpus="$c" --memory="$MEM")
  [ -n "$ENVFILE" ] && args+=(--env-file "$ENVFILE")
  [ -n "$PORT" ] && args+=(-p "$PORT")
  args+=(${EXTRA[@]+"${EXTRA[@]}"} "$IMAGE")
  cid=$(docker "${args[@]}" 2>"$log") || { echo "FAIL docker run: $(cat "$log")"; return; }
  start=$(date +%s)
  while :; do
    line=$(docker logs "$cid" 2>&1 | grep -m1 -E "$STARTED_RX")
    [ -n "$line" ] && break
    if [ "$(docker inspect -f '{{.State.Running}}' "$cid" 2>/dev/null)" != "true" ]; then
      docker logs "$cid" >"$log" 2>&1; docker rm -f "$cid" >/dev/null 2>&1
      echo "FAIL container exited before Started (see $log)"; return
    fi
    if [ $(( $(date +%s) - start )) -ge "$TIMEOUT" ]; then
      docker logs "$cid" >"$log" 2>&1; docker rm -f "$cid" >/dev/null 2>&1
      echo "FAIL timeout ${TIMEOUT}s (see $log)"; return
    fi
    sleep 0.3
  done
  # CPU consumed so far: cgroup v2 cpu.stat usage_usec, fall back to v1 cpuacct.usage (ns)
  local usage
  usage=$(docker exec "$cid" sh -c 'if [ -f /sys/fs/cgroup/cpu.stat ]; then awk "/usage_usec/{printf \"%.3f\", \$2/1000000}" /sys/fs/cgroup/cpu.stat; elif [ -f /sys/fs/cgroup/cpu,cpuacct/cpuacct.usage ]; then awk "{printf \"%.3f\", \$1/1000000000}" /sys/fs/cgroup/cpu,cpuacct/cpuacct.usage; elif [ -f /sys/fs/cgroup/cpuacct/cpuacct.usage ]; then awk "{printf \"%.3f\", \$1/1000000000}" /sys/fs/cgroup/cpuacct/cpuacct.usage; else echo NA; fi' 2>/dev/null)
  [ -z "$usage" ] && usage=NA
  docker rm -f "$cid" >/dev/null 2>&1
  echo "$(parse_line "$line") $usage"
}

run_jvm() { # $1 cpus $2 idx -> "spring process NA" | FAIL...
  local c=$1 idx=$2 start line pid
  local log="$LOGDIR/jvm-$c-$idx.log"
  "${JAVA_BIN:-java}" -XX:ActiveProcessorCount="$c" -jar "$JAR" ${EXTRA[@]+"${EXTRA[@]}"} >"$log" 2>&1 &
  pid=$!; start=$(date +%s)
  while :; do
    line=$(grep -m1 -E "$STARTED_RX" "$log" 2>/dev/null); [ -n "$line" ] && break
    kill -0 "$pid" 2>/dev/null || { echo "FAIL process exited before Started (see $log)"; return; }
    [ $(( $(date +%s) - start )) -ge "$TIMEOUT" ] && { kill -9 "$pid"; wait "$pid" 2>/dev/null; echo "FAIL timeout (see $log)"; return; }
    sleep 0.2
  done
  kill "$pid" 2>/dev/null; for _ in 1 2 3 4 5 6 7 8 9 10; do kill -0 "$pid" 2>/dev/null || break; sleep 0.5; done
  kill -9 "$pid" 2>/dev/null; wait "$pid" 2>/dev/null; rm -f "$log"
  echo "$(parse_line "$line") NA"
}

median() { printf '%s\n' "$@" | sort -n | awk '{a[NR]=$1} END{ if(NR%2) print a[(NR+1)/2]; else printf "%.3f\n",(a[NR/2]+a[NR/2+1])/2 }'; }

echo "mode=$MODE  cpus=[$CPUS]  n=$N  $( [ $MODE = docker ] && echo "image=$IMAGE mem=$MEM" || echo "jar=$JAR (ActiveProcessorCount only, no throttling)")"
[ -n "$CSV" ] && { mkdir -p "$(dirname "$CSV")"; [ -s "$CSV" ] || echo "mode,cpus,run,spring_s,process_s,cpu_seconds" >"$CSV"; }
declare -a RES_CPU RES_MED RES_CPUSEC
prev=""
for c in $CPUS; do
  procs=(); cpusecs=()
  for ((i=1; i<=N; i++)); do
    if [ $MODE = docker ]; then r=$(run_docker "$c" "$i"); else r=$(run_jvm "$c" "$i"); fi
    case "$r" in FAIL*) echo "cpus=$c run=$i: $r"; tail -20 "$LOGDIR"/*-"$c"-"$i".log 2>/dev/null; exit 1;; esac
    read -r sp pr cs <<<"$r"
    echo "cpus=$c run=$i: spring=${sp}s process=${pr}s cpu_seconds=${cs}"
    procs+=("$pr"); [ "$cs" != "NA" ] && cpusecs+=("$cs")
    [ -n "$CSV" ] && echo "$MODE,$c,$i,$sp,$pr,$cs" >>"$CSV"
  done
  RES_CPU+=("$c"); RES_MED+=("$(median "${procs[@]}")")
  if [ ${#cpusecs[@]} -gt 0 ]; then RES_CPUSEC+=("$(median "${cpusecs[@]}")"); else RES_CPUSEC+=("NA"); fi
  echo "cpus=$c: process_median=${RES_MED[${#RES_MED[@]}-1]}s"
done

echo "----"
printf '%-8s %-14s %-12s %-14s %-10s\n' cpus process_median cpu_seconds floor_cpus/limit gain_vs_prev
knee=""
for idx in "${!RES_CPU[@]}"; do
  c=${RES_CPU[$idx]}; m=${RES_MED[$idx]}; cs=${RES_CPUSEC[$idx]}
  floor=NA; [ "$cs" != "NA" ] && floor=$(awk -v a="$cs" -v b="$c" 'BEGIN{printf "%.2f", a/b}')
  gain="-"
  if [ -n "$prev" ]; then
    gain=$(awk -v p="$prev" -v m="$m" 'BEGIN{printf "%.0f%%", (p-m)/p*100}')
    g=${gain%\%}
    if [ -z "$knee" ] && [ "${g%.*}" -lt "$KNEE" ] 2>/dev/null; then knee=${RES_CPU[$((idx-1))]}; fi
  fi
  printf '%-8s %-14s %-12s %-14s %-10s\n' "$c" "$m" "$cs" "$floor" "$gain"
  prev=$m
done
if [ -n "$knee" ]; then
  echo "knee: $knee CPU(s). Adding CPU beyond this improved startup by less than ${KNEE}%."
else
  echo "knee: not reached inside [$CPUS]; startup kept improving by >=${KNEE}% per step. Extend --cpus upward."
fi
[ $MODE = jvm ] && echo "note: --jvm-only caps JVM parallelism only; a real CFS quota (docker --cpus / k8s limits) will be slower at low values."
rmdir "$LOGDIR" 2>/dev/null || echo "logs kept in $LOGDIR"
