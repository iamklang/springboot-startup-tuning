#!/usr/bin/env python3
"""Open-model HTTP load generator with coordinated-omission-safe latency.

Sends requests at a fixed arrival rate, independent of how fast the server answers.
Latency is measured from each request's *scheduled* send time, so a stalled server
shows up as high latency instead of as fewer requests (coordinated omission).

Usage:
  loadgen.py -r RATE -d SECONDS [-c MAX_IN_FLIGHT] [-H 'Name: value'] [--json] URL [URL ...]

URLs are used round-robin. Prints achieved rate, p50/p90/p99/p99.9/max and errors.
"dropped" counts requests that could not start because MAX_IN_FLIGHT workers were busy
for more than 1 s past their scheduled time; any dropped > 0 means the target rate was
not delivered and the result is not a valid capacity measurement.
Good for local comparisons up to a few hundred req/s. For staging and higher rates use
k6 (constant-arrival-rate), wrk2 -R or vegeta.
"""
import argparse
import http.client
import json
import queue
import sys
import threading
import time
import urllib.parse


def percentile(sorted_vals, p):
    if not sorted_vals:
        return float("nan")
    k = max(0, min(len(sorted_vals) - 1, int(round(p / 100.0 * len(sorted_vals) + 0.5)) - 1))
    return sorted_vals[k]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-r", "--rate", type=float, required=True, help="requests per second")
    ap.add_argument("-d", "--duration", type=float, required=True, help="seconds")
    ap.add_argument("-c", "--concurrency", type=int, default=64, help="max in-flight requests (workers)")
    ap.add_argument("-H", "--header", action="append", default=[])
    ap.add_argument("-t", "--timeout", type=float, default=10.0)
    ap.add_argument("--json", action="store_true", help="print one JSON object instead of text")
    ap.add_argument("urls", nargs="+")
    a = ap.parse_args()

    headers = {"Connection": "keep-alive"}
    for h in a.header:
        k, _, v = h.partition(":")
        headers[k.strip()] = v.strip()
    targets = []
    for u in a.urls:
        p = urllib.parse.urlsplit(u)
        path = (p.path or "/") + (("?" + p.query) if p.query else "")
        targets.append((p.hostname, p.port or 80, path))

    total = int(a.rate * a.duration)
    interval = 1.0 / a.rate
    work = queue.Queue()
    lat, errors, dropped = [], [0], [0]
    lock = threading.Lock()

    def worker():
        conns = {}
        while True:
            item = work.get()
            if item is None:
                return
            sched, (host, port, path) = item
            if time.perf_counter() - sched > 1.0:
                with lock:
                    dropped[0] += 1
                continue
            ok = False
            for attempt in (0, 1):  # one retry on a stale keep-alive connection
                c = conns.get((host, port))
                if c is None:
                    c = http.client.HTTPConnection(host, port, timeout=a.timeout)
                    conns[(host, port)] = c
                try:
                    c.request("GET", path, headers=headers)
                    r = c.getresponse()
                    r.read()
                    ok = 200 <= r.status < 400
                    break
                except Exception:
                    c.close()
                    conns.pop((host, port), None)
                    if attempt == 1:
                        ok = False
            done = time.perf_counter()
            with lock:
                lat.append((done - sched) * 1000.0)
                if not ok:
                    errors[0] += 1

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(a.concurrency)]
    for t in threads:
        t.start()
    start = time.perf_counter()
    for i in range(total):
        sched = start + i * interval
        now = time.perf_counter()
        if sched > now:
            time.sleep(sched - now)
        work.put((sched, targets[i % len(targets)]))
    for _ in threads:
        work.put(None)
    for t in threads:
        t.join()
    elapsed = time.perf_counter() - start

    s = sorted(lat)
    res = {
        "target_rps": a.rate, "achieved_rps": round(len(s) / elapsed, 1), "requests": len(s),
        "errors": errors[0], "dropped": dropped[0],
        "p50_ms": round(percentile(s, 50), 2), "p90_ms": round(percentile(s, 90), 2),
        "p99_ms": round(percentile(s, 99), 2), "p999_ms": round(percentile(s, 99.9), 2),
        "max_ms": round(s[-1], 2) if s else float("nan"),
    }
    if a.json:
        print(json.dumps(res))
    else:
        print("rate %(target_rps)s -> %(achieved_rps)s req/s  n=%(requests)s errors=%(errors)s dropped=%(dropped)s" % res)
        print("latency ms  p50 %(p50_ms)s  p90 %(p90_ms)s  p99 %(p99_ms)s  p99.9 %(p999_ms)s  max %(max_ms)s" % res)
    if res["dropped"]:
        print("warning: dropped > 0, target rate not delivered; raise -c or lower -r", file=sys.stderr)


if __name__ == "__main__":
    main()
