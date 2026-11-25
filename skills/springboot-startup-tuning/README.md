# springboot-startup-tuning

A Claude Code skill that reviews, measures and tunes the startup time of a Spring Boot project.

It scans the project, measures cold starts over several runs, sweeps CPU limits to find how much
CPU the app needs to start, applies tuning changes one tier at a time (config, JVM flags, Spring
AOT, CDS / JDK AOT cache), keeps only changes that beat the run-to-run noise, and writes a
before/after report with Kubernetes requests, limits and probe settings.

It also handles slow first requests after startup: it measures latency of the first N requests
from readiness, adds a readiness-gated warm-up runner, and recommends CPU headroom, probes and
load-balancer slow start for the warm-up period.

For services already running at scale it measures cost per request at a fixed arrival rate
(open-model load, CPU ms and KB allocated per request, p99) and tunes JVM heap, processors and GC,
database and HTTP client pools, hot-path allocation, and Kubernetes requests, limits, pod shape
and autoscaling.

## Install

Copy or clone this folder to `~/.claude/skills/springboot-startup-tuning/`.

## Contents

- `SKILL.md`: the workflow
- `scripts/`: `project_scan.sh`, `measure_startup.sh`, `cpu_sweep.sh`, `startup_report.py`, `autoconfig_report.py`, `measure_first_requests.sh`, `measure_efficiency.sh`, `loadgen.py`, `config_stub.py`
- `references/`: review checklist, CDS / AOT / Leyden guide, Kubernetes CPU and probe guide, first-request warm-up guide, steady-state runtime tuning guide, report template
- `assets/WarmupRunner.java`: readiness-gated warm-up runner template; `assets/WarmupObservationFilter.java` keeps its calls out of metrics and traces
- `evals/`: five test cases in the `claude plugin eval` layout

## Requirements

Java and Maven or Gradle for the project. Docker for a real CPU-quota sweep; without it the sweep
falls back to `-XX:ActiveProcessorCount`, which is an approximation. Python 3 for the report scripts.
