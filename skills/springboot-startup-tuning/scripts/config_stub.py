#!/usr/bin/env python3
"""Generate a stub properties file for every ${placeholder} that has no default and no value.

Apps that get their config from a Config Server, Vault or env vars often fail to start locally
because dozens of @Value("${x}") placeholders cannot resolve. This script lists them, guesses a
type from the Java/Kotlin field or parameter they are bound to, and writes a harmless value of
that type, so the app can start for measurement without the real config.

Usage:
  config_stub.py [project-dir] [-o stub.properties]

Then start the app with:
  --spring.config.additional-location=file:<stub.properties> --spring.cloud.config.enabled=false

Rules for values: URLs and hosts point at 127.0.0.1:1 (a closed port, so a real connection attempt
fails fast instead of hanging), secrets get an obvious non-secret value, and unknown types get the
string "stub". Review the file: an enum or a value with a format check needs a real value, and the
startup error names the property. @ConfigurationProperties classes are not scanned; their missing
fields show up as binding errors at startup.
"""
import argparse, os, re, sys

SRC_EXT = (".java", ".kt")
CFG_EXT = (".yml", ".yaml", ".properties")
PH = re.compile(r"\$\{([A-Za-z0-9_.\-\[\]]+)(:[^}]*)?\}")
VALUE_ANN = re.compile(r'@Value\(\s*"((?:[^"\\]|\\.)*)"\s*\)')
JAVA_DECL = re.compile(r'\)\s*(?:@\w+(?:\([^)]*\))?\s*)*(?:final\s+|private\s+|protected\s+|public\s+|static\s+)*'
                       r'([\w.]+(?:<[^>]*>)?(?:\[\])?)\s+\w+\s*[;,)=]')
KT_DECL = re.compile(r'\)\s*(?:@\w+(?:\([^)]*\))?\s*)*(?:[a-z]+\s+)*(?:val|var)\s+\w+\s*:\s*([\w.]+(?:<[^>]*>)?\??)')

BY_TYPE = {
    "boolean": "false", "Boolean": "false",
    "int": "1", "Integer": "1", "long": "1", "Long": "1", "short": "1", "Short": "1", "Int": "1",
    "double": "1.0", "Double": "1.0", "float": "1.0", "Float": "1.0", "BigDecimal": "1.0",
    "Duration": "1s", "Period": "P1D", "DataSize": "1MB",
    "UUID": "00000000-0000-0000-0000-000000000000",
    "LocalTime": "00:00", "LocalDate": "2000-01-01", "LocalDateTime": "2000-01-01T00:00:00",
    "Instant": "2000-01-01T00:00:00Z", "ZoneId": "UTC", "Charset": "UTF-8", "Locale": "en",
    "URI": "http://127.0.0.1:1/stub", "URL": "http://127.0.0.1:1/stub",
}

def by_name(name):
    n = name.lower()
    if re.search(r"(password|secret|token|api[-_.]?key|credential|private[-_.]?key)", n): return "stub-not-a-secret"
    if re.search(r"jdbc", n) or n.endswith("datasource.url"): return "jdbc:postgresql://127.0.0.1:1/stub"
    if re.search(r"(url|uri|endpoint|base[-_.]?path)$", n): return "http://127.0.0.1:1/stub"
    if re.search(r"(host|hostname|address)$", n): return "127.0.0.1"
    if re.search(r"port$", n): return "1"
    if re.search(r"(timeout|interval|delay|ttl|duration)", n): return "1s"
    if re.search(r"(enabled|enable|active)$", n): return "false"
    if re.search(r"(size|count|max|min|limit|retries|threads)$", n): return "1"
    if re.search(r"(cron)$", n): return "0 0 0 * * *"
    return "stub"

def walk(root, exts):
    for d, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if x not in ("target", "build", "node_modules", ".git", "test")]
        for f in files:
            if f.endswith(exts):
                yield os.path.join(d, f)

