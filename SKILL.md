---
name: springboot-startup-tuning
description: "Review, measure and tune the startup time of a Spring Boot project: scan the project's config and dependencies, build it, measure cold-start time over several runs, sweep CPU limits (Docker --cpus or JVM ActiveProcessorCount) to find how much CPU the app needs to start, then apply safe tuning changes (auto-config exclusions, JPA/Hikari knobs, lazy init, JVM flags, Spring AOT, CDS / JDK AOT cache) one at a time, re-measure, keep only what helps, and write a before/after report with Kubernetes requests/limits/probe recommendations. Use this whenever the user talks about a Spring Boot app starting slowly, startup time, boot time, cold start, 'Started X in N seconds', readiness/startup probes failing, CPU requests/limits for a Java service, CDS, Spring AOT, Project Leyden, CRaC, or asks how fast / how much CPU their Spring service needs — even if they only say 'ทำให้ start เร็วขึ้น' or 'app ขึ้นช้า'."
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
| `measure_startup.sh -n 5 -l LABEL -o results.csv [-u HEALTH_URL] -- <java args>` | Starts the app N times, parses the `Started ... in X seconds (process running for Y)` line, optionally polls a health URL until HTTP 200, prints median/min/max, appends a CSV row. Kills the app after each run. |
| `cpu_sweep.sh --image IMG` or `--jar app.jar --jvm-only` | Measures startup under several CPU limits and reports the knee point where more CPU stops helping. Docker mode also reports CPU-seconds consumed by the start. |
| `startup_report.py <actuator-startup.json or URL>` | Ranks the slowest startup steps and bean instantiations from the `/actuator/startup` endpoint. |
| `autoconfig_report.py <log with debug=true>` | Lists positive auto-configurations from the CONDITIONS EVALUATION REPORT and flags ones that are often unnecessary. |

Read `references/review-checklist.md` during the review step, `references/cds-aot.md` when you
reach tier 3, and `references/kubernetes.md` when writing the CPU recommendation. Use
`references/report-template.md` for the final report.

## Workflow

### Step 0: Scope and preconditions

- Identify the project directory. If the current directory has no `pom.xml` or `build.gradle(.kts)`, search one level down, then ask the user which project.
- Run `project_scan.sh`. Note Boot version, Java version, build tool, whether Actuator is present, and whether a Dockerfile or image exists. Several later choices depend on these (CDS needs Boot 3.3+, JDK AOT cache needs JDK 24+, lazy init needs Boot 2.2+).
- Check `java -version` and `docker info`. If Docker is not running, say so once and use `--jvm-only` for the CPU sweep; do not try to start the daemon.
- Ask how the app is normally started if it is not obvious (profile, env vars, external DB). An app that cannot reach its database will fail or hang at startup, and measuring that is useless. Prefer a local profile, Testcontainers, or an H2/embedded fallback the project already has. Never invent credentials.
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

1. Build once: `mvn -q -DskipTests package` or `./gradlew bootJar -x test`. Reuse the jar for every measurement in this step. Rebuilding between runs changes file-cache state and skews results.
2. Run `measure_startup.sh -n 5 -w 1 -l baseline -o <workspace>/results.csv -- -jar <jar> <app args>`. The `-w 1` warm-up run is discarded so disk cache does not favour later configurations. For web apps add `--server.port=0` to the app args so a port left open by a previous run never fails a measurement. Use the same `-n` for every later configuration.
   When the app has Actuator, also take one measurement with `-u http://localhost:<port>/actuator/health/readiness` and a fixed port. The `ready` column is wall time until the endpoint answers 200, which is what a readiness probe actually waits for; it includes the gap after the `Started` line (runners, `ApplicationReadyEvent` listeners) that neither log number shows.
