---
tags: [springboot-startup-tuning]
max_turns: 25
timeout_seconds: 900
allowed_tools: [Read, Glob, Grep, Skill, Write, Edit, Bash]
---

we run catalog-api (spring boot 3.4, java 21, spring mvc, jpa on postgres, calls 3 downstream services with RestClient on apache httpclient 5) as 120 pods on EKS. each pod: requests cpu 1 / mem 2Gi, limits cpu 1 / mem 2Gi, JAVA_TOOL_OPTIONS is empty, hikari maximum-pool-size 30, HPA on 60% cpu between 80 and 200 pods. p99 is 450ms at peak though the endpoints are simple, and we got some OOMKilled last month. finance wants the cost down and product wants p99 under 200ms. what should we change and how do we prove each change worked? write your answer to answer.md
