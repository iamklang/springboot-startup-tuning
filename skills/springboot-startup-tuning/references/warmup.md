# First-request latency and warm-up

Read this in Step 5, when the problem is how slow the *first requests* are after the app is
already up, not how long it takes to start. Symptoms: p99 spikes or 5xx right after every deploy
or scale-out, a pod passes its readiness probe and then times out, HPA adds pods that are slow and
burn CPU, or the user says "request แรกช้า" or "หลัง deploy ช้าอยู่พักหนึ่ง".

## Contents
1. Where the time goes after "Started"
2. Measure (W0, W1)
3. Spring and library fixes (W2)
4. In-app warm-up gated by readiness (W3), over loopback
4b. In-process warm-up, no HTTP call
5. Probes, CPU headroom and rollout on Kubernetes (W4, W5)
6. JVM: AOT cache trained on real requests, CRaC (W6, W8)
7. Load-balancer slow start (W7)
8. Reference numbers

## 1. Where the time goes after "Started"

There are three separate costs. A fix for one does nothing for the others, so name which one you
are attacking.

| Cost | Examples | Fixed by |
|---|---|---|
| Lazy one-time init in Spring and libraries | DispatcherServlet init, Jackson serializer cache per type, Bean Validation metadata (the first validated request can be 20 to 40 times slower), Hibernate query plan cache, Spring Data derived-query parsing, Hikari connection to its minimum size, lazy-init beans | `load-on-startup`, in-app warm-up |
| Class loading and linking of the request path | web and serialization classes loaded on first use | in-app warm-up, AOT cache, CDS (partly) |
| JIT: interpreter, then C1, then C2 | the hot path runs interpreted for the first thousands of calls, and C2 needs many more plus CPU for the compiler threads | real traffic or a long warm-up, AOT cache profiles (JEP 515), CRaC, CPU headroom |
| Runtime state outside the JVM | DNS (the JVM caches 30 s by default), TLS handshakes to dependencies, Kafka producer metadata on the first `send()`, OAuth/JWKS fetch, remote config | warm-up that touches those clients, connection pools with a fixed minimum |

A CPU limit sized for steady state makes all of these worse, because the JIT compiler threads
compete with request threads and get throttled. In the case studies, warm-up needed about three
times the steady-state CPU; without that headroom a warm-up script barely helps.

## 2. Measure (W0, W1)

Measure from the moment readiness first answers 200, because that is when the load balancer starts
sending traffic.

**First-N curve (always).** Use `scripts/measure_first_requests.sh`. It restarts the app R times,
waits for readiness, then sends N sequential requests that rotate through the URLs you give it.

```bash
measure_first_requests.sh -R 3 -n 100 -l baseline -o first.csv -d curve-baseline.csv \
  -r http://localhost:8080/actuator/health/readiness \
  -u 'http://localhost:8080/api/orders?customer=1' -u 'http://localhost:8080/api/products/42' \
  -- -jar app.jar --server.port=8080 --management.endpoint.health.probes.enabled=true
```

- Use a fixed port, because readiness and the URLs need it. `-H 'Authorization: Bearer …'` adds headers; take tokens only from the user or a local test profile, never invent them.
- Choose 2 to 5 URLs that are the real hot paths: one read with a database query, one that serializes a large DTO, one with validation. Use only idempotent requests.
- The summary line gives the median over runs of request 1, the mean of requests 2 to 10, the steady value (last 10), and first/steady. A first/steady ratio above about 5 or a request-2-to-10 mean above twice steady means warm-up is worth doing.
- The same keep rule as Step 4 applies: same R and N for every configuration, a control in the same batch, and keep a change only if request 1 and the 2-to-10 mean beat the control by more than its spread.

**Under load (when the user has a load tool or a staging environment).** Sequential curl shows
the one-time costs but not the JIT-under-load tail. For that, use an open-model generator at the
real per-pod rate, not maximum throughput: k6 `constant-arrival-rate`, `vegeta attack -rate`, or
`wrk2 -R`. Closed-model tools (plain wrk, ab, JMeter thread groups) hide stalls because they stop
sending while a request is slow (coordinated omission). Report p99 per 10-second bucket from
readiness to steady state, and the "time to warm": when p99 first stays within 1.5 times steady.
wrk2 needs runs of at least 10 s, so use curl for the first-N curve.

