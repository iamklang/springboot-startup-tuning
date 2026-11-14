# springboot-startup-tuning

A Claude Code skill that reviews, measures and tunes the startup time of a Spring Boot project.

It scans the project, measures cold starts over several runs, sweeps CPU limits to find how much
CPU the app needs to start, applies tuning changes one tier at a time (config, JVM flags, Spring
AOT, CDS / JDK AOT cache), keeps only changes that beat the run-to-run noise, and writes a
before/after report with Kubernetes requests, limits and probe settings.

## Install

Copy or clone this folder to `~/.claude/skills/springboot-startup-tuning/`.

## Contents

- `SKILL.md`: the workflow
- `scripts/`: `project_scan.sh`, `measure_startup.sh`, `cpu_sweep.sh`, `startup_report.py`, `autoconfig_report.py`
- `references/`: review checklist, CDS / AOT / Leyden guide, Kubernetes CPU and probe guide, report template
- `evals/`: three test cases in the `claude plugin eval` layout

## Requirements

Java and Maven or Gradle for the project. Docker for a real CPU-quota sweep; without it the sweep
falls back to `-XX:ActiveProcessorCount`, which is an approximation. Python 3 for the report scripts.