3. Record **both** numbers from the log line. `in X seconds` is Spring's own time. `process running for Y` includes JVM start and class loading, and is the number a readiness probe experiences. If Y minus X is large (more than about 30 percent of Y), the problem is JVM/class loading and tiers 2 and 3 will matter more than bean tuning.
4. If Actuator is present, get a startup timeline: expose `startup` (`--management.endpoints.web.exposure.include=startup`) and, if the main class does not already set `BufferingApplicationStartup`, add it temporarily so the endpoint has data. Fetch `/actuator/startup` and run `startup_report.py` on it. Without Actuator, run once with `--debug` and feed the log to `autoconfig_report.py` instead.
5. Keep a workspace dir (for example `<project>/.startup-tuning/`) for CSVs, logs and reports, and tell the user it can be deleted. Add it to `.gitignore` only if the user agrees.

### Step 3: CPU sweep

Run the sweep before tuning, because the right CPU budget is a finding on its own and because it
tells you whether the app is CPU-bound at start (most are).

- With Docker and an image or Dockerfile: `cpu_sweep.sh --image <img> --cpus "0.5 1 2 4" -n 3`. The script also prints CPU-seconds used per start. Startup time at a limit L cannot be lower than CPU-seconds divided by L, so this number is the theoretical floor and the strongest argument for the recommendation.
- Without Docker: `cpu_sweep.sh --jar <jar> --jvm-only --cpus "1 2 4"`. This sets `-XX:ActiveProcessorCount` and shows how much JVM parallelism (JIT and GC threads) helps, but it does not throttle. Say clearly in the report that it is an approximation.
- On a laptop with no quota the `--jvm-only` sweep often shows almost no difference between 1, 2 and 4; that means the app is not limited by JVM parallelism there, not that 1 core is enough in the cluster. Only Docker mode answers the cluster question.
- The knee is the first point where adding CPU improves startup by less than about 15 percent. That is the recommended startup CPU. Below 2 cores the JVM usually collapses to one compiler thread and SerialGC, so a knee at 2 is the common result.

### Step 4: Tune in tiers, one change at a time

