# SonarQube rules for test code

Source: rule metadata and the "Sonar way" profile in SonarSource/sonar-java (master, read
2026-11-26). Rules with scope `Tests` run only on test sources; rules with scope `All` run on both.
Rules with scope `Main` (for example S100 method naming, S1192 duplicated literals, S112 generic
exceptions) do **not** apply to tests, so `snake_case` test method names are fine under Sonar way.

Every test this skill writes must have no issue from the rules below. `scripts/sonar_test_lint.py`
checks the mechanical ones (`--changed` limits it to the tests written since `guard.sh snapshot`);
it follows the sonar-java check logic where it was read from source, but without type information it
approximates SonarQube and does not replace a real scan. If the
team's quality profile differs from Sonar way, follow the team's profile (ask for its export).

## Sonar way rules that shape how tests are written

**Assertions**

| Rule | Title (short) | What to do |
|---|---|---|
| S2699 | Tests should include assertions (Blocker) | Every test calls an assertion. Recognised: JUnit `assert*`, AssertJ `assertThat(...).isX()`, `fail`, Mockito `verify`, BDDMockito `then(..).should()`, MockMvc `andExpect`, and any method whose name starts with `assert`, `verify`, `should`, `check`, `expect`, `validate`, `fail`. Helper methods: name them `assert...` or `verify...` |
| S2970 | Assertions should be complete (Blocker) | No bare `assertThat(x);`, no `verify(mock);` without the method, no `assertThatThrownBy(..);` alone |
| S5958 | `assertThatThrownBy` should not be used alone | Chain `.isInstanceOf(...)` and `.hasMessage(...)` |
| S5838 | Use the dedicated AssertJ assertion | `hasSize(2)` not `size()).isEqualTo(2)`; `isEmpty()` not `isEmpty()).isTrue()`; `isNull()` not `isEqualTo(null)`; `contains(x)` not `contains(x)).isTrue()`; `isTrue()` not `isEqualTo(true)`; `isEqualTo(b)` not `assertThat(a.equals(b)).isTrue()` |
| S5853 | Chain consecutive `assertThat` on the same subject | `assertThat(order).isNotNull(); assertThat(order).hasFieldOrProperty("id");` → one chain. Sonar joins only subjects without a method call or `new` (a variable or field), and a statement whose chain uses `extracting...`, `using...`, `filtered...`, `map`, `element` starts over |
| S5841 | `allMatch`, `allSatisfy`, `doesNotContain...` also test emptiness | `assertThat(list).isNotEmpty().allMatch(...)` or `hasSize(n)` first. Raised when nothing else in the chain tests the size and the variable is not used anywhere else |
| S6103 | `satisfies(Consumer)` must assert inside the consumer | |
| S3415 | Argument order | `assertThat(actual).isEqualTo(expected)`: never a literal as the subject; JUnit `assertEquals(expected, actual)` |
| S5845 | Incompatible types in assertions | do not compare `BigDecimal` with `String` via `isEqualTo`; use `isEqualByComparingTo("1.00")` |
| S5863 | Do not compare an object to itself | |
| S5833 / S5831 | AssertJ context (`as`, `withFailMessage`) before the assertion; configuration applied | |
| S5961 | At most 25 assertions per test method (parameter `MaximumAssertionNumber`) | split the test, or parameterize |
| S9427 | No Java `assert` statements in tests | |
| S8715 | No JUnit 4 `org.junit.Assert` in JUnit 5/6 tests | |

**Exceptions**

| Rule | What to do |
|---|---|
| S5778 / S5783 | The lambda of `assertThrows(X.class, ...)`, `assertThatThrownBy(...).isInstanceOf(X.class)`, `assertThatExceptionOfType(X.class).isThrownBy(...)` contains one call that can throw. Constructors and helper calls count too (`new BigDecimal("1")`, `List.of(item(..))`, `request("x")`); only `fail`, `mock(X.class)`, no-arg `List.of()`/`Set.of()`/`Map.of()`, `Collections.empty*`/`singleton*`, `Optional.empty()` and no-arg collection constructors are free. Build everything else in local variables first: `var req = request("x"); assertThatThrownBy(() -> service.place(req))` |
| S8714 | No `try { ...; fail(); } catch (...)`; use `assertThatThrownBy` / `assertThrows` |
| S5777 / S5776 | No `@Test(expected = ...)`, no `ExpectedException` rule (JUnit 4 styles) |
| S5779 | No assertions inside `try` that catches `Error` |

