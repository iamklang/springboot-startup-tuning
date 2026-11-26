#!/usr/bin/env python3
"""Approximate SonarQube's Java test rules on test sources, without a SonarQube server.

Checks the mechanical rules from references/sonar-test-rules.md with a light parser (comments and
strings blanked, brackets matched). It is a stand-in, not SonarQube: it misses issues that need
type information and may raise a few false positives. Treat every finding as "fix or explain".

usage: sonar_test_lint.py [--project DIR] [--profile skill|sonar-way] [--max-assertions 25]
                          [--only S2699,S5838] [--changed] [--quiet] [PATH ...]
PATHs are test files or directories; default is <project>/src/test/java.
--changed: only test files that are new or changed since `guard.sh snapshot`
(test-quality/.guard.sha256), i.e. the tests written in this task. Gate on this; lint the rest
for the report.
Profile "skill" (default) = Sonar way test rules + S6073, S5977, S8692, S5979, and flags
NOSONAR / @SuppressWarnings("java:S...") in tests. Profile "sonar-way" = Sonar way only, and
lines with NOSONAR are skipped as SonarQube does.
Exit: 0 no issues, 1 issues found, 2 usage error.
"""
import argparse
import bisect
import os
import re
import sys
from collections import Counter, defaultdict

TEST_ANN = {"Test", "ParameterizedTest", "RepeatedTest", "TestFactory", "TestTemplate"}
LIFECYCLE = {"BeforeEach", "AfterEach", "BeforeAll", "AfterAll"}
CLASS_RE = re.compile(r"^((Test|IT)[a-zA-Z0-9_]+|[A-Z][a-zA-Z0-9_]*(Test|Tests|TestCase|IT|ITCase))$")
KEYWORDS = {"if", "for", "while", "switch", "catch", "synchronized", "return", "new", "throw", "else",
            "try", "do", "super", "this", "assert", "case", "yield"}
ASSERT_CALL = re.compile(r"(?<![\w$])(assert\w*|verify\w*|fail\w*|should\w*|check\w*|expect\w*|"
                         r"validate\w*|andExpect\w*|approve\w*|until\w*|isThrownBy)\s*\(")
COUNT_CALL = re.compile(r"(?<![\w$.])(assert\w*|verify|fail)\s*\(|\.(andExpect|should)\s*\(")
MATCHER = re.compile(r"^(any\w*|eq|argThat|isA|isNull|isNotNull|notNull|same|refEq|nullable|"
                     r"intThat|longThat|booleanThat|doubleThat|charThat|startsWith|endsWith|matches)\s*\(")
LITERAL = re.compile(r'^\s*(-?\d[\w.]*|"\s*"|\'.\'|\'\s*\'|true|false|null)\s*$')
WAY_EXTRA = {"S6073", "S5977", "S8692", "S5979", "NOSONAR"}   # skill profile only


class Src:
    """Comment/string-blanked view of a Java file with bracket pairs and enclosing-brace map."""

    def __init__(self, text):
        self.text = text
        self.comments = []          # (offset, text)
        out = list(text)
        i, n = 0, len(text)
        while i < n:
            c = text[i]
            if text.startswith("//", i):
                j = text.find("\n", i)
                j = n if j < 0 else j
                self.comments.append((i, text[i:j]))
                out[i:j] = " " * (j - i)
                i = j
            elif text.startswith("/*", i):
                j = text.find("*/", i + 2)
                j = n if j < 0 else j + 2
                self.comments.append((i, text[i:j]))
                out[i:j] = [ch if ch == "\n" else " " for ch in text[i:j]]
                i = j
            elif text.startswith('"""', i):
                j = text.find('"""', i + 3)
                j = n if j < 0 else j + 3
                out[i + 1:j - 1] = [ch if ch == "\n" else " " for ch in text[i + 1:j - 1]]
                i = j
            elif c in "\"'":
                j = i + 1
                while j < n and text[j] != c and text[j] != "\n":
                    j += 2 if text[j] == "\\" else 1
                out[i + 1:j] = " " * (j - i - 1)
                i = j + 1
            else:
                i += 1
        self.code = "".join(out)
        self.nl = [m.start() for m in re.finditer("\n", text)]
        self.pairs, self.encl = {}, [-1] * len(self.code)
        stack, braces = [], []
        for k, ch in enumerate(self.code):
            if ch in "({[":
                stack.append(k)
                if ch == "{":
                    braces.append(k)
            elif ch in ")}]" and stack:
                o = stack.pop()
                self.pairs[o], self.pairs[k] = k, o
                if ch == "}" and braces:
                    braces.pop()
            self.encl[k] = braces[-1] if braces else -1
        self.parent = {o: (self.encl[o - 1] if o > 0 else -1) for o in self.pairs if self.code[o] == "{"}

    def line(self, off):
        return bisect.bisect_left(self.nl, off) + 1

    def prefix_start(self, idx):
        """Start of the declaration/statement that ends just before idx."""
        k = idx - 1
        while k >= 0:
            ch = self.code[k]
            if ch in ")]" and k in self.pairs:
                k = self.pairs[k] - 1
                continue
            if ch in ";{}":
                return k + 1
            k -= 1
        return 0

    def calls(self, name_re, start=0, end=None):
        """Yield (name_start, open_paren, close_paren, name) for calls matching name_re."""
        end = len(self.code) if end is None else end
        for m in re.finditer(r"(?<![\w$])(" + name_re + r")\s*\(", self.code[start:end]):
            o = start + m.end() - 1
            if o in self.pairs:
                yield start + m.start(), o, self.pairs[o], m.group(1)

    def chain(self, close):
        """Text of the call chain after close paren up to the end of the statement."""
        k, n = close + 1, len(self.code)
        while k < n:
            ch = self.code[k]
            if ch in "({[":
                k = self.pairs.get(k, k) + 1
                continue
            if ch in ";)}],":
                break
            k += 1
        return self.code[close + 1:k], k