**CPU (W1).** Repeat the first-N measurement at the CPU limits from Step 3 (`docker run --cpus`, or
`-XX:ActiveProcessorCount` as the approximation). If request 1 or the time to warm improves sharply
from 1 to 2 CPUs, the recommendation is CPU headroom during warm-up (W5), not more code.

## 3. Spring and library fixes (W2)

Each one is a single property or a small change. Measure each with the first-N curve.

- `spring.mvc.servlet.load-on-startup=1`: the DispatcherServlet initialises at startup instead of on the first request. Boot's default is `-1` (confirmed in the `spring-configuration-metadata.json` of spring-boot-autoconfigure 3.2.4), so without this the first request pays servlet init.
- Global lazy initialization moves bean creation into the first request. If `spring.main.lazy-initialization=true` is on in production, this is often the whole problem.
- Hikari: `minimum-idle` equal to `maximum-pool-size` does **not** fill the pool at startup. HikariCP creates one connection at init (that is all `initializationFailTimeout` guarantees) and its housekeeper grows the pool toward `minimum-idle` in the background; `com.zaxxer.hikari.blockUntilFilled` is a JVM system property read by `HikariPool`, not a `spring.datasource.hikari.*` option, so treat it as unsupported. (Verified in HikariCP 5.0.1: the constructor calls `checkFailFast()`, and the pool has a `houseKeeperTask` plus `fillPool(boolean)` driven by `com.zaxxer.hikari.housekeeping.periodMs`.) To have more than one warm connection before traffic, borrow N connections *simultaneously* in warm-up and run a trivial query on each, then close them. Keep N well below `maximum-pool-size`, or the readiness `db` contributor waits for a connection and the probe fails.
- Kafka producers: call `partitionsFor(topic)` in the warm-up runner so the first `send()` does not wait for metadata. Do not send messages.
- Clients to other services (RestClient, WebClient, Feign): one call to a health or cheap read endpoint in warm-up opens the TLS session and resolves DNS. Only when the user agrees, because it touches another service.
- Reactor Netty (WebFlux, WebClient): `HttpClient.warmup().block()` and `HttpServer.warmup()` preload the event loops and native transport.
- Hibernate: the query plan cache fills on first use. Warm-up requests that run the main queries fill it.

## 4. In-app warm-up gated by readiness (W3), over loopback

### How it works

Spring Boot runs startup in this order:
1. It creates the beans and starts the embedded web server, which already accepts connections.
2. It runs every `ApplicationRunner` and `CommandLineRunner`. Readiness still answers 503.
3. When all runners have returned, it publishes `ReadinessState.ACCEPTING_TRAFFIC`, readiness answers 200, and Kubernetes starts routing traffic.

`assets/WarmupRunner.java` runs in step 2. It calls the app's own endpoints over HTTP on
`127.0.0.1:<local.server.port>`, so every request goes through filters, security, MVC, Jackson,
validation and JPA while no user traffic can arrive. Liveness is already CORRECT, so the pod is not
restarted during warm-up.

Calling service methods directly instead (section 4b) reaches the same expensive per-type caches
and loses only the HTTP plumbing. Do not claim it "warms much less" without measuring: on a
Boot 3.2.4 / JDK 21 service the difference in a CDS archive was the connector path only
(`org.apache.coyote.http11` 26 classes with loopback versus 4 without), while the Hibernate query,
Validator metadata, MapStruct and service classes were identical.

### Install

1. Copy both templates into the main class's package or a sub-package, so component scan finds them:
   `cp <skill>/assets/WarmupRunner.java <skill>/assets/WarmupObservationFilter.java src/main/java/<pkg path>/`
   `WarmupObservationFilter` keeps the warm-up calls out of `http.server.requests` and out of traces
   (see "Keeping telemetry clean" below). Skip it only for an app without Micrometer observation.
2. Replace `package CHANGE_ME;` with the real package in both files.
3. Add the properties to the production configuration (or the profile used in the cluster):

```yaml
app:
  warmup:
    enabled: true
    paths: /api/orders?customer=1,/api/products/42     # GETs
    post:                                               # optional, side-effect-free POSTs only
      "[/api/quote]": '{"customer":"c1","lines":[{"sku":"a","qty":2}]}'
    iterations: 3
    max-duration: 20s
management:
  endpoint.health.probes.enabled: true   # exposes /actuator/health/readiness and /liveness
```

