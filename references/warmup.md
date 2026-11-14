# First-request latency and warm-up

Read this in Step 5, when the problem is how slow the *first requests* are after the app is
already up, not how long it takes to start. Symptoms: p99 spikes or 5xx right after every deploy
or scale-out, a pod passes its readiness probe and then times out, HPA adds pods that are slow and
burn CPU, or the user says "request แรกช้า" or "หลัง deploy ช้าอยู่พักหนึ่ง".

## Contents
1. Where the time goes after "Started"
2. Measure (W0, W1)
3. Spring and library fixes (W2)
4. In-app warm-up gated by readiness (W3)
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

- `spring.mvc.servlet.load-on-startup=1`: the DispatcherServlet initialises at startup instead of on the first request.
- Global lazy initialization moves bean creation into the first request. If `spring.main.lazy-initialization=true` is on in production, this is often the whole problem.
- Hikari: `minimum-idle` equal to `maximum-pool-size`, so the pool is full before traffic instead of opening connections under the first requests.
- Kafka producers: call `partitionsFor(topic)` in the warm-up runner so the first `send()` does not wait for metadata. Do not send messages.
- Clients to other services (RestClient, WebClient, Feign): one call to a health or cheap read endpoint in warm-up opens the TLS session and resolves DNS. Only when the user agrees, because it touches another service.
- Reactor Netty (WebFlux, WebClient): `HttpClient.warmup().block()` and `HttpServer.warmup()` preload the event loops and native transport.
- Hibernate: the query plan cache fills on first use. Warm-up requests that run the main queries fill it.

## 4. In-app warm-up gated by readiness (W3)

### How it works

Spring Boot runs startup in this order:
1. It creates the beans and starts the embedded web server, which already accepts connections.
2. It runs every `ApplicationRunner` and `CommandLineRunner`. Readiness still answers 503.
3. When all runners have returned, it publishes `ReadinessState.ACCEPTING_TRAFFIC`, readiness answers 200, and Kubernetes starts routing traffic.

`assets/WarmupRunner.java` runs in step 2. It calls the app's own endpoints over HTTP on
`127.0.0.1:<local.server.port>`, so every request goes through filters, security, MVC, Jackson,
validation and JPA while no user traffic can arrive. Liveness is already CORRECT, so the pod is not
restarted during warm-up. Calling service methods directly would skip the web layer and warm much
less.

### Install

1. Copy the template into the main class's package or a sub-package, so component scan finds it:
   `cp <skill>/assets/WarmupRunner.java src/main/java/<pkg path>/`
2. Replace `package CHANGE_ME;` with the real package.
3. Add the properties to the production configuration (or the profile used in the cluster):

```yaml
app:
  warmup:
    enabled: true
    paths: /api/orders?customer=1,/api/products/42
    iterations: 50
    max-duration: 20s
management:
  endpoint.health.probes.enabled: true   # exposes /actuator/health/readiness and /liveness
```

4. The runner is off when `enabled` is absent. If it is turned on in the main `application.yml`, set `app.warmup.enabled: false` in the test profile so tests do not slow down.

| Property | Meaning |
|---|---|
| `enabled` | Turns the runner on. Missing means off. |
| `paths` | Comma-separated paths, query strings allowed, no commas inside a path. |
| `iterations` | Rounds. Each round calls every path once, so total requests = iterations × number of paths (100 in the example). 40 to 120 requests per path is the range in the case studies. |
| `max-duration` | Hard time budget. The runner stops when it runs out, even mid-round. It must fit inside the startupProbe window. |

### Choosing paths

- Pick 2 to 5 of the most-called endpoints: one that queries the database, one that serializes a large DTO, one with validation. Use the same URLs as the `-u` arguments of `measure_first_requests.sh`, so the measurement shows the effect.
- Only idempotent reads. The template sends GET only. No path may write data, send a message, or call another service. Confirm this with the user for every path, and refuse payment, order-creation or notification endpoints.
- Use IDs that exist in every environment. A 404 returns before the service and repository code run, so it warms only part of the path.
- Endpoints behind authentication answer 401 to the template, which warms only the filter chain. Choose public read endpoints, or add a warm-up credential header that the app reads from its own configuration (for example a client-credentials token for a local-only scope). Never disable security for warm-up, and never put a real user's token in config.
- To warm a validated POST, extend the template to send a deliberately invalid body that Bean Validation rejects with 400. First check that nothing writes or calls out before validation runs (no `@Transactional` work in a filter or an interceptor ahead of the controller).

### Verify

- The startup log ends the warm-up with one line:
  `warm-up done: 100 requests, 0 failed, 0 non-2xx, 850 ms`
  `failed` counts HTTP 5xx and connection errors. `non-2xx` counts every other non-2xx answer, usually 401 or 404 from a wrong path, a missing ID or missing auth. Both should be 0. The first problem of each kind is logged as a warning with its path.
- Measure with `measure_first_requests.sh` twice, `--app.warmup.enabled=false` then `true`, with the same `-u` URLs, R and N. Compare request 1, the mean of requests 2 to 10 and first/steady.
- Measure the `ready` time with `measure_startup.sh -u .../actuator/health/readiness` too. Warm-up moves time from the first user requests to before readiness, and the report must show both sides. On the demo app request 1 fell from 57.6 ms to 2.5 ms and readiness came about 0.25 s later.
- Size the startupProbe for startup plus `max-duration` (section 5).

### Rules to tell the user and to check in review

- Never fail startup because warm-up failed. The template logs and continues.
- The template has no `@Order`, so it runs after the app's other runners (data loading, cache priming). Keep it that way unless the user has a reason.
- Exclude warm-up requests from business metrics and access logs where it matters. The template sends `X-Warmup: true` on every request.
- If the warm-up must run asynchronously, the app has to keep readiness down itself: publish `AvailabilityChangeEvent.publish(ctx, ReadinessState.REFUSING_TRAFFIC)` before and `ACCEPTING_TRAFFIC` after. Prefer the synchronous runner.
- Under Spring AOT, set `management.endpoint.health.probes.enabled=true` explicitly, because cloud-platform detection is fixed at build time.
- With a low CPU limit the warm-up itself is throttled and helps little. Fix CPU headroom (W5) first or together.

Expect this to remove the one-time costs (request 1 drops close to the 2-to-10 mean) but not the
whole C2 warm-up, which needs far more calls than a short warm-up sends. Slow start (W7) or CPU
headroom (W5) covers the rest.

## 5. Probes, CPU headroom and rollout on Kubernetes (W4, W5)

- **startupProbe** must cover startup plus warm-up, measured at the CPU *request*, not the limit, because a busy node gives only the request. `failureThreshold = ceil((startup + warm-up) * 1.5 / periodSeconds)`. Point it and the readiness probe at `/actuator/health/readiness`. Keep liveness on `/actuator/health/liveness` and never put the warm-up state in the liveness group.
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

- The default training recipe in `cds-aot.md` (`-Dspring.context.exit=onRefresh`) improves startup only. It records no method profiles, so it does nothing for warm-up.
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
