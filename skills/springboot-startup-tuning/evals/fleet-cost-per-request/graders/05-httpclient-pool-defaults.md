---
type: llm
focus: trace
weight: 1
---

answer.md points out that Apache HttpClient 5 defaults to 5 connections per route (25 total) and recommends setting max-per-route, total and timeouts for the three downstreams.