4. The runner is off when `enabled` is absent. If it is turned on in the main `application.yml`, set `app.warmup.enabled: false` in the test profile so tests do not slow down.

| Property | Meaning |
|---|---|
| `enabled` | Turns the runner on. Missing means off. |
| `paths` | GET paths, comma-separated, query strings allowed, no commas inside a path. No default: an empty list logs a warning and skips. `server.servlet.context-path` is prepended, so write paths without it. |
| `post` | Map of path to JSON body, sent as POST with `Content-Type: application/json`. Keep the brackets around the key in YAML (`"[/api/quote]"`) or the slashes are dropped. Only for endpoints that cannot change state: quote, validate, dry-run. Without it a validated POST endpoint stays cold: on the demo its first call was 45 ms with GET-only warm-up and 1.6 ms with this. |
| `iterations` | Rounds. Each round calls every path once, so total requests = iterations × number of paths (6 in the example). Default 3. Start at 1 to 3: the costs removed are one-time, and on the demo one round gave almost all of the gain (table below). Raise it only when the measurement shows requests 2 to 10 still clearly above steady state. |
| `max-duration` | Hard time budget. The runner stops when it runs out, even mid-round, and logs it. It must fit inside the startupProbe window. |

The runner skips itself, with a log line, when there is no embedded server (tests with a mock
environment) or when `server.ssl.enabled=true`, because a loopback HTTPS call would need the server's
certificate in a trust store. It calls `server.address` when that is set, otherwise `127.0.0.1`.

Validated with the install steps above on Boot 3.0.4, 3.5.7, 4.0.0 and 4.1.3 (JDK 21, Security, JPA,
validation): it compiles unchanged, the log shows `0 failed, 0 non-2xx`, request 1 of every warmed
endpoint drops to steady state, and `WarmupObservationFilter` keeps warm-up calls out of
`http.server.requests` while a client sending `X-Warmup: true` is still counted. On 4.1.3 it also
worked in a Spring AOT + CDS build, with `spring.jpa.bootstrap=async`, with deferred repositories and
with `spring.datasource.connection-fetch=lazy`.

### How much warm-up is enough: measured

Demo app, Boot 3.5.7, JDK 21, Spring Security, JPA + H2, `@Valid` request body, three endpoints
(GET by id with a fetch join, GET search with a derived query, POST quote). Median of 5 cold starts per
row, rows interleaved, laptop without a CPU limit (load about 4). Compare ratios, not absolute values.

| Warm-up | Request 1, ms (get / search / quote) | Readiness later by | Extra `http.server.requests` per start |
|---|---|---|---|
| none | 26.4 / 14.2 / 48.6 | – | 0 |
| generic metadata prefetch (Jackson `writerFor`/`readerFor`, `getConstraintsForClass` for every handler) | 25.3 / 14.3 / 22.5 | 0.08 s | 0 |
| in-process: service + `ObjectMapper` + `Validator` calls (section 4b) | 5.3 / 8.5 / 5.7 | 0.07 s | 0 |
| in-process dispatch: security filter chain + `DispatcherServlet` with mock request objects | 2.8 / 5.9 / 2.0 | 0.04 s | 0 |
| loopback HTTP, 1 round | 3.1 / 3.0 / 1.6 | 0.17 s | 3 (0 with the observation filter) |
| loopback HTTP, 20 rounds | 1.7 / 1.9 / 1.1 | 0.22 s | 60 (0 with the observation filter) |

What it shows:
- One round removes nearly all of the first-request cost. Twenty rounds buy about 1 ms more per endpoint and twenty times the self-traffic.
- Generic metadata prefetch, which needs no knowledge of the business code, helps only request-body deserialization. It is not a substitute for executing the path.
- In-process calls reach the per-type caches but leave 4 to 7 ms of web-layer first-call cost (handler mapping, argument resolvers, message converters, filters).
- In-process dispatch is close to loopback but needs `spring-test` (`MockHttpServletRequest`) on the production classpath. Do not recommend it.

### Keeping telemetry clean

