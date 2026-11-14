# Kubernetes CPU and probe guidance for Spring Boot startup

## Why startup is CPU-bound

A Spring Boot start is a burst of single-purpose CPU work: class loading, JIT compilation of the
framework's own code, reflection, bean wiring. On a laptop with 8 free cores it takes 3 seconds;
under a CFS quota of 0.5 core the same work is spread across 10 times as many 100 ms periods and
the pod also pays throttling penalties. Memory limits rarely matter for the start itself.

The JVM also rounds the quota to an integer processor count. At `limits.cpu: 500m` or `1`, it sees
one CPU: one C1/C2 compiler thread, SerialGC, and `ForkJoinPool.commonPool` of size 1. That is why
going from 1 to 2 cores often halves startup while 2 to 4 gains only 10 to 15 percent.

## Deciding the numbers

Inputs from the sweep (`cpu_sweep.sh`):
- `cpu_seconds`: CPU consumed by one start. Startup at limit L cannot be faster than `cpu_seconds / L`.
- The knee: smallest limit where the next step improves less than ~15 percent.

Three strategies, pick one and say why:

| Strategy | When | Manifest shape |
|---|---|---|
| Request low, no CPU limit | Default recommendation. Node has slack; the app bursts at start, then idles. Kubernetes only guarantees `requests`; the absence of a limit removes throttling entirely | `requests.cpu: 250m` to `500m`, no `limits.cpu`, keep a memory limit |
| Limit at the knee | Platform policy requires limits, or noisy neighbours are a real concern | `requests.cpu` = steady-state usage, `limits.cpu` = knee (often `2`) |
| Reduce the CPU needed instead | Limit must stay under 1 core (cost, multi-tenant) | Apply tier 3 (CDS/AOT) so `cpu_seconds` drops; re-run the sweep; often 1 core becomes acceptable |

If the cluster is on Kubernetes 1.33+ (in-place pod resize beta), a fourth option is a high limit at
start and a lower limit after `Ready`, resized without restart. It needs a controller or a post-start
hook that patches the pod, so recommend it only to teams who will build that.

Also mention `-XX:ActiveProcessorCount=2` for pods stuck under 2 cores: it restores JVM
parallelism, startup usually improves, throttling increases. Measure before recommending.

## Before trusting a local sweep

- **Image architecture.** `docker image inspect <img> --format '{{.Os}}/{{.Architecture}}'` must match the Docker host. An `amd64` image on Apple Silicon runs under emulation and is several times slower; rebuild with `--platform linux/arm64` for the sweep or sweep on the cluster. `cpu_sweep.sh` checks this and stops.
- **Docker VM size.** On Docker Desktop, `--cpus 4` means nothing if the VM has 2 CPUs (`docker info --format '{{.NCPU}}'`).
- **Per-core speed.** One laptop performance core is faster than one cloud vCPU (often a hyperthread). Trust the ratios between limits, not the absolute seconds.
- **Waiting versus computing.** If cluster startup is far above `cpu_seconds / limit`, the rest is I/O wait inside the cluster: DB or Kafka connections, Flyway, Config Server or Vault, DNS, cloud credential lookups (IRSA, instance metadata hop limit). Check `/actuator/startup` from a pod; raising CPU will not shorten these.

## Measuring in the cluster

Clone the deployment once per CPU value in a non-production namespace, with labels that do not match the Service selector (so test pods get no traffic) and without the liveness probe (so nothing kills a slow start). A Service selector only stops HTTP traffic: clones still run Flyway, `@Scheduled` jobs, and consume from Kafka, SQS or RabbitMQ. Turn listeners off for the clones (for example `spring.kafka.listener.auto-startup=false`) or point them at a test topic, and run one clone at a time so they do not compete for node CPU. Read the `Started` line, the Ready transition time, and `usage_usec` / `throttled_usec` from `/sys/fs/cgroup/cpu.stat` inside each pod. If GitOps (Argo CD, Flux) manages the namespace, it may prune the clones; use a namespace it does not manage.

## Probes

Size the startup probe from the measurement at the **chosen** limit, not from the laptop:

```yaml
startupProbe:
  httpGet:
    path: /actuator/health/liveness   # or /actuator/health if liveness group is not enabled
    port: 8080
  periodSeconds: 5
  failureThreshold: <ceil(measured_startup_s * 1.5 / 5)>   # e.g. 20 s startup -> 6
readinessProbe:
  httpGet:
    path: /actuator/health/readiness
    port: 8080
  periodSeconds: 10
livenessProbe:
  httpGet:
    path: /actuator/health/liveness
    port: 8080
  periodSeconds: 10
  failureThreshold: 3
```

While `startupProbe` is failing, liveness and readiness are not evaluated, so a slow start never
triggers a liveness kill. Without a startup probe, set `initialDelaySeconds` on liveness to at
least the measured startup times 1.5; this is the weaker option because it also delays detection
of a genuinely stuck pod.

Enable the probe groups if the app does not have them:

```yaml
management:
  endpoint:
    health:
      probes:
        enabled: true
  health:
    livenessstate.enabled: true
    readinessstate.enabled: true
```

## Verifying after deploy

Throttling ratio in the first minutes (Prometheus, cAdvisor metrics):

```
sum(rate(container_cpu_cfs_throttled_periods_total{container="app"}[1m])) by (pod)
/
sum(rate(container_cpu_cfs_periods_total{container="app"}[1m])) by (pod)
```

Above about 0.25 during startup means the limit is too low for the start. A limit that shows
zero throttling at steady state but heavy throttling during the first minute is exactly the
burst-at-start pattern the first strategy solves.

Time to Ready per pod:

```bash
kubectl get pod -l app=<name> -o jsonpath='{range .items[*]}{.metadata.name}{" created="}{.metadata.creationTimestamp}{" ready="}{.status.conditions[?(@.type=="Ready")].lastTransitionTime}{"\n"}{end}'
```

Compare with the laptop measurement at the same limit; a gap larger than the laptop-to-node CPU
difference points at image pull time, init containers, or an external dependency that is slow
from inside the cluster.

JVM view of the quota (run inside the pod):

```bash
kubectl exec <pod> -- java -XX:+PrintFlagsFinal -version | grep -E 'ActiveProcessorCount|CICompilerCount|UseSerialGC|UseG1GC|MaxHeapSize'
```
