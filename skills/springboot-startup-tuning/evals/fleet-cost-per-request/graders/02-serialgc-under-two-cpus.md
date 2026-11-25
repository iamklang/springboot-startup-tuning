---
type: llm
focus: trace
weight: 1
---

answer.md says that a pod with a 1-CPU limit makes the JVM see one processor and pick SerialGC (or minimal GC/JIT threads), and recommends an explicit GC choice or at least 2 CPUs.
