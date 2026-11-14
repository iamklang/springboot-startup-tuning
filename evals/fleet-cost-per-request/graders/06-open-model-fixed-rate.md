---
type: llm
focus: trace
weight: 1
---

answer.md requires measuring at a fixed arrival rate with an open-model tool (k6 constant/ramping-arrival-rate, wrk2 -R, vegeta, or the skill's measure_efficiency.sh / loadgen.py) rather than max throughput or a closed-model tool.
