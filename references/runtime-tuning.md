# Steady-state runtime tuning at scale

Read this in Step 6, when the question is throughput, tail latency or cost per request of a service
that is already running, usually with many replicas. The lever that matters at scale is CPU and
memory *per request at a fixed load*; tune that first, then size the fleet from it. Single flags
rarely move it as much as a JDK upgrade or removing allocation on the hot path.

## Contents
1. Measure: fixed-load, per-request units (R0)
2. JVM: let it see the right container, then pick a GC (R1)
3. Spring and libraries: explicit concurrency limits (R2)
4. Hot-path allocation and observability cost (R3)
5. Kubernetes: requests, limits, pod shape, autoscaling (R4)
6. Reference numbers

## 1. Measure: fixed-load, per-request units (R0)

Compare configurations at the **same arrival rate**, never by "max throughput". A closed-model tool
(ab, plain wrk, JMeter thread groups, k6 `vus`) stops sending while the server is slow, so it
under-reports stalls (coordinated omission). In one worked example a closed generator reported
p95 = 1 ms where the open one reported p95 ≥ 250 ms.

**Locally:** `scripts/measure_efficiency.sh` starts the app, waits for readiness, runs a warm-up
phase, then a measured phase with `scripts/loadgen.py`, which sends at a fixed rate and measures
latency from each request's scheduled time.

```bash
measure_efficiency.sh -r 200 -w 30 -d 60 -l baseline -o eff.csv \
  -R http://localhost:8080/actuator/health/readiness \
  -u 'http://localhost:8080/api/orders?customer=1' -u 'http://localhost:8080/api/products/42' \
  -- -jar app.jar --server.port=8080 --management.endpoints.web.exposure.include=health,metrics
```

