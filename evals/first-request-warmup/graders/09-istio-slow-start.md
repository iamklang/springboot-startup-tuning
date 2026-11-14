---
type: llm
focus: trace
weight: 1
---

answer.md recommends Istio slow start (DestinationRule loadBalancer warmup or warmupDurationSecs) and mentions that it needs round-robin or least-request balancing, not consistent hash or session affinity.
