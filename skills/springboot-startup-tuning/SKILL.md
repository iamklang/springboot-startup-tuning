---
name: springboot-startup-tuning
description: "Review, measure and tune a Spring Boot service: startup time, first-request warm-up, and steady-state CPU, memory and latency per request at scale. Scans config and dependencies, measures cold start over several runs, sweeps CPU limits to find the CPU it needs, applies safe changes (auto-config exclusions, JPA/Hikari, JVM flags, Spring AOT, CDS / JDK AOT cache, warm-up runner, GC and heap sizing, pool limits) one at a time, measures at a fixed request rate and keeps only what helps, then reports with Kubernetes requests/limits, probes, pod shape and autoscaling advice. Use whenever the user mentions slow Spring Boot startup, cold start, failing probes, CPU/memory requests or limits for a Java service, CDS, AOT, CRaC, warm-up, slow first requests, p99 latency, throughput, GC tuning, OOMKilled, cost per request or a large Spring Boot fleet, even if they only say 'app ขึ้นช้า', 'request แรกช้า' or 'ทำให้เร็วขึ้น ใช้ resource น้อยลง'."
---

# Spring Boot startup tuning

You are tuning one specific Spring Boot project. The goal is not a generic list of tips: it is a
measured before/after result for *this* app, with only the changes that actually helped, plus a
clear recommendation for how much CPU it needs to start in production.

Startup time is noisy. Single measurements lie by 20 to 30 percent. Every conclusion in this
workflow must come from the median of several runs, and a change is only kept when it beats the
noise. Treat it like a benchmark, not a demo.

## Scripts (run them, don't rewrite them)

All scripts live in `scripts/` next to this file. Call them with absolute paths.

| Script | What it does |
|---|---|
| `project_scan.sh <project-dir>` | Detects build tool, Boot/Java version, starters, config files, Dockerfile, k8s manifests, JVM flags, existing tuning knobs. Run first. |
| `measure_startup.sh -n 5 -l LABEL -o results.csv [-u HEALTH_URL] -- <java args>` | Starts the app N times, parses the `Started ... in X seconds (process running for Y)` line, optionally polls a health URL until HTTP 200, prints median/min/max, appends a CSV row. Kills the app after each run. Ends with `RESULT: OK` or `RESULT: FAIL` (failures also go to stderr and to a FAIL row in the CSV); rejects glued arguments and runs where Spring time exceeds process time; warns on high load. |
| `config_stub.py <project-dir> -o stub.properties` | Finds `${...}` placeholders with no default and no value in config, guesses each type from the bound field, and writes harmless stub values (closed-port URLs, non-secret strings) so an app that depends on a Config Server, Vault or env vars can start locally. |
| `cpu_sweep.sh --image IMG [--env-file F] [--mem 1g] [-o sweep.csv]` or `--jar app.jar --jvm-only` | Measures startup under several CPU limits and reports the knee point where more CPU stops helping. Docker mode also reports CPU-seconds consumed by the start, and refuses an image whose architecture does not match the host. |
| `startup_report.py <actuator-startup.json or URL>` | Ranks the slowest startup steps and bean instantiations from the `/actuator/startup` endpoint. |
| `autoconfig_report.py <log with debug=true>` | Lists positive auto-configurations from the CONDITIONS EVALUATION REPORT and flags ones that are often unnecessary. |
| `measure_first_requests.sh -R 3 -n 100 -r READY_URL -u URL [-u URL] [-o CSV] [-d CURVE_CSV] -- <java args>` | Restarts the app R times, waits for readiness, sends N sequential requests over the given URLs, and reports request 1, the mean of requests 2 to 10, steady state and the first/steady ratio. Without java args it measures an app that is already running. |
| `measure_efficiency.sh -r RATE [-w 30] [-d 60] -R READY_URL -u URL [-u URL] [-o CSV] (-p PID \| -- <java args>)` | Runs a fixed-rate warm-up and measured phase (open model) and reports p50/p99/p99.9, CPU ms per request, KB allocated per request, RSS and heap used. |
| `loadgen.py -r RATE -d SECONDS URL...` | The open-model load generator used by `measure_efficiency.sh`; latency is measured from each request's scheduled time, so stalls are not hidden. |

`assets/WarmupRunner.java` is a readiness-gated warm-up runner template for Step 5.

