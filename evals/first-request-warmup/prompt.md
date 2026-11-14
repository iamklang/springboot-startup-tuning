---
tags: [springboot-startup-tuning]
max_turns: 25
timeout_seconds: 900
allowed_tools: [Read, Glob, Grep, Skill, Write, Edit, Bash]
---

payment-api (spring boot 3.4, java 21, spring mvc + jpa + bean validation) starts in about 8s and the pods go Ready fine, but after every deploy or HPA scale-out the first requests to a new pod take 2-4 seconds and we get a burst of 504s from the istio gateway for a minute or two. steady state p99 is 40ms. k8s: requests.cpu 500m, limits.cpu 1, readiness on /actuator/health/readiness. how do we make the first calls fast? write your answer to answer.md