def chain_methods(chain):
    """Names of the calls in a '.a(..).b(..)' chain, in order."""
    return re.findall(r"\.\s*(?:<[^>]*>\s*)?(\w+)\s*\(\)", flatten(chain))


NOT_SIZE = re.compile(r"^(as|describedAs|withFailMessage|overridingErrorMessage|isNotNull|asList|\w*InstanceOf\w*|"
                      r"using\w*|extracting\w*|filtered\w*|doesNotContain\w*|all\w*)$")
SET_CONTEXT = re.compile(r"^(extracting\w*|using\w*|filtered\w*|flatExtracting|map|flatMap|element)$")
CHECKED = {"Exception", "Throwable", "IOException", "UncheckedIOException_", "InterruptedException",
           "TimeoutException", "ExecutionException", "SQLException", "URISyntaxException", "ParseException",
           "ClassNotFoundException", "CloneNotSupportedException", "GeneralSecurityException",
           "FileNotFoundException", "JsonProcessingException", "MessagingException", "JMSException"}
AUTHORIZED = re.compile(r"^(fail|mock|name|ordinal|emptyList|emptySet|emptyMap|singleton|singletonList|singletonMap|"
                        r"empty)$")


def count_invocations(body):
    """Method and constructor calls in a lambda body, skipping nested lambdas and the calls Sonar
    treats as safe (fail, mock, Collections.empty*/singleton*, no-arg List/Set/Map.of, no-arg
    collection constructors, Optional/Stream.empty)."""
    # blank nested lambdas: any (...) group that contains '->'
    b = list(body)
    for mm in re.finditer(r"->", body):
        depth, k = 0, mm.start() - 1
        # find the enclosing open paren of this nested lambda
        while k >= 0:
            if body[k] == ")":
                depth += 1
            elif body[k] == "(":
                if depth == 0:
                    break
                depth -= 1
            k -= 1
        if k < 0:
            continue
        depth, j = 0, k
        while j < len(body):
            depth += body[j] == "("
            depth -= body[j] == ")"
            if depth == 0:
                break
            j += 1
        b[k + 1:j] = " " * (j - k - 1)
    body = "".join(b)
    n = 0
    for x in re.finditer(r"(?<![\w$])(new\s+([\w.]+)(?:\s*<[^>]*>)?|([\w$]+))\s*\(", body):
        if x.group(3) in KEYWORDS:
            continue
        o = x.end() - 1
        depth, j = 0, o
        while j < len(body):
            depth += body[j] == "("
            depth -= body[j] == ")"
            if depth == 0:
                break
            j += 1
        noargs = not body[o + 1:j].strip()
        name = x.group(3) or x.group(2).split(".")[-1]
        if AUTHORIZED.match(name) or (name == "of" and noargs and re.search(r"(List|Set|Map)\s*\.\s*$", body[:x.start()])) \
                or (x.group(2) and noargs and re.match(r"(ArrayList|LinkedList|HashSet|HashMap|LinkedHashMap|"
                                                      r"LinkedHashSet|TreeMap|TreeSet)$", name)):
            continue
        n += 1
    return n


def split_args(s):
    out, depth, cur = [], 0, []
    for ch in s:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            out.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    if "".join(cur).strip():
        out.append("".join(cur).strip())
    return out


def flatten(s):
    prev = None
    while prev != s:
        prev, s = s, re.sub(r"\([^()]*\)", "()", s)
    return s


def annotations(raw):
    res = []
    for m in re.finditer(r"@([\w.]+)\s*(\()?", raw):
        name = m.group(1).split(".")[-1]
        arg = None
        if m.group(2):
            depth, k = 0, m.end() - 1
            for j in range(k, len(raw)):
                depth += raw[j] == "("
                depth -= raw[j] == ")"
                if depth == 0:
                    arg = raw[k + 1:j]
                    break
        res.append((name, arg))
    return res