Read `references/review-checklist.md` during the review step, `references/cds-aot.md` when you
reach tier 3, and `references/kubernetes.md` when writing the CPU recommendation, `references/warmup.md` in Step 5, and `references/runtime-tuning.md` in Step 6. Use
`references/report-template.md` for the final report.

## Workflow

### Step 0: Scope and preconditions

- Identify the project directory. If the current directory has no `pom.xml` or `build.gradle(.kts)`, search one level down, then ask the user which project.
- Run `project_scan.sh`. Note Boot version, Java version, build tool, whether Actuator is present, and whether a Dockerfile or image exists. Several later choices depend on these (CDS needs Boot 3.3+, JDK AOT cache needs JDK 24+, lazy init needs Boot 2.2+).
- Check `java -version` and the scan's `docker:` line, which shows the daemon state at scan time and the real error when it is unreachable. If Docker is not running, say so once and use `--jvm-only` for the CPU sweep; do not try to start the daemon. If a Docker step fails later, re-check with `docker version` before blaming the image: the daemon can go down mid-session.
- Check the machine: the scan's `machine:` line or `uptime`. If the 1-minute load is above about half the CPU count, stop other work or wait before measuring, and note the load in the report. A busy machine makes every number slow and noisy and can produce impossible readings.
- Run measurement batches through bash. On macOS the default shell is zsh, which does not word-split an unquoted `$VAR`, so `$JAVA_ARGS` in a loop reaches java as one argument and every run fails. Write the batch as a bash script with arrays (`ARGS=(-Xshare:auto -jar app.jar); measure_startup.sh ... -- "${ARGS[@]}"`) or wrap it in `bash -c '...'`. After each batch, check that every configuration printed `RESULT: OK` and added a CSV row before reading the numbers.
- Ask how the app is normally started if it is not obvious (profile, env vars, external DB). An app that cannot reach its database will fail or hang at startup, and measuring that is useless. Prefer a local profile, Testcontainers, or an H2/embedded fallback the project already has. Never invent credentials.
- If the scan reports placeholders without default, or a config client (Config Server, Vault, Consul), the app will not start without its remote config. Run `config_stub.py <project> -o <workspace>/stub.properties`, review the `CHECK` lines (enums and custom types need a real value), and start every measured run with `--spring.config.additional-location=file:<workspace>/stub.properties --spring.cloud.config.enabled=false`. Fix any remaining startup error one property at a time; the error names it. Say in the report that the app was measured with stub config.
- If Step 5 or 6 may run (the user asks about first requests, latency, throughput, cost, or the whole job), ask now for 2 to 5 hot-path URLs that are safe to call repeatedly (idempotent, no side effects) and any auth header they need. Without them Steps 5 and 6 cannot run; asking at the end wastes the session.
- Never commit. Make every change as an ordinary file edit the user can diff and revert.

### Step 1: Static review

Read the main class, every `application*.yml|properties`, the dependency list, the Dockerfile and
any k8s manifests. Walk through `references/review-checklist.md` and record findings as a list of
`(area, finding, expected impact, risk)`.

The point of this step is to form hypotheses before measuring, so that each later change tests one
hypothesis. Do not change anything yet.

Typical high-value findings:
- Starters pulled in but not used (actuator metrics exporters, JMX, Spring Cloud bits, dev tools in prod).
- `spring.jpa.hibernate.ddl-auto=validate|update` on a large schema, `open-in-view` left on, `allow_jdbc_metadata_access` not set.
- Heavy work in constructors, `@PostConstruct`, `CommandLineRunner`, or `@EventListener(ContextRefreshedEvent)` (remote calls, cache warming, migrations).
- Broad `scanBasePackages` or component scan over a huge package tree.
- Dockerfile running the fat jar directly with no CDS/AOT, no `-Xshare`, default GC on a 1-core limit.
- k8s CPU limit below 1 core with no startupProbe, or a liveness probe that can fire before startup finishes.

### Step 2: Baseline measurement

1. Build once: `mvn -q -DskipTests package` or `./gradlew bootJar -x test`. On Boot 4.1+ `-DskipTests` no longer skips test AOT processing (`process-test-aot`); when the build configures it, use `-Dmaven.test.skip=true`. Reuse the jar for every measurement in this step. Rebuilding between runs changes file-cache state and skews results.
2. Run `measure_startup.sh -n 5 -w 1 -l baseline -o <workspace>/results.csv -- -jar <jar> <app args>`. The `-w 1` warm-up run is discarded so disk cache does not favour later configurations. For web apps add `--server.port=0` to the app args so a port left open by a previous run never fails a measurement. Use the same `-n` for every later configuration.
   When the app has Actuator, also take one measurement with `-u http://localhost:<port>/actuator/health/readiness` and a fixed port. The `ready` column is wall time until the endpoint answers 200, which is what a readiness probe actually waits for; it includes the gap after the `Started` line (runners, `ApplicationReadyEvent` listeners) that neither log number shows.
