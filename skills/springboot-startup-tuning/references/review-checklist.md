# Static review checklist

Work through each area and write findings as `(area, finding, expected impact, risk)`. Expected
impact is a guess at this stage; the measurements in Step 4 confirm or kill it. Risk is about
runtime behaviour change, not about startup.

## 1. Dependencies and starters

Look at `pom.xml` / `build.gradle(.kts)` (and `mvn dependency:tree` / `gradle dependencies` when
unsure what a starter pulls in).

| Look for | Why it costs startup | Action |
|---|---|---|
| Starters with no matching code (`data-redis`, `data-mongodb`, `amqp`, `kafka`, `mail`, `batch`, `integration`, `security` without any security config) | Each auto-config creates clients, pools and health indicators and may try to connect at start | Remove the dependency. Fall back to `spring.autoconfigure.exclude` only when the dependency is needed for a non-Spring reason |
| `spring-boot-devtools` in a production image | Restarter classloader, LiveReload server | Mark `optional`/`developmentOnly`; verify it is absent from the final jar |
| Micrometer registries you do not scrape (`prometheus`, `datadog`, `newrelic`, `statsd` all present) | Each registry starts a publisher thread and binds meters | Keep one |
| Spring Cloud (`config`, `eureka`, `consul`, `vault`, `bootstrap`) | Bootstrap context, remote config fetch with retries before the main context even starts | Check whether discovery/config is still used; set `spring.cloud.config.fail-fast` and sensible timeouts |
| Spring Cloud config client with `@RefreshScope` | Spring AOT does not support refresh scope; `process-aot` fails | Grep `@RefreshScope` before tier 3. Unused at runtime: `spring.cloud.refresh.enabled=false`. Used: skip Spring AOT, do CDS only |
| Many `${...}` placeholders without default (config from Config Server, Vault or env) | The app cannot start locally, so nothing can be measured | `project_scan.sh` counts them; generate stubs with `config_stub.py` and start with `--spring.config.additional-location=file:<stub>` |
| Both `spring-boot-starter-web` and `webflux` | Two server stacks on the classpath; Boot picks one, the other still gets scanned | Remove one |
| Hibernate Validator, Jackson modules, Thymeleaf/Freemarker not used | Classpath scanning and module registration | Remove |
| Large transitive trees (AWS SDK v1 full bundle, Google client libs) | Class count directly drives class-loading time, roughly 1 ms per 1 000 classes before CDS | Use per-service artifacts (`software.amazon.awssdk:s3` not `aws-java-sdk`) |

Count classes loaded once with `-Xlog:class+load:file=classload.log` and `wc -l`. Above ~15 000
classes, CDS / AOT cache (tier 3) is where most of the gain is, not bean tuning.

## 2. application.yml / properties

| Property | Startup effect | Suggested value |
|---|---|---|
| `spring.jpa.hibernate.ddl-auto` | `validate`/`update` read the whole schema metadata at start; on large schemas this is seconds | `none` outside dev; use Flyway/Liquibase for schema |
| `spring.jpa.open-in-view` | Not startup itself, but a warning and an interceptor; turn off while you are in here | `false` |
| `spring.jpa.database-platform` + `spring.jpa.properties.hibernate.boot.allow_jdbc_metadata_access=false` | Lets Hibernate skip the JDBC metadata round-trip at boot (Hibernate 6.2+) | Set both |
| `spring.jpa.defer-datasource-initialization` / `spring.sql.init.mode` | SQL scripts executed at start | `never` in prod |
| `spring.datasource.hikari.minimum-idle` | Only the first connection is opened during startup; the rest fill in the background | Leave default; not a startup lever |
| `spring.datasource.hikari.initialization-fail-timeout` | Default 1 blocks until first connection succeeds; `-1` skips the check | Keep default in prod (fail fast), `-1` only when the DB is legitimately not needed at boot |
| `spring.flyway.*` / `spring.liquibase.*` | Migration runs on every start; on a big changelog, checksum validation alone is slow | Keep, but know its cost from the startup timeline; consider running migrations as a separate job |
| `spring.main.lazy-initialization` | Beans created on first use | `true` only in dev/test profiles unless the user accepts first-request latency and late failures |
| `spring.main.banner-mode`, `spring.output.ansi.enabled` | Negligible; do not report these as wins | Leave |
| `spring.jmx.enabled` | MBean export | `false` (default since Boot 2.2, check it was not re-enabled) |
| `management.endpoints.*`, `management.metrics.*` | Each exposed endpoint and binder has a cost; `metrics.enable.*` can turn off whole groups | Expose only what is scraped |
| `spring.cache.type`, `spring.session.store-type` | Autodetect scans for providers; explicit value skips that | Set explicitly |
| `logging.level.root=DEBUG`, `debug=true`, `trace=true` | Logging itself is slow; a DEBUG root level can add seconds | INFO in prod |
| `server.tomcat.threads.min-spare` / `max` | Thread pre-creation; small cost | Default is fine |