def yaml_keys(text):
    """Flatten simple YAML mapping keys (no anchors or flow style); good enough to know what is set.
    Returns (set of keys, {line number: full key on that line})."""
    keys, stack, at = set(), [], {}
    for i, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith(("#", "-")) or line.strip() == "---":
            if line.strip() == "---": stack = []
            continue
        m = re.match(r"^(\s*)([^:#\s][^:#]*?)\s*:(\s|$)", line)
        if not m: continue
        ind = len(m.group(1)); key = m.group(2).strip().strip("'\"")
        while stack and stack[-1][0] >= ind: stack.pop()
        stack.append((ind, key))
        full = ".".join(k for _, k in stack)
        keys.add(full); at[i] = full
    return keys, at

def norm(k):  # relaxed binding: FOO_BAR, foo-bar, fooBar all map to foobar-ish form
    return re.sub(r"[-_.]", "", k).lower()

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("project", nargs="?", default=".")
    ap.add_argument("-o", "--output")
    a = ap.parse_args()
    main_dir = os.path.join(a.project, "src", "main")
    if not os.path.isdir(main_dir):
        sys.exit(f"no src/main under {a.project}")

    defined, needed = set(), {}  # name -> (type or None, first location)
    hint = {}  # placeholder -> config key it is assigned to, e.g. DB_URL -> spring.datasource.url
    for path in walk(main_dir, CFG_EXT):
        text = open(path, encoding="utf-8", errors="replace").read()
        at = {}
        if path.endswith(".properties"):
            for i, line in enumerate(text.splitlines(), 1):
                m = re.match(r"^\s*([^#!=:\s][^=:\s]*)\s*[=:]", line)
                if m: defined.add(m.group(1)); at[i] = m.group(1)
        else:
            k, at = yaml_keys(text)
            defined |= k
        for i, line in enumerate(text.splitlines(), 1):
            for m in PH.finditer(line):
                if m.group(2) is None and m.group(1) not in needed:
                    needed[m.group(1)] = (None, f"{os.path.relpath(path, a.project)}:{i}")
                    if i in at: hint[m.group(1)] = at[i]
    for path in walk(main_dir, SRC_EXT):
        text = open(path, encoding="utf-8", errors="replace").read()
        for m in VALUE_ANN.finditer(text):
            tail = text[m.end() - 1:m.end() + 300]
            t = (KT_DECL if path.endswith(".kt") else JAVA_DECL).match(tail)
            typ = t.group(1).split(".")[-1].rstrip("?") if t else None
            line = text.count("\n", 0, m.start()) + 1
            for p in PH.finditer(m.group(1)):
                if p.group(2) is None:
                    prev = needed.get(p.group(1))
                    if prev is None or prev[0] is None:
                        needed[p.group(1)] = (typ, f"{os.path.relpath(path, a.project)}:{line}")

    dn = {norm(k) for k in defined}
    missing = {k: v for k, v in needed.items() if norm(k) not in dn}
    out = ["# Stub values for placeholders with no default and no value in application*.yml/properties.",
           "# Generated for local startup measurement only. Never deploy this file.", ""]
    for name in sorted(missing):
        typ, loc = missing[name]
        base = re.sub(r"<.*", "", typ or "")
        if base in BY_TYPE and not re.search(r"(password|secret|token|key)", name.lower()):
            val = BY_TYPE[base]
        elif base in ("List", "Set", "Collection") or (typ or "").endswith("[]"):
            val = "stub"
        else:
            val = by_name(hint.get(name, name))
        note = f"{typ or 'type unknown'} @ {loc}" + (f" (value of {hint[name]})" if name in hint else "")
        if typ and base not in BY_TYPE and base not in ("String", "List", "Set", "Collection", "Object") and not (typ or "").endswith("[]"):
            note += "  CHECK: maybe an enum or custom type"
        out += [f"# {note}", f"{name}={val}"]
    text = "\n".join(out) + "\n"
    print(f"{len(needed)} placeholders without default, {len(needed) - len(missing)} already set in config, "
          f"{len(missing)} stubbed", file=sys.stderr)
    if a.output:
        os.makedirs(os.path.dirname(a.output) or ".", exist_ok=True)
        open(a.output, "w").write(text)
        print(f"wrote {a.output}", file=sys.stderr)
    else:
        sys.stdout.write(text)

if __name__ == "__main__":
    main()
