#!/usr/bin/env python3
"""Coverage + mutation status and a work list, from JaCoCo XML and PIT mutations.xml.

Usage: quality_report.py [--project DIR] [--coverage 95] [--mutation 95] [--max 40]
                         [--allowlist test-quality/equivalent-mutants.tsv] [--class PREFIX]

Finds reports in Maven (target/) or Gradle (build/) locations. Exit 0 when every gate passes,
1 when a gate fails, 2 when a report is missing.

Allowlist (accepted equivalent mutants), tab separated, one per line, '#' comments:
  <fully.qualified.Class>  <line>  <MutatorSimpleName>  <reason a reviewer can check>
Accepted mutants are reported separately; the raw score is always printed too.
"""
import argparse, os, sys, xml.etree.ElementTree as ET
from collections import defaultdict

def find(project, candidates):
    for c in candidates:
        p = os.path.join(project, c)
        if os.path.isfile(p):
            return p
    return None

def ranges(nums):
    nums = sorted(nums); out = []; i = 0
    while i < len(nums):
        j = i
        while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
            j += 1
        out.append(str(nums[i]) if i == j else f"{nums[i]}-{nums[j]}"); i = j + 1
    return ",".join(out)

def pct(a, b):
    return 100.0 if b == 0 else 100.0 * a / b