class Decl:
    def __init__(self, kind, name, start, open_, close, anns, mods, extra=None):
        self.kind, self.name, self.start, self.open, self.close = kind, name, start, open_, close
        self.anns, self.mods, self.extra = anns, mods, extra or {}

    def has(self, *names):
        return any(a in names for a, _ in self.anns)


def parse(src):
    code = src.code
    classes, methods, fields = [], [], []
    for o, c in src.pairs.items():
        if code[o] != "{":
            continue
        ps = src.prefix_start(o)
        raw_text = src.text[ps:o]
        pre = code[ps:o]
        flat = flatten(pre)
        cm = re.search(r"\b(class|interface|enum|record)\s+(\w+)", flat)
        if cm and not re.search(r"\bnew\b", flat):
            mods = set(re.findall(r"\b(public|protected|private|static|final|abstract)\b", flat[:cm.start()]))
            classes.append(Decl("class", cm.group(2), ps + pre.find(cm.group(2)), o, c,
                                annotations(raw_text), mods, {"kind": cm.group(1)}))
            continue
        par = src.parent.get(o, -1)
        if par < 0 or not any(k.open == par for k in classes) and not re.search(
                r"\b(class|interface|enum|record)\s+\w+", flatten(code[src.prefix_start(par):par])):
            continue
        nofoo = re.sub(r"@[\w.]+\s*(\(\))?", " ", flat)
        mm = re.search(r"(\w+)\s*\(\)\s*(throws\s+[\w.,\s]+)?\s*$", nofoo)
        if not mm or mm.group(1) in KEYWORDS or "=" in nofoo or "->" in nofoo:
            continue
        before = nofoo[:mm.start()].split()
        mods = {w for w in before if w in ("public", "protected", "private", "static", "final", "abstract",
                                          "default", "synchronized")}
        rtype = [w for w in before if w not in mods]
        # locate the real parameter list in the raw prefix
        name_off = ps + [m.start() for m in re.finditer(r"\b" + re.escape(mm.group(1)) + r"\s*\(", pre)][-1]
        po = code.index("(", name_off)
        params = code[po + 1:src.pairs.get(po, po)]
        methods.append(Decl("method", mm.group(1), name_off, o, c, annotations(raw_text), mods,
                            {"rtype": rtype[-1] if rtype else "", "params": params, "class_open": par}))
    # fields: statements directly in a class body
    for k in classes:
        for m in re.finditer(";", code[k.open + 1:k.close]):
            i = k.open + 1 + m.start()
            if src.encl[i] != k.open:
                continue
            ps = src.prefix_start(i)
            pre, raw_text = code[ps:i], src.text[ps:i]
            flat = flatten(pre)
            if re.search(r"\b(import|package|return|class)\b", flat):
                continue
            left = re.sub(r"@[\w.]+\s*(\(\))?", " ", flat.split("=", 1)[0]).strip()
            if not left or left.endswith(")") or "(" in left:
                continue
            words = left.replace(",", " ").split()
            if len(words) < 2:
                continue
            mods = {w for w in words if w in ("public", "protected", "private", "static", "final", "transient",
                                             "volatile")}
            init = pre.split("=", 1)[1] if "=" in flatten(pre) else ""
            fields.append(Decl("field", words[-1], ps + pre.find(words[-1]), i, i, annotations(raw_text), mods,
                               {"init": init, "class_open": k.open}))
    return classes, methods, fields