3. Record **both** numbers from the log line. Spring's time is part of process time, so a run where `in X` is larger than `running for Y` is a bad reading (log from another start, clock trouble or extreme load); the script marks it FAIL. Do not keep a baseline taken while the scan or `uptime` showed high load. `in X seconds` is Spring's own time. `process running for Y` includes JVM start and class loading, and is the closest log number to what a readiness probe experiences (the `ready` column above is the exact one). If Y minus X is large (more than about 30 percent of Y), the problem is JVM/class loading and tiers 2 and 3 will matter more than bean tuning.
4. If Actuator is present, get a startup timeline: expose `startup` (`--management.endpoints.web.exposure.include=startup`) and, if the main class does not already set `BufferingApplicationStartup`, add it temporarily so the endpoint has data. Fetch `/actuator/startup` and run `startup_report.py` on it. Without Actuator, run once with `--debug` and feed the log to `autoconfig_report.py` instead.
   The scripts need the `Started ... in X seconds` line. If it is missing, check for `spring.main.log-startup-info=false` or a root log level above INFO, and override them on the command line for the measurement (`--spring.main.log-startup-info=true`). JSON structured logging is fine: the line is still matched inside the JSON message.
5. Keep a workspace dir (for example `<project>/.startup-tuning/`) for CSVs, logs and reports, and tell the user it can be deleted. Add it to `.gitignore` only if the user agrees.

### Step 3: CPU sweep

Run the sweep before tuning, because the right CPU budget is a finding on its own and because it
tells you whether the app is CPU-bound at start (most are).

- With Docker and an image or Dockerfile: `cpu_sweep.sh --image <img> --cpus "0.5 1 2 4" -n 3`. The script also prints CPU-seconds used per start. Startup time at a limit L cannot be lower than CPU-seconds divided by L, so this number is the theoretical floor and the strongest argument for the recommendation.
- Without Docker: `cpu_sweep.sh --jar <jar> --jvm-only --cpus "1 2 4"`. This sets `-XX:ActiveProcessorCount` and shows how much JVM parallelism (JIT and GC threads) helps, but it does not throttle. Say clearly in the report that it is an approximation.
- On a laptop with no quota the `--jvm-only` sweep often shows almost no difference between 1, 2 and 4; that means the app is not limited by JVM parallelism there, not that 1 core is enough in the cluster. Only Docker mode answers the cluster question.
- The knee is the first point where adding CPU improves startup by less than about 15 percent. That is the recommended startup CPU. Below 2 CPUs the JVM sizes itself for one processor (fewer compiler threads, SerialGC), so a knee at 2 is the common result.

### Step 4: Tune in tiers, one change at a time