Loopback requests go through `ServerHttpObservationFilter`, so without a filter every start adds
`iterations × paths` samples to `http.server.requests` and the same number of trace spans.
`assets/WarmupObservationFilter.java` registers an `ObservationPredicate` bean. Spring Boot applies
every such bean to the `ObservationRegistry`, and a server request whose `X-Warmup` header equals the
runner's per-JVM random token becomes a no-op observation: no timer sample, no span. Verified on the
demo: the count for `/orders/{id}` stayed 0 after warm-up, and a normal request plus a request sending
`X-Warmup: true` from outside were both counted.

Do not match on a fixed header value or on the loopback address. With an Istio or Envoy sidecar every
external request reaches the app from `127.0.0.1`, so either check would let any client hide its own
traffic. The template's filter covers Spring MVC only; WebFlux needs the reactive
`ServerRequestObservationContext`. Tomcat's access log, when enabled, still records the requests.

### Choosing paths

- Pick 2 to 5 of the most-called endpoints: one that queries the database, one that serializes a large DTO, one with validation. Use the same URLs as the `-u` arguments of `measure_first_requests.sh`, so the measurement shows the effect.
- Only side-effect-free requests: GETs in `paths`, and POSTs in `post` only for endpoints that compute and return (quote, validate, dry-run). No request may write data, send a message, or call another service. Confirm this with the user for every path, and refuse payment, order-creation or notification endpoints.
- Use IDs that exist in every environment. A 404 returns before the service and repository code run, so it warms only part of the path.
- Endpoints behind authentication answer 401 to the template, which warms only the filter chain. Choose public read endpoints, or add a warm-up credential header that the app reads from its own configuration (for example a client-credentials token for a local-only scope). Never disable security for warm-up, and never put a real user's token in config.
- To warm a POST endpoint that does write, do not call it. Either warm a side-effect-free sibling that uses the same request DTO and validation (a `/validate` or dry-run endpoint), or send a deliberately invalid body that Bean Validation rejects with 400 and accept the `non-2xx` warning. In both cases first check that nothing writes or calls out before validation runs (no `@Transactional` work in a filter or an interceptor ahead of the controller).

### Verify

- The startup log ends the warm-up with one line:
  `warm-up done: 100 requests, 0 failed, 0 non-2xx, 850 ms`
  `failed` counts HTTP 5xx and connection errors. `non-2xx` counts every other non-2xx answer, usually 401 or 404 from a wrong path, a missing ID or missing auth. Both should be 0. The first problem of each kind is logged as a warning with its path.
- Measure with `measure_first_requests.sh` twice, `--app.warmup.enabled=false` then `true`, with the same `-u` URLs, R and N. Compare request 1, the mean of requests 2 to 10 and first/steady.
- Measure the `ready` time with `measure_startup.sh -u .../actuator/health/readiness` too. Warm-up moves time from the first user requests to before readiness, and the report must show both sides. On the first demo app request 1 fell from 57.6 ms to 2.5 ms and readiness came about 0.25 s later; on the second demo one round was enough (table above).
- Size the startupProbe for startup plus `max-duration` (section 5).

### Rules to tell the user and to check in review

- Never fail startup because warm-up failed. The template logs and continues. Release the readiness gate **before** logging and before touching metrics, so a failure in those cannot hold the pod out of the Service.
- A readiness gate as a `HealthIndicator` (bean name `warmup`, `management.endpoint.health.group.readiness.include=readinessState,warmup`) is a one-way latch that is already in place at the first probe, so it has no race. But **Boot validates group membership at startup**: a group naming a contributor that does not exist throws `NoSuchHealthContributorException` and the context fails. Register the indicator unconditionally and have it report UP with detail `disabled` when warm-up is off; put only the runner behind `@ConditionalOnProperty`.
- One tag-key set per meter name. A Prometheus registry **silently drops** a second meter that reuses a name with different tag keys — verified on Micrometer 1.12.4: the registry logs through `meterRegistrationFailed` and does not throw, so a `warmup.failures{reason}` counter registered after `warmup.failures{reason,target}` never appears in the scrape and an alert on it can never fire.
- The template has no `@Order`, so it runs with the lowest precedence. Runners that load data or prime caches must run first: give them an `@Order` with a smaller value **on the runner class**. Observed on Boot 3.0.4: `@Order` on an `@Bean` method that returns an `ApplicationRunner` was ignored, the warm-up ran before the seed data existed and logged `404 ... warms only part of the path`; the same order on the class worked. Boot 3.5.7 honoured both.
- Exclude warm-up requests from metrics and traces with `WarmupObservationFilter` (above). Business code that counts events itself (audit, analytics) can check the same header against `WarmupRunner.TOKEN`. Never accept a fixed header value as proof of a warm-up request.
- If the warm-up must run asynchronously, the app has to keep readiness down itself: publish `AvailabilityChangeEvent.publish(ctx, ReadinessState.REFUSING_TRAFFIC)` before and `ACCEPTING_TRAFFIC` after. Prefer the synchronous runner.
- Under Spring AOT, set `management.endpoint.health.probes.enabled=true` explicitly, because cloud-platform detection is fixed at build time.
- With a low CPU limit the warm-up itself is throttled and helps little. Fix CPU headroom (W5) first or together.

