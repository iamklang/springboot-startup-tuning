---
tags: [springboot-startup-tuning]
max_turns: 25
timeout_seconds: 900
allowed_tools: [Read, Glob, Grep, Skill, Write, Edit, Bash]
---

our order-service (spring boot 3.3, java 21) takes ~45s to become Ready on EKS but like 6s on my macbook. deployment has requests.cpu 250m / limits.cpu 500m, no startupProbe, liveness on /actuator/health with initialDelaySeconds 30 and the pod keeps restarting during rollout. what cpu should we give it and how do I prove it? we have the image order-service:dev locally but docker desktop may not be running
