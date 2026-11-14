# Startup tuning report: <project name>

Date: <YYYY-MM-DD>. Measured on: <machine, CPU, JDK build>. Boot <version>, Java <version>, <build tool>.
Workspace: `<path to .startup-tuning/>` (safe to delete).

## Summary

<Two or three sentences: baseline median, best configuration median, relative improvement, recommended startup CPU. No other numbers in prose.>

## Measurements

All times are seconds, median of n runs after 1 discarded warm-up run. "Spring" is `Started ... in X seconds`; "Process" is `process running for Y` and is what a readiness probe sees.

| # | Configuration | n | Spring median | Process median | Process min | Process max | vs baseline | Decision |
|---|---|---|---|---|---|---|---|---|
| 0 | baseline | 5 | | | | | 0% | – |
| 1 | <change> | 5 | | | | | | kept / reverted (no measurable effect) |

## CPU sweep

Mode: <docker --cpus | --jvm-only approximation>. Image/jar: <...>.

| CPU limit | Process median | CPU-seconds per start | Floor (cpu_s / limit) | Gain vs previous |
|---|---|---|---|---|

Knee: <N cores>. Recommended startup CPU: <N>. <One sentence on the floor argument.>

## Changes applied

For each: file, exact property or code change, measured effect, runtime behaviour change if any.

- `src/main/resources/application.yml`: `spring.jpa.open-in-view=false` ... effect: ... runtime change: ...

## Findings not applied

| Finding | Why not applied | Expected gain | Who decides |
|---|---|---|---|

## First requests after readiness (Step 5, if run)

| Label | Request 1 ms | Requests 2-10 mean ms | Steady ms | First/steady | Ready ms | Kept |
|---|---|---|---|---|---|---|
| baseline | | | | | | |
| +warm-up runner | | | | | | |

Warm-up recommendations: startupProbe covering startup plus warm-up at the CPU request, rollout
settings, CPU headroom, AOT cache trained on real requests, LB slow start. Say that the curl
curve does not show the JIT tail under load, and give the load-test command for staging.

## Steady-state cost per request (Step 6, if run)

Rate: <req/s>, warm-up <s>, measured <s>, runs per configuration: <n>.

| Label | p99 ms | p99.9 ms | CPU ms/req | KB alloc/req | RSS MB | Heap MB | Kept |
|---|---|---|---|---|---|---|---|
| baseline | | | | | | | |

Pool and client findings (DB pool × replicas vs `max_connections`, HTTP client pools and
timeouts, transactions holding connections), allocation hot spots from the profile, and the
staging capacity-test procedure for absolute numbers.

## Kubernetes recommendation

```yaml
resources:
  requests: { cpu: <...>, memory: <...> }
  limits:   { memory: <...> }      # or cpu: <knee>
startupProbe: { ... failureThreshold: <...>, periodSeconds: 5 }
```

Verify after deploy with the throttling query in `references/kubernetes.md`.

## Caveats and approximations

- <e.g. No database available locally; app measured with the `local` profile and H2.>
- <e.g. CPU sweep used -XX:ActiveProcessorCount, not a real quota.>
- <e.g. Laptop CPU is faster than node CPU; use ratios.>
