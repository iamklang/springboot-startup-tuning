---
tags: [springboot-startup-tuning]
max_turns: 25
timeout_seconds: 900
allowed_tools: [Read, Glob, Grep, Skill, Write, Edit, Bash]
---

We're on Spring Boot 3.4.2 with Temurin 21, Maven, fat jar in a plain `FROM eclipse-temurin:21-jre` Dockerfile with `ENTRYPOINT ["java","-jar","app.jar"]`. Startup in prod is about 12s and most of it seems to be before Spring even logs anything. I keep hearing about CDS and Spring AOT. Rewrite our Dockerfile to use them and tell me what could break and how I'd check the archive is actually being used. Our prod profile is `prod` and Flyway runs at startup.