Apply changes in this order. After each change rebuild only if code or dependencies changed,
re-measure with the same `-n`, and compare medians. Keep a change only when the median improves by
more than the noise (rule of thumb: improvement greater than 5 percent **and** greater than the
baseline's max-minus-min spread). Otherwise revert it and record "no measurable effect". The user
needs to see what did not work as much as what did.

Machines drift: other processes, thermal throttling and file cache change the numbers over the
session. Compare each candidate against a control measured in the same batch (re-run the current
best configuration right before or after the candidate), not against a baseline from an hour ago.
If the control itself has moved by more than its spread, say so and re-measure.

After tier 3, re-check the tier 1 changes you kept by toggling each one off on top of the final
configuration. Spring AOT and CDS remove much of the work that tier 1 knobs were saving (bean
definition parsing, part of Hibernate bootstrap), so a change that was worth 13 percent on the plain
jar can be inside the noise afterwards. Report its value in the final configuration, and keep it only
if it still helps or has a reason beyond startup (for example `ddl-auto=none` in production).

**Tier 1, config and code, low risk**
- Remove unused starters and exclude auto-configurations reported by `autoconfig_report.py` that the app clearly does not use. Prefer removing the dependency over `spring.autoconfigure.exclude`; an exclusion hides the class but keeps the jar on the classpath.
- JPA: `spring.jpa.open-in-view=false`, `ddl-auto=none` outside dev, set `spring.jpa.database-platform` and `spring.jpa.properties.hibernate.boot.allow_jdbc_metadata_access=false`, `spring.jpa.defer-datasource-initialization` only when it exists.
- Hikari: lower `minimum-idle` or leave default pool lazily filled; do not pre-fill 20 connections at start.
- Move remote calls and cache warming out of constructors and `@PostConstruct` to an `@Async` listener on `ApplicationReadyEvent` **only if** readiness semantics allow it; ask when unsure.
- Narrow `scanBasePackages` if the scan covers unrelated packages.
- Disable JMX (`spring.jmx.enabled=false`, default off since Boot 2.2) and Hibernate statistics if on.

**Tier 2, JVM flags, applied in the Dockerfile or JAVA_TOOL_OPTIONS**
- `-XX:TieredStopAtLevel=1` only for dev or short-lived jobs; it hurts steady-state throughput, so never recommend it for a long-running production service without saying that.
- `-Xshare:auto` (default) plus `-XX:+UseSerialGC` for containers at or below 2 cores; `-XX:+UseParallelGC` or G1 above that.
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

Choose by versions found in Step 0:
- Boot 3.3+ on JDK 17 to 23: Spring AOT (`process-aot` / `processAot`, run with `-Dspring.aot.enabled=true`) plus CDS training run and `-XX:SharedArchiveFile`. Buildpacks: `BP_JVM_CDS_ENABLED=true`, `BP_SPRING_AOT_ENABLED=true`.
- JDK 24+: replace CDS with the JDK AOT cache (`-XX:AOTCache`); on JDK 25 use the one-step `-XX:AOTCacheOutput`. Boot 4 is built for this.
- These need the jar extracted (`-Djarmode=tools extract`) and an identical JDK, architecture and classpath order between training and runtime. Put the training run in the Dockerfile so the archive is rebuilt with every image; a stale archive is silently ignored and the gain vanishes without an error.
- Spring AOT fixes the bean graph at build time: profiles and `@ConditionalOnProperty` are evaluated during the build. Check that the training run uses the production profile set, or the AOT output will not match what runs in prod and Spring will refuse to start.

**Tier 4, recommend only, do not apply without explicit request**
- CRaC (Boot 3.2+, Linux only, CRaC-enabled JDK): tens of milliseconds restore, but open connections, secrets in the snapshot and operational complexity. Fit for scale-to-zero and serverless.
- GraalVM native image: sub-100 ms start, long builds, reflection hints needed, lower peak throughput. Fit for CLIs and functions, rarely for a large monolith.

### Step 5: Report and Kubernetes recommendation

Write the report with `references/report-template.md` into the workspace dir and summarise it in
chat. If writing the report file is refused, put the full report in your final message instead and
say that it was not saved; do not look for another way to write the file. The report must contain:
- A table of every configuration measured: label, median, min, max, n, kept or reverted.
- The CPU sweep table and the recommended startup CPU with the CPU-seconds floor argument.
- The exact diffs applied (file and property), and the list of findings not applied with reasons.
- k8s guidance from `references/kubernetes.md`: requests/limits strategy, `startupProbe` sized as measured startup at the chosen limit times 1.5, and the Prometheus throttling query to verify after deploy.
- What was approximated (for example `--jvm-only` sweep, no DB available, measured on a laptop not the cluster node).

Numbers belong in tables, not in sentences. Report failures honestly: if the app would not start without infrastructure you did not have, say what was skipped.

## Judgment calls

- A 10 percent gain that adds operational risk (CRaC, lazy init in prod) is not worth it for most teams; a 40 percent gain from CDS/AOT that is invisible at runtime almost always is. Say which category each change is in.
- Laptop numbers do not transfer to cluster nodes, but ratios do. Report relative improvements as the primary result and absolute times as secondary.
- Before a local Docker sweep, check that the image architecture matches the Docker host (`cpu_sweep.sh` refuses otherwise). An amd64 image on Apple Silicon runs under emulation and every number is wrong by a large, unknown factor.
- If cluster startup is much slower than the CPU-seconds floor predicts, the extra time is waiting, not computing: DB connection across the VPC, Flyway, remote config, DNS, Kafka metadata, cloud credential lookups (IRSA / instance metadata timeouts). The `/actuator/startup` timeline shows these as long single steps with little CPU. More CPU will not fix them.
- If the baseline is already under about 3 seconds on 2 cores, tell the user early that tier 3 is the only lever left and ask whether it is worth the Dockerfile change.
- When the user only wants a review, stop after Step 1 and Step 2 and hand over the findings; do not start changing files.
