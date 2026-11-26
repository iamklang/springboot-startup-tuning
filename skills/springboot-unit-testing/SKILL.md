---
name: springboot-unit-testing
description: Write unit tests for a Java Spring Boot project until JaCoCo line and branch coverage and the raw PIT mutation score are all at least 95 percent, honestly, with tests that raise no SonarQube issue (Sonar way test rules). Sets up JaCoCo and PIT (Maven or Gradle, Boot 3.x and 4.x), writes fast plain JUnit 5/6 + Mockito + AssertJ tests and slice tests per layer, iterates on uncovered lines and surviving mutants from the reports, checks the tests against SonarQube's Java test rules with a bundled lint, and guards against gaming (no production-code edits, no unapproved exclusions, no assertion-free tests). Use whenever the user asks to write, add or improve unit tests for a Spring Boot or Java service, raise code coverage, set up JaCoCo, mutation testing or PIT, kill surviving mutants, reach a coverage target, or make tests pass SonarQube / Sonar rules, even if they only say 'เขียน unit test', 'เพิ่ม coverage', 'test ให้ครบ' or 'mutation score'.
---

# Spring Boot unit tests to 95 percent coverage and mutation score

The goal is tests that would catch a real regression. The two numbers are the evidence, not the goal:
coverage shows what the tests execute, the mutation score shows what they would notice if it broke.
Line coverage above 95 percent is routine; a mutation score above 95 percent needs a test at every
boundary, every returned value asserted exactly and every side effect observed. The tests are also
code that SonarQube scans, so they follow its test rules from the first line written.

**Done means all of these, on the whole module:**

| Gate | Threshold | Checked by |
|---|---|---|
| Line coverage | ≥ 95 % | JaCoCo `check` in `mvn verify` |
| Branch coverage | ≥ 95 % | JaCoCo `check` in `mvn verify` |
| Mutation score, raw, `DEFAULTS` mutators | ≥ 95 % | PIT `mutationThreshold`, `quality_report.py` |
| SonarQube test rules (Sonar way) | 0 issues in tests written or changed here | `sonar_test_lint.py --changed` (approximation) |

The mutation gate is the raw score. Approved equivalent mutants are shown next to it but do not make
a raw 93 percent pass; only the user can decide that (see the last section).

Read these when the step says so:
- `references/tooling.md`: JaCoCo/PIT versions, setup rules, what may be excluded, known errors
- `references/test-patterns.md`: imports per Boot version, which test per layer, templates,
  mutator → assertion table, hard survivors, forbidden test shapes
- `references/mutation-loop.md`: the round-by-round loop, acceptance filter, equivalent mutants,
  suspected bugs, CI
- `references/sonar-test-rules.md`: the SonarQube rules that apply to test code and how to comply;
  read it before writing the first test
- `scripts/quality_report.py`: one view of both reports plus a work list of missed lines and live mutants
- `scripts/sonar_test_lint.py`: checks test sources against those rules (`FILE:LINE RULE message`)
- `scripts/guard.sh`: snapshot and check of production code, build files and existing tests

## Ground rules

1. **Production code does not change.** Not to make a test pass, not to remove dead code, not to
   add a seam. If the code needs a change to be testable, or looks wrong, report it.
2. **Gate settings do not move without the user.** Thresholds, mutator group, `excludes`,
   `excludedClasses`, `excludedMethods` and `test-quality/equivalent-mutants.tsv` change only with
   the user's explicit approval, one entry at a time with its reason.
3. **Expected values come from the specification, not from running the code.** Read the Javadoc,
   names, messages and any requirement the user gave. When the code clearly contradicts them, do not
   test that behaviour; report a suspected bug (see `mutation-loop.md`). When the specification is
   only silent or ambiguous, test the current behaviour under a name that states it, and list it as
   a question for the user.
4. **Every test asserts behaviour.** No assertion-free tests, no `isNotNull()` where the value is
   known, no getter/setter tests, no `@SpringBootTest` just to execute code. Full list:
   `test-patterns.md` section 6.
5. **Report raw numbers.** The raw PIT score is always shown, next to any score with approved
   equivalent mutants. The raw score is the one that passes or fails.
6. **Tests follow SonarQube's test rules.** Write them as `sonar-test-rules.md` says (package-private
   JUnit 5/6 classes and methods, static imports for AssertJ and Mockito, `@Mock` fields, one call in
   an exception lambda, chained and dedicated AssertJ assertions, similar tests as one
   `@ParameterizedTest`, no `Thread.sleep`, no system clock, no random data). Never silence a rule
   with `// NOSONAR` or `@SuppressWarnings("java:S...")`; if a rule truly cannot be met, list it
   under "Needs a human" with the reason.

## Step 0: Preflight

- Build tool and wrapper, Spring Boot version (3.0–3.3, 3.4/3.5 or 4.x), Java version, JUnit
  version, Lombok, MapStruct/OpenAPI/jOOQ generators, Spring AOT (`process-aot`), modules.
- Existing tests: run them (`mvn -q test` / `./gradlew test`). If they fail, stop and report; do not
  build on a red baseline.
- Existing JaCoCo/PIT configuration, `argLine` settings, and any existing exclusions (list them in
  the final report; do not widen them).
- `git status`: work on a clean tree, or note the uncommitted files so they are not mistaken for
  changes made here.
- Scope: whole module by default. If the user named packages or classes, the gates still run on the
  module, and the report shows the scoped numbers separately (`quality_report.py --class`).

## Step 1: Tooling

