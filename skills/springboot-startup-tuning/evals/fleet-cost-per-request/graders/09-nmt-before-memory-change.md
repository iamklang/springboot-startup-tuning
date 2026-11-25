---
type: llm
focus: trace
weight: 1
---

answer.md addresses the OOMKilled pods by measuring non-heap memory (Native Memory Tracking, or RSS vs heap) before setting heap percentage and memory limit, rather than only raising the limit.
