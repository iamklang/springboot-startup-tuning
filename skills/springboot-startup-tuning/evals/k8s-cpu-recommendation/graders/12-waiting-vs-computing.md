---
type: llm
focus: trace
weight: 1
---

answer.md notes that part of the 45 s may be I/O waiting inside the cluster (database, Flyway, remote config, DNS, credential lookups) that more CPU will not fix, and says how to tell.
