#!/usr/bin/env python3
"""Rank the slowest steps in a Spring Boot /actuator/startup snapshot.

Usage:
  startup_report.py <file.json | http://host:port/actuator/startup> [--top N] [--min-ms M]

The endpoint needs `management.endpoints.web.exposure.include=startup` and a
BufferingApplicationStartup set on the SpringApplication (otherwise the timeline is empty).
Reads the JSON over HTTP when given a URL (no auth; use curl -o file.json for anything else).
"""
import json
import sys
import urllib.request
from collections import defaultdict
from datetime import datetime


def parse_ts(s):
    # 2024-05-01T10:00:00.123456Z or with offset; Python 3.11+ handles both via fromisoformat
    s = s.replace("Z", "+00:00")
    # trim nanoseconds to microseconds
    if "." in s:
        head, tail = s.split(".", 1)
        frac = ""
        rest = ""
        for i, ch in enumerate(tail):
            if ch.isdigit():
                frac += ch
            else:
                rest = tail[i:]
                break
        s = f"{head}.{frac[:6].ljust(6, '0')}{rest}"
    return datetime.fromisoformat(s)


def load(src):
    if src.startswith("http://") or src.startswith("https://"):
        with urllib.request.urlopen(src, timeout=10) as r:
            return json.load(r)
    with open(src) as f:
        return json.load(f)


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    src = argv[1]
    top = 20
    min_ms = 0.0
    if "--top" in argv:
        top = int(argv[argv.index("--top") + 1])
    if "--min-ms" in argv:
        min_ms = float(argv[argv.index("--min-ms") + 1])
    data = load(src)
    events = data.get("timeline", {}).get("events", [])
    if not events:
        print("no events: is BufferingApplicationStartup configured and the startup endpoint exposed?")
        return 1
    rows = []
    by_parent = defaultdict(float)
    for e in events:
        st = e["startupStep"]
        start, end = parse_ts(e["startTime"]), parse_ts(e["endTime"])
        ms = (end - start).total_seconds() * 1000
        tags = {t["key"]: t["value"] for t in st.get("tags", [])}
        rows.append((ms, st["name"], st["id"], st.get("parentId"), tags, start))
        if st.get("parentId") is not None:
            by_parent[st["parentId"]] += ms
    # self time = own duration minus children durations (children are nested inside)
    rows_self = [(ms - by_parent.get(sid, 0.0), ms, name, sid, pid, tags, start) for ms, name, sid, pid, tags, start in rows]
    t0 = min(r[6] for r in rows_self)
    t1 = max(parse_ts(e["endTime"]) for e in events)
    print(f"events: {len(rows)}  timeline span: {(t1 - t0).total_seconds():.3f}s  (startTime {data.get('timeline', {}).get('startTime', '?')})")

    print(f"\n== Top {top} steps by self time (ms, excluding nested children) ==")
    for self_ms, total_ms, name, sid, pid, tags, _ in sorted(rows_self, key=lambda r: -r[0])[:top]:
        if self_ms < min_ms:
            break
        label = tags.get("beanName") or tags.get("name") or tags.get("location") or ""
        print(f"{self_ms:9.1f}  {total_ms:9.1f}  {name:<42} {label}")

    beans = [(r[1], r[5].get("beanName")) for r in rows_self if r[2] == "spring.beans.instantiate" and r[5].get("beanName")]
    if beans:
        print(f"\n== Top {top} beans by instantiate time, inclusive of their dependencies (ms) ==")
        for ms, bean in sorted(beans, key=lambda b: -b[0])[:top]:
            print(f"{ms:9.1f}  {bean}")
        print("(inclusive time: a bean that waits for a slow dependency shows that dependency's time too;"
              " the self-time table above says who actually did the work)")

    agg = defaultdict(float)
    for self_ms, _, name, *_ in rows_self:
        agg[name] += self_ms
    print("\n== Self time aggregated by step type (ms) ==")
    for name, ms in sorted(agg.items(), key=lambda kv: -kv[1])[:15]:
        print(f"{ms:9.1f}  {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