Expect this to remove the one-time costs (request 1 drops close to the 2-to-10 mean) but not the
whole C2 warm-up, which needs far more calls than a short warm-up sends. Slow start (W7) or CPU
headroom (W5) covers the rest.

## 4b. In-process warm-up, no HTTP call (W3 variant)

Prefer this when the team wants the warm-up's side-effect safety checked by the compiler rather
than by reviewing URL strings, or when no HTTP self-call is acceptable at all. If the only concern is
clean telemetry, loopback plus `WarmupObservationFilter` already gives that and warms the web layer
too; measured, in-process calls left 4 to 7 ms of first-call cost per endpoint that one loopback
round removed (section 4, "How much warm-up is enough").

What it reaches, equal to loopback: Hibernate query plan cache, the Hikari pool, MapStruct mappers,
Bean Validation metadata, and — this is the load-bearing detail — the Jackson serializer cache,
**provided the app's `ObjectMapper` is a context bean**. Boot builds
`MappingJackson2HttpMessageConverter` from the `ObjectMapper` bean (verified in
spring-boot-autoconfigure 3.2.4: `JacksonHttpMessageConvertersConfiguration` calls
`new MappingJackson2HttpMessageConverter(objectMapper)`), so serializing in-process fills
the very cache the first response would have had to build. Check for a `@Primary ObjectMapper`
bean first; a client-side converter built with `new MappingJackson2HttpMessageConverter()` has its
own mapper and is **not** warmed this way.

What it cannot reach: the connector (Coyote/HTTP11), the filter chain, the observation filter,
HandlerMapping, argument resolvers and the converter wiring. For class loading, a CDS or AOT cache
covers most of that; the rest is one request's worth of dispatch cost.

Trade-offs to state explicitly:

| | Loopback | In-process |
|---|---|---|
| HTTP plumbing | warmed | not warmed |
| Change the warm-up targets | config only, per environment | code change plus a release |
| Side-effect safety | a URL string; an endpoint can gain a write or a downstream call later without anyone noticing | the call site and its signature are compile-checked |
| Target rot | a renamed path returns 404 and warms almost nothing, silently | a renamed method fails the build |
| App telemetry | nothing extra with `WarmupObservationFilter`; without it, real `http.server.requests` samples and trace spans | nothing extra published |
| Thread safety | the main thread blocks on a request served by its own worker pool; a footgun if anyone parallelises it later | no self-request |
| Pool priming | one connection per sequential request | can hold N connections at once |

**Fidelity rule.** Serialize through the real response shape, not the service return value. A
controller that wraps results in an envelope (`GeneralResponse<T>`, `ApiResult<T>`) leaves the
envelope's serializer cold otherwise — measured as one missing class in the archive comparison. A
response type declared inside a controller method (a local `record`) cannot be built from outside
at all, which is the clearest case of this approach drifting from what the controller really does.

Shape (an `ApplicationRunner`, same readiness gate and budget as the loopback template):

```java
private record Task(String name, Runnable action) { }

private List<Task> tasks() {
    return List.of(
            new Task("service-areas", () -> serializeResponse(serviceAreaService.getServiceAreas())),
            new Task("request-validation", () -> validator.validate(new PageableRequest())),
            new Task("connection-pool", this::primeConnections));
}

private void serializeResponse(Object data) {      // wrapper included, see the fidelity rule
    objectMapper.writeValueAsString(new GeneralResponse<>(SUCCESS, data));
}
```