def lint_file(path, text, opts):
    src = Src(text)
    code = src.code
    issues = []
    nosonar_lines = {src.line(off) for off, t in src.comments if "NOSONAR" in t}

    def add(off, rule, msg):
        if opts.only and rule not in opts.only:
            return
        if opts.profile == "sonar-way" and rule in WAY_EXTRA:
            return
        ln = src.line(off)
        if opts.profile == "sonar-way" and ln in nosonar_lines:
            return
        issues.append((path, ln, rule, msg))

    classes, methods, fields = parse(src)
    imports = re.findall(r"(?m)^\s*import\s+(static\s+)?([\w.]+(?:\.\*)?)\s*;", code)
    jupiter = any(i.startswith("org.junit.jupiter") for _, i in imports)
    tests = [m for m in methods if m.has(*TEST_ANN)]
    by_class = defaultdict(list)
    for m in methods:
        by_class[m.extra["class_open"]].append(m)
    local = {m.name: m for m in methods}

    # ---- assertion presence (S2699), with local helper methods followed
    memo = {}

    def asserts(m, seen=()):
        if m.name in memo:
            return memo[m.name]
        body = code[m.open:m.close]
        ok = bool(ASSERT_CALL.search(body)) or bool(re.search(r"\bassertThat\w*\s*\(", body))
        if not ok:
            for c in re.finditer(r"(?<![\w$.])(\w+)\s*\(", body):
                h = local.get(c.group(1))
                if h and h is not m and h.name not in seen and asserts(h, seen + (m.name,)):
                    ok = True
                    break
        memo[m.name] = ok
        return ok

    for m in tests:
        body = code[m.open + 1:m.close]
        anns = dict(m.anns)
        if not asserts(m) and not (anns.get("Test") and "expected" in (anns.get("Test") or "")):
            add(m.start, "S2699", f"test '{m.name}' has no assertion")
        if not body.strip():
            add(m.start, "S1186", f"empty method '{m.name}'")
        if m.has("Test") and m.has("ParameterizedTest", "RepeatedTest", "TestFactory", "TestTemplate"):
            add(m.start, "S5967", f"'{m.name}' has @Test with another test annotation")
        if "private" in m.mods or "static" in m.mods:
            add(m.start, "S5810", f"test '{m.name}' is {'private' if 'private' in m.mods else 'static'}; JUnit skips it")
        if m.extra["rtype"] != "void" and not m.has("TestFactory"):
            add(m.start, "S5810", f"test '{m.name}' does not return void; JUnit skips it")
        if jupiter and "public" in m.mods:
            add(m.start, "S5786", f"test method '{m.name}' should be package-private (drop 'public')")
        if anns.get("Test") and re.search(r"\bexpected\s*=", anns.get("Test") or ""):
            add(m.start, "S5777", "@Test(expected=...): use assertThatThrownBy / assertThrows")
        n = len(COUNT_CALL.findall(body))
        if n > opts.max_assertions:
            add(m.start, "S5961", f"test '{m.name}' has {n} assertions (max {opts.max_assertions})")
    for m in methods:
        if jupiter and m.has(*LIFECYCLE) and "public" in m.mods:
            add(m.start, "S5786", f"lifecycle method '{m.name}' should be package-private")
        if not m.has(*TEST_ANN) and not code[m.open + 1:m.close].strip() and not any(
                o > m.open and o < m.close for o, _ in src.comments):
            add(m.start, "S1186", f"empty method '{m.name}' (add a comment saying why, or remove it)")
        for a, arg in m.anns:
            if a in ("Disabled", "Ignore") and not (arg and arg.strip()):
                add(m.start, "S1607", f"@{a} on '{m.name}' without a reason")
    for o, cnt in by_class.items():
        kinds = Counter(a for m in cnt for a, _ in m.anns if a in LIFECYCLE)
        for a, k in kinds.items():
            if k > 1:
                add(o, "S8745", f"{k} @{a} methods in one class")

    # ---- classes
    def has_tests(k):
        return any(k.open < m.open < k.close for m in tests)

    top = [k for k in classes if src.parent.get(k.open, -1) == -1]
    for k in classes:
        is_top = k in top
        if has_tests(k):
            if is_top and not CLASS_RE.match(k.name) and "abstract" not in k.mods:
                add(k.start, "S3577", f"test class '{k.name}' does not match the test class naming pattern")
            if is_top and jupiter and "public" in k.mods:
                add(k.start, "S5786", f"test class '{k.name}' should be package-private (drop 'public')")
            own = [m for m in tests if m.extra["class_open"] == k.open]
            if not is_top and own and (not k.has("Nested") or "static" in k.mods):
                add(k.start, "S5790", f"inner test class '{k.name}' should be a non-static @Nested class")
        elif is_top and CLASS_RE.match(k.name) and "abstract" not in k.mods and k.extra["kind"] == "class" \
                and not k.has("Suite", "RunWith", "Cucumber", "AnalyzeClasses"):
            add(k.start, "S2187", f"test class '{k.name}' has no tests")
        for a, arg in k.anns:
            if a in ("Disabled", "Ignore") and not (arg and arg.strip()):
                add(k.start, "S1607", f"@{a} on class '{k.name}' without a reason")
        ext = " ".join(arg or "" for a, arg in k.anns if a in ("ExtendWith", "RunWith"))
        mockito_ext = "MockitoExtension" in ext or "MockitoJUnitRunner" in ext
        kf = [f for f in fields if f.extra["class_open"] == k.open]
        if mockito_ext:
            for f in kf:
                if re.match(r"\s*(Mockito\s*\.\s*)?mock\s*\(", f.extra["init"]):
                    add(f.start, "S9015", f"field '{f.name}' uses mock(); declare it with @Mock")
        if any(f.has("Mock", "Spy", "Captor", "InjectMocks") for f in kf) and not mockito_ext \
                and not k.has("MockitoSettings") and "openMocks" not in code and "initMocks" not in code \
                and not re.search(r"MockitoExtension|MockitoJUnit\.rule", code):
            add(k.start, "S5979", f"class '{k.name}' has @Mock fields but no MockitoExtension / openMocks")

    # ---- imports (S8715, S1128)
    body_code = re.sub(r"(?m)^\s*(import|package)\s+[^;]*;", "", code)
    usage = body_code + "\n".join(t for _, t in src.comments)
    for static, imp in imports:
        off = code.find(imp)
        if jupiter and re.match(r"org\.junit\.Assert\b|junit\.framework\.", imp):
            add(off, "S8715", f"JUnit 4 assertions '{imp}' in a JUnit 5/6 test")
        if imp.endswith(".*"):
            continue
        simple = imp.rsplit(".", 1)[-1]
        if not re.search(r"(?<![\w$])" + re.escape(simple) + r"(?![\w$])", usage):
            add(off, "S1128", f"unused import '{imp}'")
    if re.search(r"\bExpectedException\b", body_code):
        add(body_code.find("ExpectedException"), "S5776", "ExpectedException rule: use assertThatThrownBy / assertThrows")

    # ---- fields / private methods / unused locals
    for f in fields:
        if "private" in f.mods and not f.anns and f.name != "serialVersionUID":
            if len(re.findall(r"(?<![\w$])" + re.escape(f.name) + r"(?![\w$])", code)) <= 1:
                add(f.start, "S1068", f"unused private field '{f.name}'")
    for m in methods:
        if "private" in m.mods and not m.anns:
            if len(re.findall(r"(?<![\w$])" + re.escape(m.name) + r"(?![\w$])", src.text)) <= 1:
                add(m.start, "S1144", f"unused private method '{m.name}'")
            body = code[m.open:m.close]
            for p in split_args(m.extra["params"]):
                pn = re.sub(r"@\w+(\([^)]*\))?", "", p).split()
                if pn and not re.search(r"(?<![\w$])" + re.escape(pn[-1]) + r"(?![\w$])", body):
                    add(m.start, "S1172", f"unused parameter '{pn[-1]}' in '{m.name}'")
    for m in methods:
        body = code[m.open + 1:m.close]
        for d in re.finditer(r"(?<![\w$.])(?:final\s+)?(?:var|[A-Z][\w.]*(?:<[^;=()]*>)?(?:\[\])*|int|long|double|"
                             r"float|boolean|char|byte|short)\s+([a-z_]\w*)\s*(=|;|:)", body):
            name, rest = d.group(1), body[d.end():]
            if d.group(2) == ":":
                rest = body[d.end():]
            if not re.search(r"(?<![\w$])" + re.escape(name) + r"(?![\w$])", rest):
                add(m.open + 1 + d.start(1), "S1481", f"unused local variable '{name}'")

    def enclosing_method(off):
        return next((m for m in methods if m.open < off < m.close), None)

    def uses_once(name, off):
        """Rough stand-in for Sonar's 'symbol has a single usage' test."""
        w = r"(?<![\w$.])" + re.escape(name) + r"(?![\w$])"
        m = enclosing_method(off)
        if m is None:
            return False
        body = code[m.open:m.close]
        if re.search(r"[\w>\]]\s+" + re.escape(name) + r"\s*[=;:]", body):
            return len(re.findall(w, body)) - 1 == 1
        if re.search(w, m.extra["params"]):
            return len(re.findall(w, body)) == 1
        if any(f.name == name for f in fields):
            return len(re.findall(w, code)) - 1 == 1
        return False

    # ---- AssertJ subject + chain checks
    for s, o, c, name in src.calls(r"assertThat"):
        arg = code[o + 1:c].strip()
        ch, _ = src.chain(c)
        chs = re.sub(r"\s+", "", ch)
        if not chs:
            if not re.search(r"(=|return|\(|,|->)\s*$", code[max(0, s - 30):s]):
                add(s, "S2970", "assertThat(...) without an assertion method")
            continue
        if re.match(r"^\.(as|describedAs|withFailMessage|overridingErrorMessage)\(\)$", flatten(chs)) or \
                re.search(r"\.(as|describedAs|withFailMessage|overridingErrorMessage)\(\)$", flatten(chs)) and \
                re.search(r"\.(is|has|contains|matches|starts|ends|satisfies|returns)\w*\(", chs):
            add(s, "S5833", "description/failure message set after the assertion; put .as(...) first")
        a = re.sub(r"\s+", "", arg)
        if re.search(r"\.(size|length)\(\)$|\.length$", a) and re.match(r"\.isEqualTo\(", chs):
            add(s, "S5838", "use hasSize(n) instead of size()).isEqualTo(n)")
        elif re.search(r"\.(size|length)\(\)$", a) and re.match(r"\.is(Zero|GreaterThan\w*|LessThan\w*|Positive)\(", chs):
            add(s, "S5838", "use isEmpty()/hasSizeGreaterThan(...) on the collection itself")
        if re.search(r"\.(isEmpty|isBlank|isPresent|isNotEmpty|contains\w*|startsWith|endsWith|matches|equals|"
                     r"containsKey|containsValue|isInstance|before|after)\(.*\)$", a) and \
                re.match(r"\.is(True|False)\(\)", chs):
            add(s, "S5838", "assert on the object with the dedicated AssertJ method, not on a boolean")
        if re.match(r"\.is(True|False)\(\)", chs) and re.search(r"[^=!<>](==|!=|<=|>=|<|>)[^=]",
                                                              a.replace("->", "")):
            add(s, "S5838", "comparison inside assertThat(...).isTrue(); use isEqualTo/isNull/isGreaterThan")
        if re.match(r"\.isEqualTo\(null\)", chs):
            add(s, "S5838", "use isNull() instead of isEqualTo(null)")
        if re.match(r"\.isNotEqualTo\(null\)", chs):
            add(s, "S5838", "use isNotNull() instead of isNotEqualTo(null)")
        if re.match(r"\.isEqualTo\((true|false)\)", chs):
            add(s, "S5838", "use isTrue()/isFalse() instead of isEqualTo(true/false)")
        if LITERAL.match(arg):
            exp = re.match(r"\.(isEqualTo|isNotEqualTo|isSameAs)\((.*)\)$", chs)
            if exp and not LITERAL.match(exp.group(2)):
                add(s, "S3415", "literal passed as the actual value; assertThat(actual).isEqualTo(expected)")
        if a and re.match(r"\.(isEqualTo|isSameAs)\(" + re.escape(a) + r"\)$", chs):
            add(s, "S5863", "object compared to itself")
        names = chain_methods(ch)
        for t, nm in enumerate(names):
            if not re.match(r"^(allMatch|allSatisfy|doesNotContain|doesNotContainSequence|doesNotContainSubsequence|"
                            r"doesNotContainAnyElementsOf)$", nm):
                continue
            if all(NOT_SIZE.match(x) for x in names[:t]) and all(NOT_SIZE.match(x) for x in names[t + 1:]):
                if nm == "doesNotContain" and re.search(r'\.doesNotContain\(\s*"', chs):
                    continue        # String assert, not an iterable
                root = re.match(r"\s*(?:\(\s*[\w.<>]+\s*\)\s*)?([A-Za-z_$][\w$]*)", arg)
                if root and root.group(1) not in ("this", "super") and uses_once(root.group(1), s):
                    add(s, "S5841", f"{nm} passes on an empty collection; add isNotEmpty()/hasSize() first")
            break
        for sm in re.finditer(r"\.satisfies\(", ch):
            po = c + 1 + sm.end() - 1
            if po in src.pairs and not ASSERT_CALL.search(code[po:src.pairs[po]]) and \
                    "assertThat" not in code[po:src.pairs[po]]:
                add(s, "S6103", "satisfies(...) consumer has no assertion")

    # ---- consecutive assertThat on the same subject (S5853): Sonar only joins subjects that
    # always return the same value (no method call or 'new' in them) and resets on statements
    # whose chain changes the subject (extracting, using..., filtered..., map, element ...)
    for m in tests:
        segs, last = [], m.open + 1
        k = m.open + 1
        while k < m.close:
            ch = code[k]
            if ch in "([":
                k = src.pairs.get(k, k) + 1
                continue
            if ch == "{":
                k = src.pairs.get(k, k) + 1
                segs.append((last, k, False))
                last = k
                continue
            if ch == ";":
                segs.append((last, k, True))
                last = k + 1
            k += 1
        prev, reported = None, False
        for a, b, simple in segs:
            st = code[a:b].strip()
            subj = None
            if simple and re.match(r"assertThat\s*\(", st):
                o = code.index("(", a + code[a:b].find("assertThat"))
                ch, _ = src.chain(src.pairs.get(o, o))
                if not any(SET_CONTEXT.match(x) for x in chain_methods(ch)) and chain_methods(ch):
                    subj = re.sub(r"\s+", "", code[o + 1:src.pairs.get(o, o)])
                    if "(" in subj or "->" in subj:
                        subj = None
            if subj and subj == prev:
                if not reported:
                    add(first, "S5853", f"several assertThat({subj}) in a row; chain them into one assertion")
                    reported = True
            else:
                prev, reported = subj, False
                first = a + (len(code[a:b]) - len(code[a:b].lstrip()))

    # ---- exceptions
    # S5958 and S5778 (runtime exception, type known from X.class as Sonar requires)
    for s, o, c, name in src.calls(r"assertThrows|assertThatThrownBy|assertThatCode|assertThatExceptionOfType|"
                                   r"catchThrowableOfType"):
        args = split_args(code[o + 1:c])
        ch, _ = src.chain(c)
        if name == "assertThatThrownBy" and not ch.strip():
            add(s, "S5958", "assertThatThrownBy used alone; chain isInstanceOf(...) / hasMessage(...)")
        exc, lam = None, None
        if name == "assertThrows" and len(args) >= 2:
            exc, lam = (args[1], args[2]) if len(args) == 3 and LITERAL.match(args[0]) else (args[0], args[1])
        elif name in ("assertThatThrownBy", "assertThatCode") and args:
            im = re.search(r"\.(isInstanceOf|isExactlyInstanceOf|isOfAnyClassIn|isInstanceOfAny)\s*\(([^()]*)\)", ch)
            exc, lam = (im.group(2) if im else None), args[0]
        elif name == "assertThatExceptionOfType" and args:
            im = re.search(r"\.isThrownBy\s*\(", ch)
            if im:
                po = c + 1 + im.end() - 1
                exc, lam = args[0], code[po + 1:src.pairs.get(po, po)]
        elif name == "catchThrowableOfType" and len(args) == 2:
            exc, lam = args[1], args[0]
        if not exc or not lam or "->" not in lam:
            continue
        types = [t.strip() for t in exc.split(",")]
        if not any(re.match(r"^[A-Z]\w*\.class$", t) and t[:-6] not in CHECKED for t in types):
            continue
        n = count_invocations(lam.split("->", 1)[1])
        if n > 1:
            add(s, "S5778", f"{n} calls that may throw inside the {name} lambda; build the arguments in local "
                            f"variables before it so only the call under test is inside")
    for t in re.finditer(r"(?<![\w$])try\s*\{", code):
        o = t.end() - 1
        c = src.pairs.get(o)
        if c and re.search(r"(?<![\w$])fail\w*\s*\(", code[o:c]) and re.match(r"\s*catch\b", code[c + 1:]):
            add(t.start(), "S8714", "try { ...; fail(); } catch: use assertThatThrownBy / assertThrows")
    for a in re.finditer(r"(?:^|[;{}])\s*(assert)\s+[^;=]", code, re.M):
        add(a.start(1), "S9427", "Java 'assert' statement in a test; use an assertion library")

    # ---- Mockito
    for mm in re.finditer(r"(?<![\w$])(Mockito|BDDMockito|ArgumentMatchers)\s*\.\s*(\w+)\s*\(", code):
        add(mm.start(), "S8924", f"static-import {mm.group(2)}() instead of {mm.group(1)}.{mm.group(2)}()")
    def qualified(s):
        """True when the call is x.name(...) on something other than Mockito/BDDMockito."""
        q = re.search(r"([\w$]+)\s*\.\s*$", code[max(0, s - 40):s])
        return bool(q) and q.group(1) not in ("Mockito", "BDDMockito")

    for s, o, c, name in src.calls(r"when|given"):
        if qualified(s):
            continue
        ch, _ = src.chain(c)
        if not ch.strip():
            add(s, "S9017", f"{name}(...) without thenReturn/willReturn/...")
    for s, o, c, name in src.calls(r"verify"):
        if qualified(s):
            continue
        ch, _ = src.chain(c)
        if not ch.strip():
            add(s, "S2970", "verify(mock) without the verified method call")
    for s, o, c, name in src.calls(r"should"):
        ch, _ = src.chain(c)
        if s > 0 and code[s - 1] == "." and not ch.strip():
            add(s, "S2970", "then(mock).should() without the verified method call")
    for s, o, c, name in src.calls(r"thenReturn|willReturn|doReturn"):
        if re.search(r"(?<![\w$.])(Mockito\s*\.\s*)?mock\s*\(", code[o:c]):
            add(s, "S9016", f"mock(...) created inside {name}(...); create it first")
    ctx = list(src.calls(r"when|given")) + [(s, o, c, n) for s, o, c, n in src.calls(r"verify|should")]
    seen = set()
    for s, o, c, name in ctx:
        region = code[o + 1:c] if name in ("when", "given") else src.chain(c)[0]
        base = o + 1 if name in ("when", "given") else c + 1
        for cm in re.finditer(r"\.\s*(\w+)\s*\(", region):
            po = base + cm.end() - 1
            if po not in src.pairs or po in seen:
                continue
            seen.add(po)
            args = split_args(code[po + 1:src.pairs[po]])
            if not args:
                continue
            ms = [bool(MATCHER.match(x)) for x in args]
            if all(re.match(r"eq\s*\(", x) for x in args):
                add(s, "S6068", f"every argument of {cm.group(1)}(...) is eq(...); pass the values directly")
            elif any(ms) and not all(ms):
                add(s, "S6073", f"{cm.group(1)}(...) mixes matchers and raw values; wrap the raw ones in eq(...)")

    # ---- stability
    for mm in re.finditer(r"(?<![\w$])(Thread\s*\.\s*sleep|TimeUnit\s*\.\s*\w+\s*\.\s*sleep)\s*\(", code):
        add(mm.start(), "S2925", "Thread.sleep in a test; use a fixed Clock or Awaitility")
    for mm in re.finditer(r"(?<![\w$])(new\s+Random\s*\(|Math\s*\.\s*random\s*\(|UUID\s*\.\s*randomUUID\s*\(|"
                          r"ThreadLocalRandom\b|RandomStringUtils\b|SecureRandom\s*\()", code):
        add(mm.start(), "S5977", "random test data; use fixed values")
    for mm in re.finditer(r"(?<![\w$])((?:Instant|LocalDate|LocalDateTime|LocalTime|ZonedDateTime|OffsetDateTime|"
                          r"YearMonth|Year)\s*\.\s*now\s*\(\s*\)|System\s*\.\s*currentTimeMillis\s*\(|"
                          r"new\s+Date\s*\(\s*\))", code):
        add(mm.start(), "S8692", "system clock in a unit test; use Clock.fixed(...) or a fixed value")

    # ---- similar tests -> parameterized (S5976)
    groups = defaultdict(list)
    lit = re.compile(r'"[^"\n]*"|\'[^\'\n]*\'|(?<![\w$])-?\d[\w.]*|\b(true|false)\b')
    for m in tests:
        if not m.has("Test"):
            continue
        body = code[m.open:m.close]
        values = [x.group(0) for x in lit.finditer(src.text[m.open:m.close])]
        groups[re.sub(r"\s+", " ", lit.sub("$", body))].append((m, values))
    for key, ms in groups.items():
        if len(ms) >= 3 and "$" in key:
            cols = list(zip(*[v for _, v in ms])) if len({len(v) for _, v in ms}) == 1 else []
            diff = sum(1 for col in cols if len(set(col)) > 1)
            if cols and 0 < diff < 4:
                add(ms[0][0].start, "S5976", f"{len(ms)} tests ({', '.join(x.name for x, _ in ms)}) differ only "
                                             f"by values; make one @ParameterizedTest")

    # ---- comments
    for off, t in src.comments:
        if re.search(r"\bTODO\b", t):
            add(off, "S1135", "TODO comment")
        if t.startswith("//"):
            body = t[2:].strip()
            if re.search(r"[;{}]\s*$", body) or re.match(r"(@Test|import |assertThat\(|verify\()", body):
                add(off, "S125", "commented-out code")
        if "NOSONAR" in t:
            add(off, "NOSONAR", "NOSONAR hides a finding; fix the test instead (needs a human if kept)")
    for mm in re.finditer(r'@SuppressWarnings\s*\(([^)]*)\)', src.text):
        if re.search(r'"(java|squid):S\d+', mm.group(1)):
            add(mm.start(), "NOSONAR", "Sonar rule suppressed with @SuppressWarnings; fix the test instead")
    return issues


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("paths", nargs="*")
    ap.add_argument("--project", default=".")
    ap.add_argument("--profile", choices=["skill", "sonar-way"], default="skill")
    ap.add_argument("--max-assertions", type=int, default=25)
    ap.add_argument("--only", default="")
    ap.add_argument("--quiet", action="store_true", help="summary only")
    ap.add_argument("--changed", action="store_true", help="only tests new/changed since guard.sh snapshot")
    opts = ap.parse_args()
    opts.only = {x.strip() for x in opts.only.split(",") if x.strip()}
    paths = opts.paths or [os.path.join(opts.project, "src", "test", "java")]
    files = []
    for p in paths:
        if os.path.isfile(p):
            files.append(p)
        elif os.path.isdir(p):
            for root, _, names in os.walk(p):
                files += [os.path.join(root, n) for n in names if n.endswith(".java")]
        else:
            print(f"not found: {p}", file=sys.stderr)
            return 2
    if opts.changed:
        state = os.path.join(opts.project, "test-quality", ".guard.sha256")
        if not os.path.isfile(state):
            print("--changed needs a snapshot: run guard.sh snapshot first", file=sys.stderr)
            return 2
        import hashlib
        known = {}
        with open(state) as fh:
            for ln in fh:
                parts = ln.rstrip("\n").split(None, 1)
                if len(parts) == 2:
                    known[os.path.normpath(parts[1])] = parts[0]

        def changed(f):
            rel = os.path.normpath(os.path.relpath(f, opts.project))
            with open(f, "rb") as fh:
                return known.get(rel) != hashlib.sha256(fh.read()).hexdigest()
        files = [f for f in files if changed(f)]
        if not files:
            print("no new or changed test files since the snapshot\nRESULT: PASS")
            return 0
    if not files:
        print("no test sources found", file=sys.stderr)
        return 2
    issues = []
    for f in sorted(files):
        with open(f, encoding="utf-8", errors="replace") as fh:
            issues += lint_file(os.path.relpath(f, opts.project), fh.read(), opts)
    issues.sort(key=lambda x: (x[0], x[1], x[2]))
    if not opts.quiet:
        for path, ln, rule, msg in issues:
            print(f"{path}:{ln} {rule} {msg}")
    counts = Counter(r for _, _, r, _ in issues)
    scope = "new/changed test files" if opts.changed else "files"
    print(f"\nsonar_test_lint ({opts.profile} profile, approximation of SonarQube): "
          f"{len(files)} {scope}, {len(issues)} issues")
    for r, n in sorted(counts.items()):
        print(f"  {r}: {n}")
    print("RESULT:", "PASS" if not issues else "FAIL")
    return 0 if not issues else 1


if __name__ == "__main__":
    sys.exit(main())