Also check profile files: a tuning property set in `application.yml` can be overridden back in
`application-prod.yml`. Review the resolved set for the profile that runs in production.

## 3. Code

Search for these patterns (`grep -rn` across `src/main/java` and Kotlin sources):

- Constructors or `@PostConstruct` that do I/O: HTTP clients called, caches warmed, files parsed, `Thread.sleep`, `.block()`, `CompletableFuture.get()`.
- `CommandLineRunner` / `ApplicationRunner` / `@EventListener(ContextRefreshedEvent)` doing bulk loads. Move to `ApplicationReadyEvent` and make it `@Async` only when readiness does not depend on it.
- `@Scheduled(initialDelay = 0)` or `fixedRate` tasks that start heavy work right at boot and compete for CPU with the remaining bean creation.
- `@EnableAsync`, `@EnableScheduling`, `@EnableCaching` on apps that do not use them (each adds post-processors that proxy every bean).
- `@ComponentScan` / `scanBasePackages` wider than the project (`com.company` instead of `com.company.thisservice`).
- `@EnableJpaRepositories` with `basePackages` covering hundreds of repositories; also `spring.data.jpa.repositories.bootstrap-mode=deferred` or `lazy` lets repository proxies be created in a background thread. Boot 4.1 adds `spring.jpa.bootstrap=async`, which builds the `EntityManagerFactory` on a background thread. `deferred` and `async` need an `AsyncTaskExecutor` bean; Boot's `applicationTaskExecutor` is enough, but it backs off when the app defines its own `Executor`. On 4.1 without one, startup fails with `An EntityManagerFactoryBean bootstrap executor is required` (verified on 4.1.3); `lazy` needs none. The gain grows with the entity model and the database round trips; on a 1-entity H2 demo (4.1.3) neither changed startup beyond noise, so measure before keeping it.
- Static initializers that build large maps, load JSON fixtures, or compile regexes at class-load time in classes that are loaded at boot.
- Manual `new RestTemplate()` / `WebClient` per bean versus a shared builder (many SSL contexts initialise slowly).
- Kotlin: `kotlin-reflect` on the classpath plus heavy data classes; Jackson Kotlin module reflection at boot.

Prefer finding these from the startup timeline (`startup_report.py`) rather than by reading every
class; use grep to confirm what the timeline points at.

## 4. Dockerfile / container image

- Base image JDK version versus the Boot version (Boot 3.x and 4.x need 17+; Boot 4.1 with jOOQ needs 21+, because jOOQ 3.20 does). Newer JDKs start noticeably faster, and the JDK AOT cache needs 24+.
- `-Djarmode=layertools` in the Dockerfile: removed in Boot 4.1, the build fails. Replace with `-Djarmode=tools extract --layers --launcher` (see `cds-aot.md`).
- Fat jar run with `java -jar app.jar`: no layering, no CDS possible. Prefer the extracted layout (`-Djarmode=tools extract`) so the CDS/AOT training run and layer caching work.
- `ENTRYPOINT` with `sh -c` wrapping: adds a shell process, breaks signal handling, and `exec` form is faster.
- JVM flags present: `-Xshare`, `-XX:SharedArchiveFile`, `-XX:AOTCache`, GC choice, `MaxRAMPercentage`, `TieredStopAtLevel`. Note each one and whether it matches the CPU limit the app actually gets.
- `JAVA_TOOL_OPTIONS` versus `JAVA_OPTS`: only the first is read by the JVM automatically.
- Buildpacks (`spring-boot:build-image` / `bootBuildImage`): check `BP_JVM_CDS_ENABLED`, `BP_SPRING_AOT_ENABLED`, `BP_JVM_VERSION`.
- Multi-stage build that compiles inside the image but ships the JDK instead of a JRE: not a startup issue, but worth a line.

## 5. Kubernetes / deployment manifests