**JUnit 5/6 structure**

| Rule | What to do |
|---|---|
| S5786 | Test classes and methods package-private: `class FooTest`, `void cancelsOrder()`, no `public` |
| S3577 | Class name matches `^((Test\|IT)[a-zA-Z0-9_]+\|[A-Z][a-zA-Z0-9_]*(Test\|Tests\|TestCase\|IT\|ITCase))$` |
| S2187 | A test class contains at least one test |
| S5790 | Inner test classes are `@Nested` (non-static) |
| S5810 | Test methods are not `private`/`static` and return `void`, so JUnit does not skip them silently |
| S5967 | Never `@Test` together with `@ParameterizedTest` / `@RepeatedTest` |
| S8745 | One lifecycle method of each type (`@BeforeEach` ...) per class |
| S1607 | `@Disabled("reason")` always with a reason; better, do not leave disabled tests |
| S5976 | Tests that differ only in values become one `@ParameterizedTest` |

**Mockito**

| Rule | What to do |
|---|---|
| S9015 | With `MockitoExtension`, declare mocks as `@Mock` fields, not `mock(...)` calls |
| S9016 | No `mock(...)` inside `thenReturn(...)`; create the mock first |
| S9017 | Stubbing complete: `when(x)` / `given(x)` always followed by `thenReturn`/`willReturn`/... |
| S6068 | Do not wrap every argument in `eq(...)`; pass the values directly |
| S8924 | Static imports for Mockito core methods (`when`, `verify`, `mock`, `given`, `then`) |
| S5969 | Do not mock every non-private method of a class (use a real object or a fake) |

**Stability and Spring**

| Rule | What to do |
|---|---|
| S2925 | No `Thread.sleep` in tests; use a fixed `Clock` or Awaitility |
| S5973 | Tests must be stable: no dependence on time, order or environment |
| S7177 | `@DirtiesContext` with the right mode; avoid it in unit tests altogether |

**General rules that also run on tests (scope All)**: S1128 unused imports, S1481 unused local
variables, S1068 unused private fields, S1144 unused private methods, S1172 unused parameters,
S1186 empty methods, S125 commented-out code, S1135 TODO comments.

## Not in Sonar way, but follow them anyway (they cost nothing and help mutation testing)

| Rule | What to do |
|---|---|
| S6073 | Matchers on all arguments or on none: `given(repo.find(eq(id), any()))` |
| S5977 | Fixed data, not random values |
| S8692 | No system clock in unit tests: inject `Clock.fixed(...)` |
| S5979 | Annotated mocks initialised (`@ExtendWith(MockitoExtension.class)`) |

Off by default and left off unless the team's profile enables them: S2698 (assertion messages),
S3578 (test method naming, default `^test[A-Z][a-zA-Z0-9]*$`), S9024 (`@InjectMocks` instead of
constructing the object under test; if enabled, use `@InjectMocks` when every dependency is a mock).

## Where Sonar and mutation testing meet

- S5976 (parameterize) and boundary testing for ConditionalsBoundary mutants point the same way:
  one `@ParameterizedTest` with below / at / above rows.
- S5961 (at most 25 assertions) keeps tests focused; asserting every field of a large response goes
  through `usingRecursiveComparison()` against an expected object, which is one assertion.
- S5778 forces arguments out of the exception lambda, which also makes it clear which call throws.
- S5838 dedicated assertions (`hasSize`, `containsExactly`) are also the ones that kill
  EmptyObjectReturnVals and collection mutants; `isNotEmpty()` alone kills neither.
