---
type: llm
focus: trace
weight: 1
---

answer.md explains that a 500m CPU limit makes the JVM see a single processor (one JIT compiler thread, SerialGC) and that this is why startup is many times slower than on the laptop.