- `resources.requests.cpu` and `limits.cpu`: anything at or under `1` without a startup probe is the single most common cause of "the app starts slowly" tickets. Record the values.
- `startupProbe` present? `failureThreshold * periodSeconds` must exceed the measured startup at the configured limit, with margin. Without it, `livenessProbe` kills the pod mid-start and the restart loop looks like a hang.
- `readinessProbe` path: `/actuator/health/readiness` versus plain `/actuator/health` (the latter includes DB and external checks that may flap).
- `initialDelaySeconds` on probes hard-coded to a guess (`60`) that is far above or below the measured value.
- HPA minimum replicas and `terminationGracePeriodSeconds` do not affect startup but matter for rollout time; mention only if asked.
- `JAVA_TOOL_OPTIONS` in the pod env overriding the Dockerfile flags.
- Liveness probe on plain `/actuator/health`: the aggregate includes the database and other dependencies, so a DB blip restarts every pod. Use `/actuator/health/liveness` and `/actuator/health/readiness`.
- `management.endpoint.health.group.*.include` naming a contributor that no bean provides: Boot validates group membership at startup and throws `NoSuchHealthContributorException`, so the whole context fails. A warm-up indicator in the readiness group must be registered unconditionally.
- A readiness group that includes `db` (or any external dependency): one dependency blip marks every pod NotReady at once and empties the Service. Flag it; it is a judgment call, not automatically wrong.

## Warm-up code, if the project has any (Step 5)

- Warm-up targets chosen from a URL string or an HTTP verb rather than from the controller method's call graph: a `GET` can still reach another service or a write through response assembly. Trace the method.
- Warm-up that serializes a service return value instead of the real response envelope: the envelope's serializer stays cold.
- Two meters sharing a name with different tag keys (for example a per-target failure counter and a global one): a Prometheus registry drops the second silently, so the alert-able metric disappears. One tag-key set per name.
- Warm-up state in the liveness group, a warm-up that can throw out of `run()`, or a readiness gate released after logging and metrics instead of before them.
- Warm-up concurrency at or above `server.tomcat.threads.max` or the Hikari pool size, and loopback warm-up driven from the main thread in parallel: both can starve the probe that must answer during warm-up.

## Runtime efficiency (Step 6)

Not startup costs, but read the same files for them while you are here and record them for Step 6.

| Look for | Why it costs at runtime | Action |
|---|---|---|
| No `MaxRAMPercentage` / `-Xmx` in the Dockerfile or `JAVA_TOOL_OPTIONS` | Max heap is 25 percent of the container memory | `-XX:MaxRAMPercentage=75 -XX:InitialRAMPercentage=75` when memory request equals limit; less with many threads or direct buffers |
| No CPU limit and no `-XX:ActiveProcessorCount` | The JVM sizes GC, JIT and common-pool threads for every core on the node | Set `ActiveProcessorCount` to the CPU request rounded up, at least 2 |
| Pod with less than 2 CPUs or about 1.8 GB and no GC flag | The JVM silently runs SerialGC | Pick the GC explicitly and measure |
| `maximum-pool-size` × replicas (and HPA max) above the database's `max_connections` | Connection storms, refused connections at scale-out | Small pool per pod, sized from the HikariCP guideline |
| `RestClient`/`RestTemplate` on Apache HttpClient 5 without a custom connection manager | 5 connections per route, 25 total | Set max-per-route, total and all three timeouts |
| Outbound clients with no timeouts | Threads or virtual threads pile up behind a slow downstream | Connect, read and pool-acquire timeouts on every client |
| `spring.threads.virtual.enabled=true` on JDK 21 to 23 | `synchronized` pins carrier threads (fixed by JEP 491 in JDK 24) | JDK 25, or leave platform threads |
| `GenerationType.IDENTITY` on entities inserted in bulk | Disables JDBC insert batching | SEQUENCE with a pooled optimizer, `hibernate.jdbc.batch_size`, `order_inserts` |
| HTTP or messaging calls inside `@Transactional` methods | Holds a DB connection for the whole remote call | Move the remote call outside the transaction |
| Unbounded caches (`new ConcurrentHashMap` caches, Caffeine without `maximumSize`) | Heap grows with traffic until GC thrash or OOM | Bound size and expiry |
| String concatenation in log calls, payload logging, `%L`/`%M` or caller data in log patterns | CPU and allocation per request, even when the level is off for concatenation | Parameterized logging, no payloads, no location info |
| Metric tags built from user IDs, raw paths or request input | Unbounded cardinality, memory growth | Bounded tag values only |
| `percentiles-histogram` off for `http.server.requests` | p99 cannot be computed across pods | Turn it on for the service's main timers |