- It reports achieved req/s, p50/p99/p99.9, **CPU ms per request** (process CPU time delta), **KB allocated per request** (Actuator `jvm.gc.memory.allocated` delta), RSS and heap used. Expose the `metrics` endpoint, or the allocation column is NA.
- Pick a rate around the real per-pod rate in production (RPS ÷ replicas), and well below the local machine's limit. A run with `dropped > 0` or errors is invalid; lower the rate.
- Warm up for at least 30 s at the same rate so JIT and caches settle; the measured phase excludes it.
- Use the same rate, durations, URLs and heap flags for every configuration. Repeat each configuration at least 3 times and compare medians with the keep rule from Step 4 (better than 5 percent and better than the control's spread, control measured in the same batch).
- loadgen.py is fine up to a few hundred req/s on a laptop. For staging and capacity tests use k6 `constant-arrival-rate` / `ramping-arrival-rate` (any `dropped_iterations` > 0 invalidates the run), `wrk2 -R` or `vegeta attack -rate`.

**Capacity per pod (staging):** one pod with production resources, step the arrival rate up and
hold each step several minutes after warm-up. Max RPS per pod is the last step whose p99 is inside
the SLO. Then soak at 60 to 80 percent of that for hours to catch leaks, unbounded caches and pool
exhaustion.

**In production,** compare releases in per-request units:

| Metric | PromQL (Micrometer and cAdvisor names) |
|---|---|
| p99 (needs `management.metrics.distribution.percentiles-histogram.http.server.requests=true`) | `histogram_quantile(0.99, sum by (le, uri) (rate(http_server_requests_seconds_bucket[5m])))` |
| CPU-seconds per request | `sum(rate(container_cpu_usage_seconds_total{container="app"}[5m])) / sum(rate(http_server_requests_seconds_count[5m]))` |
| Bytes allocated per request | `sum(rate(jvm_gc_memory_allocated_bytes_total[5m])) / sum(rate(http_server_requests_seconds_count[5m]))` |
| Throttle ratio | `rate(container_cpu_cfs_throttled_periods_total[5m]) / rate(container_cpu_cfs_periods_total[5m])` |
| CPU efficiency | usage ÷ `kube_pod_container_resource_requests{resource="cpu"}` |

**Profiling:** keep continuous CPU *and* wall-clock profiling on (JFR or async-profiler, about
100 Hz; JFR's default profile is documented at under 1 to 2 percent overhead; verify with a canary).
CPU profiles do not show time spent waiting on locks or I/O, so Hikari starvation and N+1 queries
appear only in wall-clock profiles. Turn on allocation and lock profiling for short windows only.

## 2. JVM: let it see the right container, then pick a GC (R1)

Defaults in a container waste most of it: max heap is 25 percent of the memory limit, and below 2
CPUs or about 1.8 GB the JVM silently picks SerialGC (G1 becomes the default everywhere only in
JDK 27, JEP 523).

- **Heap:** `-XX:MaxRAMPercentage=75` and `-XX:InitialRAMPercentage=75` on pods whose memory request equals the limit. Drop to 60 to 65 percent for many threads, heavy Netty direct buffers or large metaspace. Do not guess the non-heap part: run with `-XX:NativeMemoryTracking=summary` in staging, `jcmd <pid> VM.native_memory baseline`, apply real load, then `jcmd <pid> VM.native_memory summary.diff`. RSS can exceed what NMT sees (glibc arenas); one case found about 300 MiB more. Memory limit = max heap + measured non-heap + margin. Cap `-XX:MaxMetaspaceSize` and `-XX:MaxDirectMemorySize` so a leak fails inside the JVM with a clear error, not as an OOMKill.
- **Processors:** since JDK-8281181 (JDK 19, backported to 17.0.5 and 11.0.17) the JVM sizes itself from the CPU *limit* and ignores the request. With no CPU limit it sees every core of the node and oversizes GC, JIT and common-pool threads. Set `-XX:ActiveProcessorCount` to the CPU request rounded up (at least 2) whenever there is no limit.
- **GC:** G1 by default. ParallelGC for pods of 2 vCPU or less that want throughput over pause times. Generational ZGC (default ZGC mode since JDK 23) when p99.9 matters and there is 20 to 30 percent CPU headroom and roughly double the heap G1 would need; at very high allocation rates or with too little CPU it stalls and G1 wins. Measure with the fixed-load run, never assume.
- **JDK upgrade first:** moving to the current LTS (JDK 25) is the cheapest large win at fleet scale (Netflix: about 20 percent less CPU from 8 to 17 with no code change). Re-measure at the same load after the upgrade before any flag tuning.
- **Compact object headers (JDK 25, JEP 519):** `-XX:+UseCompactObjectHeaders` shrinks every object header; the JEP reports about 22 percent less heap and 8 percent less CPU on SPECjbb2015. It becomes the default in JDK 27 (JEP 534). A/B it per service; code that locks heavily on many objects can get slower.
- **Not by default:** `-XX:TieredStopAtLevel=1` (lower peak throughput), `AlwaysPreTouch`, string deduplication and transparent huge pages have no recent measured wins; treat them as A/B candidates only.

## 3. Spring and libraries: explicit concurrency limits (R2)

At scale the failure is usually a hidden or missing limit, not slow code.

- **Database pool:** small pools are faster. HikariCP's guideline is about `(cores × 2) + effective spindles` per database, *summed over all pods*: pods × `maximum-pool-size` must stay below the database's `max_connections` with room for other clients. In the Oracle demo on the HikariCP wiki, cutting the pool from 2048 to 96 connections took response time from about 100 ms to about 2 ms. Short `connection-timeout` (for example 2 to 5 s) so starvation fails fast and shows up in metrics.
- **Hibernate:** `spring.jpa.open-in-view=false`; `spring.datasource.hikari.auto-commit=false`, so the connection is taken only when the first statement runs (Spring Boot then sets `hibernate.connection.provider_disables_autocommit=true` itself when not on JTA; `readOnly=true` or a custom isolation level still acquires eagerly; Boot 4.1+ also offers `spring.datasource.connection-fetch=lazy`, which wraps the pool in `LazyConnectionDataSourceProxy` so any code path takes a physical connection only at its first statement); `hibernate.jdbc.batch_size` with `order_inserts` / `order_updates`; SEQUENCE ids instead of IDENTITY (IDENTITY disables insert batching); MySQL `rewriteBatchedStatements=true` (about 9.6× faster batches in one measurement); never call remote services inside `@Transactional`, because the connection is held for the whole call. Find N+1 queries with Hibernate statistics or a datasource proxy in tests, not in production logs.
- **Outbound HTTP:** set connect, read and pool-acquire timeouts explicitly on every client. Apache HttpClient 5, the first choice of Spring's RestClient auto-detection, defaults to 5 connections per route and 25 in total, a silent cap under load. Spring Boot has no pool-size property (`spring.http.clients.*` in Boot 4, `spring.http.client.*` in 3.5, covers timeouts, redirects, SSL and the factory only), so configure the connection manager in a `ClientHttpRequestFactory` or HttpClient bean. Size max-per-route from the call rate and latency of each downstream (Little's law: concurrent ≈ rate × latency). Add bulkheads and circuit breakers (Resilience4j) for downstreams that can slow down.
- **Threads:** Tomcat's default 200 worker threads is a concurrency limit. With virtual threads (`spring.threads.virtual.enabled=true`) that limit disappears and the database pool and HTTP client pools become the only ones, so set those first. Enable virtual threads only on JDK 24+ (in practice 25 LTS), because before JEP 491 blocking inside `synchronized` pinned carrier threads (Netflix hit deadlock-like stalls on JDK 21). In one benchmark they did not raise throughput and produced 23 to 30 percent errors at extreme concurrency from pool exhaustion. MVC with virtual threads matched or beat WebFlux in most comparisons of a recent Boot 4 benchmark; do not rewrite to WebFlux for performance alone.
- **Serialization:** Jackson is often the top hot method. Return DTOs that contain only the needed fields, avoid serializing entities, and keep payloads small. Boot 4 moves to Jackson 3.
- **Caching:** cache only what a profile or the query log shows as hot; bound every cache (Caffeine `maximumSize` / `expireAfterWrite`) because an unbounded cache becomes a memory leak at scale.

## 4. Hot-path allocation and observability cost (R3)

Allocation per request drives GC CPU, heap size and tail latency together. LinkedIn cut allocation
from about 12 GB/s to 1 to 2 GB/s on one service (with a JDK and GC upgrade), and p999 fell 55
percent and capacity rose 28 percent. Typical sources in Spring services, found with an allocation
profile:

- Logging: messages built even when the level is off (use parameterized logging or a guard), full payloads logged, location info (`%L`, `%M`, caller data) in the pattern, synchronous appenders that block on a slow sink.
- `Optional`, boxing and streams on very hot paths; duplicated strings; regex compiled per call; exceptions used for control flow; large intermediate JSON trees.
- Metrics and tracing: keep tag cardinality bounded (never user IDs, raw paths or arbitrary method names as tags; past CVEs exploited unbounded tags), keep trace sampling at the default 10 percent or lower and use tail sampling in the collector.

## 5. Kubernetes: requests, limits, pod shape, autoscaling (R4)

- **CPU:** set the request from measured usage (p95 to p99 plus 20 to 30 percent, excluding warm-up spikes) and, for latency-sensitive services, no CPU limit, with `-XX:ActiveProcessorCount` set. A UIUC and IBM study (microservice benchmark, not JVM) found a limit on one pod raised tail latency about 5× and removing limits saved about 38 percent CPU. If a limit is required, use whole cores and watch the throttle ratio (above about 0.1 under normal load means the limit is too low).
- **Memory:** request equals limit, sized from heap plus measured non-heap. Do not let a recommender size heap from memory usage when `Xms` equals `Xmx`: usage is flat at the configured size and the tool just echoes it. Size heap from the live set after GC (GC log or JFR) instead.
- **Pod shape:** fewer, wider pods (2 to 4 vCPU) beat many 1-vCPU pods for the JVM: per-pod overhead (JIT, metaspace, sidecar) is paid fewer times and GC and JIT get real parallelism. Keep at least 2 to 3 replicas across zones.
- **Autoscaling:** HPA on CPU misreads JIT warm-up as load and adds more cold pods. Scale primarily on requests per second or in-flight requests per pod (Prometheus adapter or KEDA), with CPU as a fallback and a scale-up stabilization window. The controller's defaults (CPU initialization period 5 min, initial readiness delay 30 s, scale-down stabilization 5 min) are set on kube-controller-manager and usually cannot be changed on managed clusters. Use VPA or KRR in recommendation mode and apply through GitOps. With Karpenter consolidation, every eviction is a cold start; protect with PodDisruptionBudgets and disruption budgets, and combine with the warm-up from Step 5.
- **Sidecars and architecture:** an Istio sidecar requests 100m CPU and 128 Mi per pod by default; ambient mode moves this to a per-node proxy. ARM (Graviton) can lower cost for Java, but measured fleet savings are around 10 to 20 percent and needed JVM tuning; vendor "up to 40 percent" figures are marketing. GraalVM native lowers RSS (one PetClinic run: 641 MB vs 1,029 MB) at the cost of build time and sometimes peak throughput.
- **Efficiency target:** Datadog found most container workloads use less than 25 percent of requested CPU. Track usage ÷ request per service and lower requests after the per-request numbers improve; otherwise the savings never reach the bill.

## 6. Reference numbers

Use for expectations, not promises; workloads differ.

| Source | Change | Result |
|---|---|---|
| Netflix | JDK 8 → 17, no code change | about 20% less CPU |
| Netflix | G1 → generational ZGC (JDK 21) | lower average and p99 latency at the same CPU target |
| LinkedIn FollowFeedStorage | JDK 11 + CMS → JDK 21 + generational ZGC, plus allocation cuts | p999 100 → 45 ms, heap −34%, capacity +28% |
| JEP 519 | compact object headers | SPECjbb2015 heap −22%, CPU −8% |
| HikariCP wiki | pool 2048 → 96 connections | response time about 100 → 2 ms |
| MySQL batching | `rewriteBatchedStatements=true` | batch time 1072 → 112 ms |
| UIUC and IBM (non-JVM) | CPU limit on one pod / limits removed | tail latency about 5× / CPU −38% |
| Twitter (Dan Luu) | thread pools sized to the real CPU quota | 1.5 to 2× capacity |
| This skill's demo app, M4, 200 req/s | `open-in-view=false` | No measurable effect: CPU 1.60 vs 1.55 ms/req (median of 3), inside the control's 0.19 spread. A single run had shown −12%, which is why every comparison needs repeats |
