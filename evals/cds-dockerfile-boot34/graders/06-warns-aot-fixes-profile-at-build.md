---
type: llm
focus: trace
weight: 1
---

answer.md warns that Spring AOT evaluates profiles and conditions at build time, so the prod profile must be active during AOT processing/training or the app will fail to start.