Add the block from `assets/pom-test-quality.xml` or `assets/build-test-quality.gradle.kts`
(read `references/tooling.md` first). This is part of the request, so do it without asking and list
it in the report. It contains one exclusion, the `@SpringBootApplication` class. These also belong to
the standard setup, without asking, when the project has them:

- Lombok: add `assets/lombok.config` at the project root, rebuild, confirm generated accessors left
  the report.
- Generated sources (MapStruct `*Impl`, OpenAPI, jOOQ, QueryDSL) or Spring AOT output: path
  excludes for exactly those generated classes, as in `tooling.md`.
- Boot 4 slice tests: the matching test starters (`spring-boot-starter-webmvc-test`, ...), test scope.

Any other exclusion, and any change to thresholds or mutators, needs the user's approval first.

Then `mvn -q test` (or `./gradlew test`) must pass and produce `jacoco.xml`. Then snapshot:

```bash
bash <skill>/scripts/guard.sh snapshot      # from the project root
```

## Step 2: Baseline

```bash
mvn -q test && mvn -q test-compile org.pitest:pitest-maven:mutationCoverage -Dmutation.minimum=0
python3 <skill>/scripts/quality_report.py
```

Gradle: `./gradlew test pitest -PpitThreshold=0`. On a large module, take the PIT baseline per
package instead. Order the classes: business logic with most missed lines and live mutants first,
then controllers and clients, then the rest.

## Step 3: Coverage

Per class, read the code and its specification, then write tests by the layer table in
`test-patterns.md` section 2, using the imports for this Boot version (section 1). Rerun `mvn -q test`
and `quality_report.py --mutation 0` until the class has no missed lines or branches that valid input
can reach. Write assertions strong enough for mutation testing from the start: exact values,
boundaries (below, at, above), both outcomes of each condition, effects of void calls. Run
`sonar_test_lint.py <TestFile>` on each new test class before moving on; fixing a rule then is cheaper than
after the mutation round.

## Step 4: Mutation

Per class, run PIT for that class only and work the list of live mutants in batches of 5 to 10
(`mutation-loop.md`, "One round"; `test-patterns.md` sections 4 and 5 for the kill per mutator). At
most 3 attempts per mutant. Every new test passes the acceptance filter: compiles, passes, passes 5
runs in a row, adds coverage or kills a mutant, has no `sonar_test_lint.py` issue, follows the ground
rules.

A mutant that no valid input can kill is a candidate equivalent mutant: write down why, and go on.
Code that the specification says behaves differently is a suspected bug: stop on it and go on with
the rest.

## Step 5: Full gate

```bash
mvn verify                                                     # tests + coverage gate
mvn test-compile org.pitest:pitest-maven:mutationCoverage     # whole module, 95 percent gate
python3 <skill>/scripts/quality_report.py                       # raw mutation score >= 95
python3 <skill>/scripts/sonar_test_lint.py --changed             # 0 issues in the tests written here
python3 <skill>/scripts/sonar_test_lint.py --quiet               # all tests, for the report
bash <skill>/scripts/guard.sh check
for i in 1 2 3; do mvn -q test || break; done                  # the whole suite stays green
```

Gradle: `./gradlew check pitest`, then the same scripts. Issues in tests that existed before this
task are listed in the report and fixed only if the user agrees (they are not this task's tests); an
existing test you had to change counts as yours and must be clean. If the project has a real SonarQube or
SonarCloud scan, run it too and treat its result as the authority over the lint.

`guard.sh check` must say OK. A changed existing test is listed for review: say in the report what
changed and why (for example a compile fix), and never weaken an existing assertion.

If a gate is still below 95 percent, do not loosen anything. Report what is left and why. The task
is not done while the raw mutation score is below 95 percent: keep killing mutants while there are
killable ones left, and stop only when every remaining survivor is a candidate equivalent, a
suspected bug or "needs a human" after 3 attempts.

## Step 6: Report

Reply in the user's language. Include:

| | Before | After |
|---|---|---|
| Line coverage | | |
| Branch coverage | | |
| Mutation score (raw, `DEFAULTS` mutators) | | |
| Test strength | | |
| Sonar test-rule issues (`sonar_test_lint.py`, new tests / existing tests) | | |
| Tests (count, run time) | | |

- Gates: pass or fail for each of the four, with the commands to rerun them. Say that the Sonar
  result comes from the bundled lint unless a real scan was run.
- Build changes: tooling block, `lombok.config`, test starters, every exclusion with its reason.
- **Suspected bugs:** file and line, expected (with the source of that expectation) and actual.
- **Candidate equivalent mutants** for the user to approve or reject, each with its reason; with the
  score that approving all of them would give. Only after approval do they go into
  `test-quality/equivalent-mutants.tsv`.
- **Needs a human:** mutants or lines still alive after 3 attempts, with what was tried, and code
  that is hard to test because of its design (a refactoring suggestion, not a change).
- Guard result and anything it listed.
- What was not covered: integration tests, slow tests excluded from PIT, modules not run.

## When the target cannot be reached honestly

Say so with the numbers, and report the mutation gate as FAIL. Typical reasons: equivalent mutants
(about 3 to 6 percent of Java mutants), code only reachable through a running container, suspected
bugs that block assertions, dead code. Offer the choices to the user: change the code (fix the bug,
remove dead guards; their decision, outside this skill's rules), approve the equivalents and gate on
the score with them (`quality_report.py --pass-with-equivalents`), or accept a lower gate for the
module. Never pick one of these yourself.
