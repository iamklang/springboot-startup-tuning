---
type: llm
focus: trace
weight: 1
---

The Dockerfile training run uses -XX:ArchiveClassesAtExit together with -Dspring.context.exit=onRefresh (or an equivalent that makes the context start and exit).
