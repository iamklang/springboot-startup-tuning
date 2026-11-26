# The coverage and mutation loop

Contents: order of work · one round · acceptance filter · equivalent mutants · suspected bugs ·
budget and time · CI

## Order of work

1. Coverage first, because it is cheap: write tests for each class until JaCoCo shows no missed lines
   or branches that valid input can reach.
2. Then mutation, class by class, starting with business logic (services, policies, calculators,
   validators, value objects), then controllers and clients.
3. One full run of both gates at the end.

Line coverage saturates early; mutation score keeps rising for a few more rounds. Research systems
converge in 2 to 5 rounds per class. Tests that kill a mutant without adding coverage are valuable
(about half of Meta's mutation-guided tests), so never drop a test only because coverage did not move.

## One round

```bash
# coverage (Maven)
mvn -q test
python3 <skill>/scripts/quality_report.py --mutation 0          # coverage section and work list
# mutation for one class and its tests
mvn -q test-compile org.pitest:pitest-maven:mutationCoverage \
    -Dpit.targetClasses=com.x.billing.LateFeePolicy -DtargetTests='com.x.billing.*Test' -Dmutation.minimum=0
python3 <skill>/scripts/quality_report.py --class com.x.billing.LateFeePolicy --coverage 0
```

`-Dmutation.minimum=0` keeps a partial run from failing the build while working; `-DmutationThreshold=0`
does not work, because the template sets the threshold explicitly. Gradle (template properties):
`./gradlew pitest -PpitTargetClasses=... -PpitTargetTests=... -PpitThreshold=0`. Both verified.

For each surviving mutant in the work list:
1. Read the method and the mutated line. Ask: which input makes the original and the mutant return
   or do different things? (`test-patterns.md` section 4 lists the answer per mutator.)
2. Add or strengthen a test. Prefer one new parameterized row over a new test method.
3. Work in batches of 5 to 10 mutants from the same class, then re-run PIT once. Re-running after
   every single mutant costs several times more for little gain.
4. At most 3 attempts per mutant. After that it goes to the "needs a human" list with what was tried.

## Acceptance filter for every new test

Keep a test only if all hold:
- it compiles and passes on the first run;
- it passes 5 runs in a row (`for i in 1 2 3 4 5; do mvn -q -Dtest=XTest test || break; done`);
- it adds coverage or kills at least one mutant that survived before;
- it follows `test-patterns.md` section 6 (no assertion-free tests, literals as expected values).

Delete tests that fail the filter instead of keeping them "for later".

## Equivalent mutants

An equivalent mutant changes the code without changing behaviour, so no test can kill it. Research
puts them at about 3 to 6 percent of Java mutants, which means a raw 95 percent may be out of reach.

Protocol:
1. Propose, do not decide. For each candidate, write the reason no input can distinguish the
   versions (for example "`size` is never 0 here: the constructor rejects empty lists, so `<` and
   `<=` behave the same"). Judge from the code with comments ignored; comments can be wrong.
2. Often the honest fix is in the code (dead guard, duplicated check). Production code is off limits:
   list it as a refactoring suggestion instead.
3. The user approves each entry. Only then add it to `test-quality/equivalent-mutants.tsv`
   (`assets/equivalent-mutants.tsv` shows the format: class, line, mutator, reason, approver, date).
4. Report both numbers: raw score (PIT's gate) and the score with approved equivalents.
   `quality_report.py` prints both and lists allowlist entries that no longer match.

PIT's own `mutationThreshold` and `quality_report.py` both pass or fail on the raw score. Approved
equivalents change only the second number. If they keep the raw score below 95, the gate fails and
the user chooses: fix the code that produces them (often a dead or duplicated guard), or set the PIT
threshold to the raw number reached and gate CI on `quality_report.py --pass-with-equivalents`. Do
not make that choice for them.

## Suspected bugs

Mutation-guided tests assume the current code is right. When the value a test would pin clearly
contradicts the Javadoc, the method or field name, an error message, a constant's comment or the
user's requirement, stop writing that test. (Silent or ambiguous specifications are different: test
the current behaviour under a name that states it, and list it as a question.) Record it under "suspected bugs" with the file, line, the
expected and actual behaviour, and the evidence. Do not change production code, and do not write a
test that asserts the questionable behaviour. Leave the related mutants and lines out of the "needs a
human" list and say they are blocked on the bug.

## Budget and time

- PIT time grows with mutants × tests per mutant. Keep Spring context tests out of the PIT run.
- Per class with `-DtargetTests` is seconds to a minute; a whole module once at the end.
- Long runs: start PIT in the background and wait for it, rather than hitting a tool timeout.
- `+auto_threads` uses the cores; `-DwithHistory` reuses results between runs of unchanged code.

## CI (suggest to the user, do not set up unasked)

- PR: mutation testing on changed classes only. PIT has `scmMutationCoverage`; confirm its parameters
  with `mvn help:describe -Dplugin=org.pitest:pitest-maven -Dgoal=scmMutationCoverage -Ddetail`.
- Nightly: the whole module with history cached.
- Coverage gate on every build (`mvn verify` / `./gradlew check`).
