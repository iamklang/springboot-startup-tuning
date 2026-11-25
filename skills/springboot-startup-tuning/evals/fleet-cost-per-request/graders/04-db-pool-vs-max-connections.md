---
type: llm
focus: trace
weight: 1
---

answer.md computes or points out that 30 connections per pod times up to 200 pods (6,000) can exceed Postgres max_connections, and recommends a smaller pool sized per pod against the database limit.
