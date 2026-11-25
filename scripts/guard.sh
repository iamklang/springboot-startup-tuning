#!/usr/bin/env bash
# Guard against "making the numbers pass" instead of testing.
#
# Usage (from the project root):
#   guard.sh snapshot   record checksums of production code, build files and existing tests
#   guard.sh check      compare with the snapshot; exit 1 if production code or build files changed
#
# Snapshot after the tooling setup the user approved, before writing tests. `check` then reports:
#   - any change under src/main (production code must not change to make tests pass)
#   - any change to build files or lombok.config (thresholds, excludes, agents)
#   - changed or deleted test files that existed at snapshot time (review: weakened assertions?)
# New test files are expected and are listed for information only.
set -u
STATE=test-quality/.guard.sha256
cmd=${1:-}

files_main()  { find src/main -type f 2>/dev/null; }
files_build() { ls pom.xml */pom.xml build.gradle build.gradle.kts */build.gradle */build.gradle.kts \
                   settings.gradle settings.gradle.kts gradle.properties lombok.config \
                   test-quality/equivalent-mutants.tsv 2>/dev/null; }
files_test()  { find src/test -type f 2>/dev/null; }
sum() { shasum -a 256 "$@" 2>/dev/null || sha256sum "$@"; }

case "$cmd" in
  snapshot)
    mkdir -p test-quality
    { files_main; files_build; files_test; } | sort -u | while read -r f; do sum "$f"; done > "$STATE"
    echo "snapshot: $(wc -l < "$STATE" | tr -d ' ') files -> $STATE"
    ;;
  check)
    [ -f "$STATE" ] || { echo "no snapshot; run: guard.sh snapshot" >&2; exit 2; }
    bad=0
    while read -r h f; do
      if [ ! -f "$f" ]; then now="deleted"; else now=$(sum "$f" | cut -d' ' -f1); fi
      [ "$now" = "$h" ] && continue
      case "$f" in
        src/main/*)  echo "CHANGED production code: $f"; bad=1 ;;
        src/test/*)  echo "REVIEW changed or deleted existing test: $f" ;;
        *)           echo "CHANGED build/config: $f"; bad=1 ;;
      esac
    done < "$STATE"
    known=$(cut -d' ' -f3- "$STATE")
    newfiles=$({ files_main; files_test; } | while read -r f; do
      printf '%s\n' "$known" | grep -qxF "$f" || echo "$f"; done)
    for f in $newfiles; do
      case "$f" in
        src/main/*) echo "CHANGED production code (new file): $f"; bad=1 ;;
        *)          echo "new test file: $f" ;;
      esac
    done
    if [ $bad -eq 0 ]; then echo "GUARD: OK (production code and build files unchanged)"; else
      echo "GUARD: FAIL - revert these, or get the user's explicit approval for each" >&2; fi
    exit $bad
    ;;
  *) echo "usage: guard.sh snapshot|check" >&2; exit 2 ;;
esac
