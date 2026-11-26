# springboot-unit-testing

Claude skill: write unit tests for a Spring Boot project until JaCoCo line and branch coverage and the
raw PIT mutation score are all at least 95 percent, with tests that raise no SonarQube issue under the
Sonar way test rules, without gaming the numbers.

## Install

See the repository README: install as a plugin, or run `./install.sh` to link it into `~/.claude/skills/`.

| Path | What |
|---|---|
| `SKILL.md` | workflow: preflight, tooling, baseline, coverage, mutation, full gate, report; ground rules |
| `references/tooling.md` | versions, Maven/Gradle setup rules, gate semantics, exclusion policy, known errors |
| `references/test-patterns.md` | Boot 3/4 imports, test per layer, templates, mutator → assertion, forbidden tests |
| `references/mutation-loop.md` | per-class loop, acceptance filter, equivalent mutants, suspected bugs, CI |
| `references/sonar-test-rules.md` | SonarQube Java rules that run on test code (from sonar-java), how to comply |
| `scripts/quality_report.py` | reads `jacoco.xml` + `mutations.xml`: both scores, work list, allowlist |
| `scripts/sonar_test_lint.py` | lint for those rules without a SonarQube server; `--changed` = tests written since the snapshot |
| `scripts/guard.sh` | snapshot/check of `src/main`, build files and existing tests |
| `assets/pom-test-quality.xml` | Maven block: JaCoCo 0.8.16, PIT 1.30.2, Mockito agent, 95 percent gates |
| `assets/build-test-quality.gradle.kts` | the same for Gradle |
| `assets/lombok.config`, `assets/equivalent-mutants.tsv` | Lombok filter; allowlist format |

Verified (November 2026) on a demo shop service with Maven 3.9 + Boot 3.5.7 (JUnit 5.12) and Boot
4.1.3 (JUnit 6.0), Gradle 9.3 + Boot 3.5.7, JDK 21/24. In an eval against a baseline without the skill,
both reached the gates; the skill runs also refused to pin a seeded bug and configured enforcing gates.

`sonar_test_lint.py` follows the logic of the sonar-java checks (for example S5778 counts constructor
and helper calls and runs only when the exception type is given; S5853 joins only subjects without
method calls) but has no type information, so it is an approximation. A real SonarQube or SonarCloud
scan, when the project has one, is the authority.
