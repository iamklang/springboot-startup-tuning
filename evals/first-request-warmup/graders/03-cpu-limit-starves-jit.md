---
type: llm
focus: trace
weight: 1
---

answer.md explains that the 1-CPU limit starves JIT compilation during warm-up (or that the JVM sizes itself for one CPU) and recommends more CPU headroom during warm-up.
