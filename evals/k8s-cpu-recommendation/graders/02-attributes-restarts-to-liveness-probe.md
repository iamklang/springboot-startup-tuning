---
type: llm
focus: trace
weight: 1
---

answer.md attributes the restart loop to the liveness probe (initialDelaySeconds 30) firing before the ~45 s startup completes.