Apply changes in this order. After each change rebuild only if code or dependencies changed,
re-measure with the same `-n`, and compare medians. Keep a change only when the median improves by
more than the noise (rule of thumb: improvement greater than 5 percent **and** greater than the
control's max-minus-min spread). Otherwise revert it and record "no measurable effect". The user
needs to see what did not work as much as what did.

Machines drift: other processes, thermal throttling and file cache change the numbers over the
session. Compare each candidate against a control measured in the same batch (re-run the current
best configuration right before or after the candidate), not against a baseline from an hour ago.
If the control itself has moved by more than its spread, say so and re-measure. (In one session the same control moved from 4.23 s to 4.82 s; a candidate compared against the old number would have looked 12 percent worse for no reason.)

After tier 3, re-check the tier 1 changes you kept by toggling each one off on top of the final
configuration. Spring AOT and CDS remove much of the work that tier 1 knobs were saving (bean
definition parsing, part of Hibernate bootstrap), so a change that was worth 13 percent on the plain
jar can be inside the noise afterwards. Report its value in the final configuration, and keep it only
if it still helps or has a reason beyond startup (for example `ddl-auto=none` in production).

**Tier 1, config and code, low risk**
- Remove unused starters and exclude auto-configurations reported by `autoconfig_report.py` that the app clearly does not use. Prefer removing the dependency over `spring.autoconfigure.exclude`; an exclusion hides the class but keeps the jar on the classpath.
- JPA: `spring.jpa.open-in-view=false`, `ddl-auto=none` outside dev, set `spring.jpa.database-platform` and `spring.jpa.properties.hibernate.boot.allow_jdbc_metadata_access=false`. With an embedded or in-memory DB the schema still has to come from somewhere, so do not switch `ddl-auto` to `none` there without a migration tool.
- Hikari: only the first connection blocks startup; the rest of the pool fills in the background. Its cost is one round trip to the database, which is a waiting cost, not a CPU cost. Look at it only when the timeline shows `dataSource` or `entityManagerFactory` as a long step.
- Move remote calls and cache warming out of constructors and `@PostConstruct` to an `@Async` listener on `ApplicationReadyEvent` **only if** readiness semantics allow it; ask when unsure.
- Narrow `scanBasePackages` if the scan covers unrelated packages.
- Disable JMX (`spring.jmx.enabled=false`, default off since Boot 2.2) and Hibernate statistics if on.

**Tier 2, JVM flags, applied in the Dockerfile or JAVA_TOOL_OPTIONS**
- `-XX:TieredStopAtLevel=1` only for dev or short-lived jobs; it hurts steady-state throughput, so never recommend it for a long-running production service without saying that.
- GC choice: the JVM already picks SerialGC below 2 CPUs or about 1.8 GB of memory, and G1 above. Forcing `-XX:+UseSerialGC` or `ParallelGC` rarely moves startup by more than the noise; measure it rather than assuming.
- Set `-Xms` equal to `-Xmx` or use `-XX:InitialRAMPercentage` matching `MaxRAMPercentage` to avoid heap resizing during start.
- `-XX:ActiveProcessorCount=2` when the limit is below 2 cores but the user cannot raise it; note it increases throttling.
- `-Dspring.main.lazy-initialization=true` is a tier 2 flag, not tier 1: it hides bean errors until first use and moves the cost to the first request. Measure it, report it, but recommend it for dev/test only unless the user explicitly wants it in prod.

**Tier 3, build-time: Spring AOT, CDS, JDK AOT cache**
Read `references/cds-aot.md` first. Measure these as separate steps, in this order, each against the
previous one: (a) the extracted jar layout alone, (b) plus Spring AOT, (c) plus CDS or the AOT cache.
Extraction is a prerequisite for CDS but is also a gain of its own (often 10 to 15 percent, from
dropping the nested-jar loader). Comparing "extracted + CDS" against the fat jar credits that gain to
CDS and overstates it. If a tier 3 step shows no gain where 10 to 30 percent is typical, verify
before reverting: confirm the archive maps (`-Xshare:on`, `-Xlog:cds`), confirm the training run
used the same jar, flags and JDK as the measured run, and re-measure in a fresh batch. Report a
verified null result as such; it is a real finding on that machine.

Run training runs and measurements one JVM at a time, with no other JVMs or containers running. A training run needs more memory than a normal start; exit code 137 means it was killed (usually out of memory), not that CDS does not work. Re-run it alone.

Before Spring AOT on an app with a Spring Cloud config client, grep `@RefreshScope` (the scan counts it): AOT does not support refresh scope. Pass properties to `process-aot` as JVM system properties (`-Dspring-boot.aot.jvmArguments="-D..."`), not as application arguments. Details in `references/cds-aot.md`.

Choose by versions found in Step 0:
- Boot 3.3+ on JDK 17 to 23: Spring AOT (`process-aot` / `processAot`, run with `-Dspring.aot.enabled=true`) plus CDS training run and `-XX:SharedArchiveFile`. Buildpacks: `BP_JVM_CDS_ENABLED=true`, `BP_SPRING_AOT_ENABLED=true`.
- Boot 3.0 to 3.2: Spring AOT plus CDS by hand: unpack the jar, pack `BOOT-INF/classes` into a jar (CDS refuses directories on the classpath) and keep the `BOOT-INF/classpath.idx` order. The recipe is in `references/cds-aot.md`.
- JDK 24+: replace CDS with the JDK AOT cache (`-XX:AOTCache`); on JDK 25 use the one-step `-XX:AOTCacheOutput`. Boot 4 is built for this.
- These need the jar extracted (`-Djarmode=tools extract`; Boot 4.1 removed the old `layertools` mode, so a Dockerfile still using it fails to build) and an identical JDK, architecture and classpath order between training and runtime. Put the training run in the Dockerfile so the archive is rebuilt with every image; a stale archive is silently ignored and the gain vanishes without an error.
- Spring AOT fixes the bean graph at build time: profiles and `@ConditionalOnProperty` are evaluated during `process-aot`. Pass the production profile to `process-aot` and to the training run. A mismatch either fails at startup with an error naming the bean, or silently runs the build-time bean set; neither is visible until it is tested with the real profile.

**Tier 4, recommend only, do not apply without explicit request**
- CRaC (Boot 3.2+, Linux only, CRaC-enabled JDK): tens of milliseconds restore, but open connections, secrets in the snapshot and operational complexity. Fit for scale-to-zero and serverless.
- GraalVM native image: sub-100 ms start, long builds, reflection hints needed, lower peak throughput. Fit for CLIs and functions, rarely for a large monolith.

### Step 5: First-request latency and warm-up

Do this step when the user asks about slow first requests, latency or errors right after deploy or
scale-out, or warm-up; when the review found global lazy init in production; or when the user asks
for the whole job. Otherwise mention it in one line of the report and skip it. Read
`references/warmup.md` first.

1. **W0 baseline:** `measure_first_requests.sh` against 2 to 5 real, idempotent hot-path URLs, gated on `/actuator/health/readiness`, with the configuration kept from Step 4. If first/steady is below about 3 and requests 2 to 10 are near steady, report that warm-up is not a problem here and stop the step.
2. **W1 CPU:** repeat W0 at the limits from Step 3. A big gain from 1 to 2 CPUs points to CPU headroom during warm-up (W5) as the main fix.
3. **W2 Spring settings:** `spring.mvc.servlet.load-on-startup=1` (Boot's default `-1` initialises the DispatcherServlet on the first request), lazy init off, and connection priming in warm-up rather than `minimum-idle` — raising `minimum-idle` does not fill the pool at startup. One at a time, with the keep rule from Step 4 applied to request 1 and the 2-to-10 mean.
4. **W3 warm-up runner:** copy `assets/WarmupRunner.java` and `assets/WarmupObservationFilter.java`, set their package and the `app.warmup.*` properties, pointing at the same hot paths (`paths` for GETs, `post` for side-effect-free POSTs such as quote or validate endpoints; a GET-only warm-up leaves a validated POST endpoint cold). Data-loading runners must carry a smaller `@Order` on their class. Start with 1 to 3 iterations: the costs it removes are one-time, and one round gave nearly the whole gain on the demo. The observation filter keeps warm-up calls out of `http.server.requests` and traces by matching a per-JVM random token, never a fixed header value. Re-measure both the first-N curve and the `ready` time from `measure_startup.sh -u`, because warm-up moves time from the first requests to before readiness. Report both numbers. Confirm with the user that every warm-up path is free of side effects. Check that the `warm-up done` log line shows 0 failed and 0 non-2xx; a 401 or 404 means the path warms only part of the stack. Installation, path choice and auth are in section 4 of `references/warmup.md`. Choose warm-up targets by tracing the controller method's call graph, never from the URL or the HTTP verb. When the service must not publish warm-up traffic in its own metrics and traces, or the team wants side-effect safety checked by the compiler instead of by reviewing URL strings, use the in-process variant in section 4b: it reaches the same per-type caches and gives up only the HTTP plumbing.
5. **W4 to W8:** write recommendations (startupProbe covering startup plus warm-up at the CPU request, rollout settings, CPU headroom or startup CPU boost, AOT cache trained on real requests, LB slow start, CRaC) from `references/warmup.md`. Apply the AOT cache training change only if tier 3 already uses an AOT cache and the training run can reach what the requests need.

The sequential curl curve shows the one-time costs. It does not show the JIT tail under load; say
so, and give the user the open-model load-test command (k6 `constant-arrival-rate`, vegeta or wrk2
at the real per-pod rate) to run in staging.

### Step 6: Steady-state runtime efficiency

Do this step when the user asks about throughput, p99 latency, GC, memory or OOMKilled, CPU or cost
per request, or tuning a large fleet; or when they ask for the whole job. Read
`references/runtime-tuning.md` first. The unit of every comparison is cost per request at a fixed
arrival rate, never maximum throughput from a closed-model tool.

1. **R0 baseline:** `measure_efficiency.sh` at a rate near the real per-pod rate, against the same hot-path URLs as Step 5, with `metrics` exposed. Repeat 3 times. Record p99, p99.9, CPU ms per request, KB allocated per request, RSS and heap used. If the user has production metrics, also write down the PromQL values from the reference for the same service.
2. **R1 JVM:** in this order, one change at a time against a same-batch control: heap and processor flags (`MaxRAMPercentage`/`InitialRAMPercentage`, `ActiveProcessorCount` when there is no CPU limit), GC choice for the pod size and latency target, and on JDK 25 `-XX:+UseCompactObjectHeaders`. Recommend a JDK upgrade when the project is on an older LTS; do not change the JDK in the build without asking.
3. **R2 Spring and pools:** check pods × Hikari `maximum-pool-size` against the database's `max_connections`, `open-in-view`, Hibernate batching and IDENTITY ids, explicit timeouts and pool sizes on every outbound HTTP client (Apache HC5 defaults to 5 per route), and remote calls inside `@Transactional`. Turn on virtual threads only on JDK 24+ and only after the pools are sized.
4. **R3 allocation:** if CPU or allocation per request is high, take a short allocation and wall-clock profile (JFR: `jcmd <pid> JFR.start duration=60s settings=profile filename=app.jfr` while `measure_efficiency.sh` runs) and look for the hot-path sources listed in the reference (logging, Jackson, boxing, regex, exceptions). Report findings with file and line; change code only when the user agrees.
5. **R4 Kubernetes:** write requests, limits, pod shape and autoscaling recommendations from the measured CPU ms per request, the target RPS per pod and the reference: CPU request from measured usage, no CPU limit for latency-sensitive services with `ActiveProcessorCount`, memory request equal to limit from heap plus measured non-heap, 2 to 4 vCPU pods, HPA on RPS or in-flight requests.

A laptop run shows relative differences between configurations, not production capacity. Give the
user the staging capacity-test procedure from the reference (k6 `ramping-arrival-rate`, one pod with
production resources, max RPS per pod at the SLO, then a soak test) for the absolute numbers.

### Step 7: Report and Kubernetes recommendation

Write the report with `references/report-template.md` into the workspace dir and summarise it in
chat. If writing the report file is refused, put the full report in your final message instead and
say that it was not saved; do not look for another way to write the file. The report must contain:
- A table of every configuration measured: label, median, min, max, n, kept or reverted.
- The CPU sweep table and the recommended startup CPU with the CPU-seconds floor argument.
- The exact diffs applied (file and property), and the list of findings not applied with reasons.
- k8s guidance from `references/kubernetes.md`: requests/limits strategy, `startupProbe` sized as measured startup at the chosen limit times 1.5, and the Prometheus throttling query to verify after deploy.
- If Step 5 ran: the first-N table (request 1, requests 2 to 10, steady, first/steady, ready time) for each configuration, and the warm-up recommendations W4 to W8.
- If Step 6 ran: the per-request table (rate, p99, p99.9, CPU ms/req, KB/req, RSS, heap) for each configuration, the pool and client findings, and the R4 recommendations.
- What was approximated (for example `--jvm-only` sweep, no DB available, measured on a laptop not the cluster node).

Numbers belong in tables, not in sentences. Report failures honestly: if the app would not start without infrastructure you did not have, say what was skipped.

## Judgment calls

- A gain that adds operational risk (CRaC, lazy init in prod) is not worth it for most teams; a gain from extraction, AOT or CDS that is invisible at runtime almost always is, once measured. Say which category each change is in.
- Laptop numbers do not transfer to cluster nodes, but ratios do. Report relative improvements as the primary result and absolute times as secondary.
- Before a local Docker sweep, check that the image architecture matches the Docker host (`cpu_sweep.sh` refuses otherwise). An amd64 image on Apple Silicon runs under emulation and every number is wrong by a large, unknown factor.
- If cluster startup is much slower than the CPU-seconds floor predicts, the extra time is waiting, not computing: DB connection across the VPC, Flyway, remote config, DNS, Kafka metadata, cloud credential lookups (IRSA / instance metadata timeouts). The `/actuator/startup` timeline shows these as long single steps with little CPU. More CPU will not fix them.
- If the baseline is already under about 3 seconds on 2 cores and the timeline has no single step above a few hundred milliseconds, tell the user early that tier 3 is the main lever left and ask whether it is worth the Dockerfile change.
- When the user only wants a review, stop after Step 1 and Step 2 (and W0 of Step 5 if they asked about first requests) and hand over the findings; do not start changing files.
- Use the scripts' own timeouts (`-t`). macOS has no `timeout` command, and a missing command silently skips whatever it wraps.
