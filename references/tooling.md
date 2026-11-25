# Tooling: JaCoCo and PIT in a Spring Boot build

Contents: versions · setup · what the gates measure · exclusions · Lombok and generated code ·
multi-module · problems seen in practice

## Versions (verified November 2026)

| Tool | Version | Notes |
|---|---|---|
| JaCoCo | 0.8.16 | Java 27 class files supported. Minimum per JDK: 21 → 0.8.11, 25 → 0.8.14. Spring Boot does not manage the version: pin it. |
| PIT (`pitest-maven`) | 1.30.2 | Java 25 fixes since 1.25.8 |
| `pitest-junit5-plugin` | 1.2.3 | worked with JUnit 6.0.3 (Boot 4.1.3) and 5.12 (Boot 3.5.7) in this skill's checks |
| Gradle plugin `info.solidsoft.pitest` | 1.19.0 | defaults to an old PIT: set `pitestVersion = "1.30.2"` |
| Mockito | managed by Boot (5.17 on 3.5.7, 5.23 on 4.1.3) | load as `-javaagent` on JDK 21+ |

Templates: `assets/pom-test-quality.xml`, `assets/build-test-quality.gradle.kts`. Both were run on a
Spring Boot project (Maven 3.9 with Boot 3.5.7 and 4.1.3, Gradle 9.3 with Boot 3.5.7); both gates fail
the build below 95 percent.

## Setup rules

- **`@{argLine}`, not `${argLine}`.** JaCoCo's `prepare-agent` writes the agent into the `argLine`
  property; Surefire's `@{...}` reads it late. Keep an empty `<argLine/>` property so the build still
  works with `-Djacoco.skip=true`. If the project has its own `argLine`, prepend `@{argLine}` to it.
- **Tests must fork.** `forkCount=0` records no coverage.
- **Mockito agent.** On JDK 21+ the inline mock maker self-attaches with a warning, and newer JDKs
  will refuse it. Pass `-javaagent:<mockito-core jar>` to Surefire and to PIT's `argLine`
  (Maven: `maven-dependency-plugin:properties` makes `${org.mockito:mockito-core:jar}` available;
  Gradle: a `mockitoAgent` configuration). `-XX:+EnableDynamicAgentLoading` only hides the warning.
- **Run PIT after `test-compile` in one Maven command**
  (`mvn test-compile org.pitest:pitest-maven:mutationCoverage`). A bare plugin invocation skips
  `dependency:properties`, the agent path stays unresolved and PIT dies with `MINION_DIED`.
- **Coverage gate on `verify`, report on `test`.** With the check on `test`, every `mvn test` fails
  while coverage is still being built up. Gradle: `jacocoTestCoverageVerification` is not part of
  `check` until wired in.
- **Reports must include XML.** `quality_report.py` reads `jacoco.xml` and `mutations.xml`;
  `timestampedReports=false` keeps the PIT path stable.

## What the gates measure

- **Coverage:** LINE and BRANCH covered ratio, BUNDLE level (the whole module), minimum 0.95.
  Class-level rules are stricter and noisier; add one only if the user asks.
- **Mutation:** PIT's `mutationThreshold` = detected / all mutants, where mutants without coverage
  count as survivors. That is the number to call "mutation score". Test strength (detected /
  covered mutants) ignores uncovered code and is always higher; report it, do not gate on it.
- **Mutators:** `DEFAULTS`. `STRONGER` adds a few; `ALL` adds noisy experimental mutators that make
  95 percent meaningless. State the group with every score.
- **Detected** includes KILLED, TIMED_OUT, MEMORY_ERROR and RUN_ERROR. A kill that is only a timeout
  is weak (often an infinite loop the mutant created); mention timeouts in the report.

## Exclusions: the easiest way to fake 95 percent

A 95 percent gate on code with a third excluded is a weak gate. Never add an exclusion to make a
number pass. The standard set below is added without asking (it is code nobody writes logic in) and
listed in the report; anything else is proposed with its reason and waits for the user.

Standard set:
- the `@SpringBootApplication` main class (`**/*Application.class`, PIT `**.*Application`)
- generated code: MapStruct `*MapperImpl`, OpenAPI generator output, jOOQ, QueryDSL `Q*`
  (their `@Generated` is often SOURCE retention, so JaCoCo does not filter them; exclude by path)
- Spring AOT output when the build runs `process-aot`: `**/*__*` (bean definitions,
  `__ApplicationContextInitializer`)

Not acceptable:
- whole packages (`service`, `controller`, `util`, `config`)
- wildcards like `**/*Config*`, `**/*Dto*`, `**/*Helper*` that sweep in logic
- `@Configuration` classes with conditions or logic
- adding an annotation whose name contains `Generated` to hand-written code (JaCoCo skips any
  annotation with that simple name)
- raising `excludedMethods`, lowering `coverage.minimum` / `mutationThreshold`

Records need no exclusion: JaCoCo filters their generated `equals`/`hashCode`/`toString` and accessors.

## Lombok and generated code

- `assets/lombok.config` (`lombok.addLombokGeneratedAnnotation = true`) makes JaCoCo and PIT skip
  Lombok-generated members. It is part of the standard setup when the project uses Lombok: add it,
  list it in the report, rebuild and confirm getters/setters no longer show as missed lines.
- Old-style `switch` on an enum leaves a synthetic branch JaCoCo cannot filter; switch expressions
  do not.

## Multi-module builds

- Run the loop per module; gates are per module.
- `report-aggregate` produces one combined report but is not a gate.
- Unit and integration coverage: `prepare-agent` + `prepare-agent-integration`, then `merge` and
  `report`/`check` on the merged file. Mutation runs stay on unit tests.

## Problems seen in practice

| Symptom | Cause | Fix |
|---|---|---|
| `Error opening zip file ... ${org.mockito:mockito-core:jar}`, `MINION_DIED` | PIT invoked without the build lifecycle | `mvn test-compile org.pitest:pitest-maven:mutationCoverage` |
| `mvn test` fails with "Coverage checks have not been met" | `check` bound to `test` | bind `check` to `verify` |
| Coverage 0 percent, `jacoco.exec` missing | `argLine` overridden without `@{argLine}`, or `forkCount=0` | prepend `@{argLine}` |
| PIT reports far fewer mutants than expected | `targetClasses` too narrow, or a partial run with `-DtargetClasses` | check the `partial` flag in `mutations.xml` |
| Mutants survive in code that a `@SpringBootTest` runs | context cached before the mutant was inserted | test that logic with plain unit tests |
| PIT very slow | full-context tests in the PIT run | exclude `*IT`, `*IntegrationTest`; run per class with `-DtargetTests` |
