---
type: llm
focus: trace
weight: 1
---

The Dockerfile is multi-stage and extracts the jar with java -Djarmode=tools ... extract before the training run.