def source_line(project, src_roots, pkg_path, source_file, line):
    for root in src_roots:
        p = os.path.join(project, root, pkg_path, source_file)
        if os.path.isfile(p):
            try:
                with open(p, encoding="utf-8", errors="replace") as f:
                    for n, text in enumerate(f, 1):
                        if n == line:
                            return text.strip()
            except OSError:
                return ""
    return ""

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default=".")
    ap.add_argument("--coverage", type=float, default=95.0)
    ap.add_argument("--mutation", type=float, default=95.0)
    ap.add_argument("--max", type=int, default=40, help="max work-list entries per section")
    ap.add_argument("--allowlist", default="test-quality/equivalent-mutants.tsv")
    ap.add_argument("--class", dest="cls", default="", help="only classes starting with this prefix")
    a = ap.parse_args()
    proj = a.project
    src_roots = ["src/main/java", "src/main/kotlin"]
    failed = False

    jx = find(proj, ["target/site/jacoco/jacoco.xml", "build/reports/jacoco/test/jacocoTestReport.xml",
                     "target/site/jacoco-merged/jacoco.xml", "build/reports/jacoco/jacocoTestReport.xml"])
    mx = find(proj, ["target/pit-reports/mutations.xml", "build/reports/pitest/mutations.xml"])
    if not jx and not mx:
        print("no JaCoCo XML or PIT mutations.xml found; run the tests with the JaCoCo report first", file=sys.stderr)
        return 2

    # ---------- coverage ----------
    if jx:
        root = ET.parse(jx).getroot()
        tot = defaultdict(lambda: [0, 0])          # counter -> [missed, covered]
        rows = []
        for pkg in root.iter("package"):
            pname = pkg.get("name")
            for sf in pkg.findall("sourcefile"):
                fq = (pname + "/" + sf.get("name").rsplit(".", 1)[0]).replace("/", ".")
                if a.cls and not fq.startswith(a.cls):
                    continue
                c = {x.get("type"): (int(x.get("missed")), int(x.get("covered"))) for x in sf.findall("counter")}
                for k, (m, cv) in c.items():
                    tot[k][0] += m; tot[k][1] += cv
                miss = [int(l.get("nr")) for l in sf.findall("line") if int(l.get("ci")) == 0 and int(l.get("mi")) > 0]
                part = [(int(l.get("nr")), int(l.get("cb")), int(l.get("mb")) + int(l.get("cb")))
                        for l in sf.findall("line") if int(l.get("mb")) > 0 and int(l.get("ci")) > 0]
                lm, lc = c.get("LINE", (0, 0)); bm, bc = c.get("BRANCH", (0, 0))
                if lm or bm:
                    rows.append((lm + bm, fq, pct(lc, lm + lc), pct(bc, bm + bc), miss, part))
        lm, lc = tot["LINE"]; bm, bc = tot["BRANCH"]
        line_p, br_p = pct(lc, lm + lc), pct(bc, bm + bc)
        ok = line_p >= a.coverage and br_p >= a.coverage
        failed |= not ok
        print(f"== Coverage  ({os.path.relpath(jx, proj)})")
        print(f"LINE {line_p:.1f}% ({lm} missed of {lm + lc})   BRANCH {br_p:.1f}% ({bm} missed of {bm + bc})   "
              f"target {a.coverage:g}%   {'PASS' if ok else 'FAIL'}")
        if rows:
            print("-- classes with missed lines or branches (most first):")
            for _, fq, lp, bp, miss, part in sorted(rows, reverse=True)[: a.max]:
                extra = []
                if miss:
                    extra.append("uncovered lines " + ranges(miss))
                if part:
                    extra.append("partial branches " + ", ".join(f"{n}({c}/{t})" for n, c, t in part))
                print(f"  {fq}  line {lp:.0f}% branch {bp:.0f}%  " + "; ".join(extra))
    else:
        print("== Coverage: no JaCoCo XML report found (enable the XML report)")
        failed = True

    # ---------- mutation ----------
    if mx:
        allow = {}
        ap_path = os.path.join(proj, a.allowlist)
        if os.path.isfile(ap_path):
            with open(ap_path, encoding="utf-8") as f:
                for raw in f:
                    if raw.strip() and not raw.lstrip().startswith("#"):
                        parts = raw.rstrip("\n").split("\t")
                        if len(parts) >= 4 and parts[3].strip():
                            allow[(parts[0].strip(), int(parts[1]), parts[2].strip())] = parts[3].strip()
                        else:
                            print(f"allowlist line ignored (needs 4 tab-separated fields incl. reason): {raw.strip()}")
        root = ET.parse(mx).getroot()
        counts = defaultdict(int); total = detected = no_cov = 0
        accepted = []; work = []; used = set()
        for m in root.iter("mutation"):
            cls = m.findtext("mutatedClass")
            if a.cls and not cls.startswith(a.cls):
                continue
            status = m.get("status"); det = m.get("detected") == "true"
            total += 1; counts[status] += 1
            detected += det; no_cov += status == "NO_COVERAGE"
            if det:
                continue
            line = int(m.findtext("lineNumber")); mut = m.findtext("mutator").rsplit(".", 1)[-1]
            key = (cls, line, mut)
            if key in allow:
                accepted.append((key, allow[key])); used.add(key); continue
            outer = cls.split("$")[0]
            pkg_path = outer.rsplit(".", 1)[0].replace(".", "/") if "." in outer else ""
            work.append((status, cls, line, m.findtext("mutatedMethod"), mut, m.findtext("description"),
                         source_line(proj, src_roots, pkg_path, m.findtext("sourceFile"), line)))
        raw_score = pct(detected, total)
        adj_score = pct(detected + len(accepted), total)
        strength = pct(detected, total - no_cov)
        ok = adj_score >= a.mutation
        failed |= not ok
        # mutations.xml carries partial="true" even for full runs (PIT 1.30), so it says nothing; with
        # --class or a -DtargetClasses run, only part of the module was mutated
        print(f"\n== Mutation  ({os.path.relpath(mx, proj)})")
        print("mutants " + str(total) + ": " + ", ".join(f"{k.lower()} {v}" for k, v in sorted(counts.items())))
        print(f"mutation score {raw_score:.1f}% (PIT mutationThreshold uses this)   test strength {strength:.1f}%")
        if accepted:
            print(f"accepted equivalent mutants: {len(accepted)} -> score with allowlist {adj_score:.1f}%")
        print(f"target {a.mutation:g}%   {'PASS' if ok else 'FAIL'}")
        stale = set(allow) - used
        if stale:
            print(f"allowlist entries that matched no surviving mutant (remove them): {len(stale)}")
            for k in sorted(stale)[:10]:
                print("  " + "\t".join(map(str, k)))
        if work:
            print("-- surviving / uncovered mutants (kill them, or prove equivalence):")
            work.sort(key=lambda w: (w[1], w[2]))
            for status, cls, line, meth, mut, desc, text in work[: a.max]:
                print(f"  {status:<11} {cls}:{line} {meth}()  {mut}: {desc}")
                if text:
                    print(f"              | {text}")
            if len(work) > a.max:
                print(f"  ... {len(work) - a.max} more (use --max or --class)")
    else:
        print("\n== Mutation: no PIT mutations.xml found (outputFormats must include XML)")
        failed = True

    print("\nRESULT: " + ("FAIL" if failed else "PASS"))
    return 1 if failed else 0

if __name__ == "__main__":
    sys.exit(main())