Pick read-only targets by **tracing the controller method's call graph**, not from the URL or the
HTTP verb. A `GET /v1/bookings/{id}` on one real service called two other services through the
response-assembly path; only reading the method showed it. The same discipline applies to the
loopback template's `paths`.

## 5. Probes, CPU headroom and rollout on Kubernetes (W4, W5)

- **startupProbe** must cover startup plus warm-up, measured at the CPU *request*, not the limit, because a busy node gives only the request. `failureThreshold = ceil((startup + warm-up) * 1.5 / periodSeconds)`. Choose what the startupProbe points at deliberately:
  `/actuator/health/readiness` (below) means a warm-up that overruns the budget gets the pod **restarted**;
  `/actuator/health/liveness` passes as soon as the context refreshes, so warm-up length can never cause a
  restart, at the cost that a warm-up which never finishes leaves the pod NotReady forever instead of
  restarting. Name the trade-off for the user; point the readiness probe at readiness either way. Keep liveness on `/actuator/health/liveness` and never put the warm-up state in the liveness group.
  Example for 8 s startup plus a 20 s warm-up budget, ceil(28 × 1.5 / 5) = 9, rounded up for margin:

  ```yaml
  startupProbe:
    httpGet: { path: /actuator/health/readiness, port: 8080 }
    periodSeconds: 5
    failureThreshold: 12
  readinessProbe:
    httpGet: { path: /actuator/health/readiness, port: 8080 }
  livenessProbe:
    httpGet: { path: /actuator/health/liveness, port: 8080 }
  ```
- **Rollout:** `maxSurge: 1`, `maxUnavailable: 0`, and `minReadySeconds` equal to the measured time to warm, so old warm pods are not removed before new ones have warmed.
- **CPU headroom (W5),** pick one:
  - Burstable QoS: request at steady state, limit 2 to 4 times higher or no limit (see `kubernetes.md`). Simplest and the most effective lever in the case studies.
  - In-place pod resize (GA in Kubernetes 1.35) through kube-startup-cpu-boost. It is v1alpha1 and "not an officially supported Google product"; say so. Use `fixedDuration` covering startup plus time to warm. A boost that ends at `podCondition: Ready` ends exactly when real traffic and C2 compilation start.
  - With any boost, set `-XX:ActiveProcessorCount` explicitly. The JVM sizes JIT and GC threads once at start from the CPUs it sees, and does not shrink them when the boost ends.
- **HPA:** cold pods use more CPU, so CPU-based HPA can add more cold pods in a loop. Readiness-gated warm-up breaks the loop. Suggest scaling on RPS or pre-scaling before known peaks when the user has that problem.

## 6. JVM: AOT cache trained on real requests, CRaC (W6, W8)

- The default training recipe in `cds-aot.md` (`-Dspring.context.exit=onRefresh`) improves startup only. It records no method profiles, so it does nothing for warm-up. It also exits before the app serves anything, so none of the request-path classes are in the archive.
- **A CDS archive stores classes, not cache state.** Running warm-up during training adds only the classes those calls touch; the warmed query plans, serializers and validator metadata are runtime state that no archive can carry. Measured on one service: Hibernate query (422), Validator metadata (86), mapper (46) and service (60) class counts were identical with no warm-up, with loopback warm-up and with in-process warm-up.
- A training run has no database, so every warm-up task that needs one fails after loading its classes and contributes nothing further. Either accept that, or train in a CI step against a real test container (`cds-aot.md`).
- For warm-up on JDK 25+: train with `-XX:AOTCacheOutput=app.aot` while sending representative requests (start the app, run the warm-up runner or a short load against the hot endpoints, then stop it cleanly so the cache is written on exit). Run with `-XX:AOTCache=app.aot`. JEP 515 then stores method profiles, so the JIT starts from known hot methods. Measure both startup and the first-N curve, and check steady-state throughput too: there is an open JDK bug about lower PetClinic peak performance with an AOT cache.
- The training run needs whatever the requests touch (a database, for example). If that is not available in the image build, train in a CI step against a test container and copy the cache in, with the same JDK, architecture and classpath.
- CRaC is the only HotSpot option that removes both class loading and JIT warm-up, and only if the checkpoint is taken on demand after warm-up traffic (`jcmd <pid> JDK.checkpoint`). The automatic `-Dspring.context.checkpoint=onRefresh` mode does not give a warm JVM. Tier 4: recommend, do not apply without an explicit request.
- `-XX:TieredStopAtLevel=1` helps only at 1 CPU or less and lowers peak throughput. Not a warm-up fix for a long-running service.

## 7. Load-balancer slow start (W7)

Slow start ramps a new endpoint's share of traffic over a window, so C2 compiles under a small
load. It works only with round-robin or least-request balancing, not with hash or session affinity.

| Proxy | Setting | Caveat |
|---|---|---|
| Istio | `DestinationRule.trafficPolicy.loadBalancer.warmup: {duration, minimumPercent, aggression}` (`warmupDurationSecs` is deprecated) | Little effect when all endpoints are new at once |
| Envoy | `slow_start_config: {slow_start_window, aggression, min_weight_percent}` | RR, least request, WRR only |
| AWS ALB | target group attribute `slow_start.duration_seconds` | round_robin only; no ramp when every target in the group is new (whole-group blue/green) |
| Azure App Gateway for Containers | `BackendLoadBalancingPolicy.slowStart` | Not with session affinity or ring hash |
| HAProxy | `slowstart <ms>` on the server | Applies after a server comes back from failed, not at HAProxy start |
| NGINX | `server … slow_start=30s` | Documented as a commercial feature; unconfirmed for open source |
| Linkerd, GCP LB | none found | Rely on readiness-gated warm-up |

Set the window to the measured time to warm.

## 8. Reference numbers

Use these to set expectations, not as promises. Ratios transfer better than absolute times.

| Source | Before | After | Main change |
|---|---|---|---|
| jeongil.dev | first request 1.2 to 1.4 s | 0.15 to 0.23 s | 40 warm-up requests, readiness-gated |
| Scienta, 0.25 CPU | p50 2.8 s | 0.8 s | 120 loopback warm-up requests |
| Xflow | first request about 10 s with 5xx | under 0.75 s | CPU 0.9 to 2–4 cores, Burstable QoS |
| blog.rasc.ch, Boot 4.1 | 6.4 s to first HTTP 200 | 2.6 s AOT cache, 1.0 s CRaC | JVM-level |
| AWS Lambda SnapStart with priming | p50 5,048 ms | 1,178 ms | checkpoint after warm-up |
| This skill's demo app, M4, Boot 3.4, JPA + Jackson | request 1: 57.6 ms (24x steady) | 2.5 ms (1.2x steady) | `WarmupRunner`, 100 loopback requests, about 0.25 s added to readiness |
| This skill's second demo, M4, Boot 3.5.7, Security + JPA + validation | request 1: 26 / 14 / 49 ms | 3.1 / 3.0 / 1.6 ms | one loopback round (3 requests), 0.17 s added to readiness |
| Same demo, Boot 4.1.3 | request 1: about 26 / 22 / 45 ms | 2.3 / 2.1 / 1.4 ms | 3 rounds (9 requests), about 0.14 s added to readiness |

Archive coverage, same app and image (Boot 3.2.4, JDK 21, `amazoncorretto:21.0.1-alpine`, classes in
the CDS dictionary). Use it to size the expected gain of training with requests, not as a latency promise:

| Training run | Classes | Archive | `coyote.http11` |
|---|---|---|---|
| `spring.context.exit=onRefresh` | 17,797 | 103.6 MB | 4 |
| warm-up over loopback | 18,846 | 109.3 MB | 26 |
| warm-up in-process | 18,295 | 106.2 MB | 4 |

Most of the gain over `onRefresh` comes from letting startup finish (reaching
`ApplicationReadyEvent`), not from the warm-up calls themselves.

JIT thresholds, measured with `java -XX:+PrintFlagsFinal -version` on JDK 21.0.10, to keep
expectations honest: `Tier3InvocationThreshold=200`, `Tier3MinInvocationThreshold=100`,
`Tier4InvocationThreshold=5000`, `Tier4CompileThreshold=15000`. A warm-up of a few dozen calls
leaves the application's own methods at level 0 (interpreted), so a short warm-up is **cache and
class-loading priming, not JIT warm-up**. Reaching level 3 for a method needs roughly 200 calls and
level 4 thousands — more than any startup budget allows.
