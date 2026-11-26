# Test patterns that cover code and kill mutants

Contents: 1. Imports by Boot version · 2. Which test for which layer · 3. Templates ·
4. Mutator → assertion · 5. Hard survivors · 6. Never write these

## 1. Imports by Boot version

Check the Boot version in the build file first. Wrong packages are the most common compile error.

| Type | Boot 3.4 / 3.5 | Boot 4.x |
|---|---|---|
| `@WebMvcTest` | `org.springframework.boot.test.autoconfigure.web.servlet.WebMvcTest` | `org.springframework.boot.webmvc.test.autoconfigure.WebMvcTest` |
| `@AutoConfigureMockMvc` | `org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc` | `org.springframework.boot.webmvc.test.autoconfigure.AutoConfigureMockMvc` |
| `@DataJpaTest` | `org.springframework.boot.test.autoconfigure.orm.jpa.DataJpaTest` | `org.springframework.boot.data.jpa.test.autoconfigure.DataJpaTest` |
| `@RestClientTest` | `org.springframework.boot.test.autoconfigure.web.client.RestClientTest` | `org.springframework.boot.restclient.test.autoconfigure.RestClientTest` |
| `@JsonTest` | `org.springframework.boot.test.autoconfigure.json.JsonTest` | same |
| `@MockitoBean` | `org.springframework.test.context.bean.override.mockito.MockitoBean` (3.4+) | same; `@MockBean` is removed in 4 |
| `MockMvcTester` | `org.springframework.test.web.servlet.assertj.MockMvcTester` (3.4+) | same |
| Jackson | `com.fasterxml.jackson.databind.ObjectMapper` (Jackson 2) | `tools.jackson.databind.json.JsonMapper` (Jackson 3); annotations stay `com.fasterxml.jackson.annotation` |
| Test starters | `spring-boot-starter-test` has every slice | slices live in per-technology modules: add `spring-boot-starter-webmvc-test`, `-data-jpa-test`, `-restclient-test`, `-jackson-test`, `-security-test` as needed (test scope) |

Boot 3.0 to 3.3: no `@MockitoBean` and no `MockMvcTester`; use `@MockBean` and `MockMvc` with
`MockMvcResultMatchers`. On Boot 4, add the slice's test starter (test scope) when a slice test
needs it, and list it in the report; it does not change production behaviour.

## 2. Which test for which layer

Plain unit tests are the core of the suite: no Spring context, milliseconds per test, and PIT runs
them thousands of times. Use a slice only where the framework behaviour is the thing under test
(request mapping, validation, JSON, error mapping, HTTP client error handling).

| Layer | Test | Notes |
|---|---|---|
| Domain logic, services, policies, calculators | plain JUnit + real collaborators; Mockito only for ports to the outside (repositories, clients, message senders) | mock what crosses the process boundary; real value objects and pure helpers kill more mutants |
| Value objects, records with compact constructors, enums with logic | plain JUnit, every validation branch and its message | |
| Controllers | `@WebMvcTest(XController.class)` + `MockMvcTester` + `@MockitoBean` for services | covers mapping, `@Valid`, JSON and `@RestControllerAdvice` together; `@Import` the advice if it is outside the scanned slice |
| `@RestControllerAdvice` | through the controller test: one request per handled exception, assert status and body | |
| HTTP clients (`RestClient`) | plain test: `RestClient.Builder b = RestClient.builder(); MockRestServiceServer server = MockRestServiceServer.bindTo(b).build();` then build the client from `b` | 2xx, 404, other 4xx, 5xx, IO error, empty body |
| Bean Validation on DTOs | plain `Validator` from `Validation.buildDefaultValidatorFactory()`, values at each limit | or through the controller test |
| `@ConfigurationProperties` records | construct directly for validation logic; `ApplicationContextRunner` only if binding itself has logic | |
| `@Scheduled` jobs | plain test that calls the method and verifies the delegate | the cron string is configuration, not logic |
| Repositories (derived queries) | none in the unit suite; Spring Data generates them | custom `@Query` with logic: `@DataJpaTest`, excluded from PIT if slow |
| JPA entities / Lombok | through the services that use them; add `lombok.config` so generated accessors are not counted | never test getters/setters directly |
| `@Configuration` with plain `@Bean` factory methods | usually nothing to assert; propose an exclusion to the user only if it has no conditions | conditional beans need `ApplicationContextRunner` |

## 3. Templates

The examples use an invoicing domain; adapt names, never copy values.

Service with a fixed clock, real collaborators and a mocked outside port (JUnit 5/6, Mockito 5, AssertJ):

```java
@ExtendWith(MockitoExtension.class)
class InvoiceServiceTest {
  static final Instant NOW = Instant.parse("2026-03-02T10:00:00Z");
  @Mock InvoiceRepository repo;                       // outside the process: mock
  @Mock PaymentGateway gateway;
  Clock clock = Clock.fixed(NOW, ZoneOffset.UTC);
  InvoiceService service;

  @BeforeEach
  void setUp() {
    LateFeePolicy policy = new LateFeePolicy(new BillingProperties(new BigDecimal("0.02"), 30));
    service = new InvoiceService(repo, gateway, policy, clock);   // real policy: its mutants die here too
  }

  @Test
  void void_rejects_invoice_issued_more_than_48_hours_ago() {
    Invoice inv = invoice(InvoiceStatus.OPEN, NOW.minus(Duration.ofHours(48)).minusSeconds(1));
    given(repo.findById(7L)).willReturn(Optional.of(inv));

    assertThatThrownBy(() -> service.voidInvoice(7L))
        .isInstanceOf(IllegalStateException.class)
        .hasMessage("Void window closed for invoice 7");
    assertThat(inv.getStatus()).isEqualTo(InvoiceStatus.OPEN);
  }

  @Test
  void void_allows_invoice_exactly_48_hours_old() {     // the boundary itself
    Invoice inv = invoice(InvoiceStatus.OPEN, NOW.minus(Duration.ofHours(48)));
    given(repo.findById(7L)).willReturn(Optional.of(inv));

    assertThat(service.voidInvoice(7L).getStatus()).isEqualTo(InvoiceStatus.VOID);
  }
}
```

Boundaries with a parameterized test. Expected values are literals worked out by hand, never
recomputed with the production formula:

```java
@ParameterizedTest(name = "{0} days late -> fee {1}")
@CsvSource({"29, 0.00", "30, 2.00", "31, 2.00"})       // below, at, above the grace period
void late_fee_starts_after_grace_period(int daysLate, String expected) {
  assertThat(policy.lateFee(new BigDecimal("100.00"), daysLate)).isEqualByComparingTo(expected);
}
```

Exceptions: everything except the call under test is built before the lambda (SonarQube S5778
counts constructors and calls to helpers too), and the type and message are both asserted (S5958):

```java
@Test
void create_rejects_more_than_100_items() {
  List<InvoiceItem> items = items(101);                 // built outside the lambda
  Customer acme = new Customer("acme");

  assertThatThrownBy(() -> service.create(acme, items))
      .isInstanceOf(IllegalArgumentException.class)
      .hasMessage("an invoice has at most 100 items");
  then(repo).shouldHaveNoInteractions();
}
```

BigDecimal: compare with `isEqualByComparingTo("12.50")`; use `isEqualTo(new BigDecimal("12.50"))`
only when the scale is part of the contract (rounding code), because `setScale` mutants survive
`isEqualByComparingTo`.

Controller slice (Boot 4 imports; Boot 3.4/3.5 differ only in the `WebMvcTest` package):

```java
@WebMvcTest(InvoiceController.class)
@Import(ApiErrors.class)                             // only if the advice is not picked up
class InvoiceControllerTest {
  @Autowired MockMvcTester mvc;
  @MockitoBean InvoiceService invoices;

  @Test
  void create_returns_201_with_location() {
    given(invoices.create(eq("acme"), anyList())).willReturn(savedInvoice(42L));

    assertThat(mvc.post().uri("/api/invoices").contentType(MediaType.APPLICATION_JSON)
            .content("""
                {"customer":"acme","items":[{"code":"X1","amount":10.00}]}
                """))
        .hasStatus(HttpStatus.CREATED)
        .bodyJson().extractingPath("$.id").isEqualTo(42);
  }

  @Test
  void create_rejects_empty_items_with_400() {
    assertThat(mvc.post().uri("/api/invoices").contentType(MediaType.APPLICATION_JSON)
            .content("""
                {"customer":"acme","items":[]}
                """))
        .hasStatus(HttpStatus.BAD_REQUEST);
    then(invoices).shouldHaveNoInteractions();
  }
}
```

`RestClient` wrapper without a Spring context:

```java
RestClient.Builder builder = RestClient.builder().baseUrl("http://rates");
MockRestServiceServer server = MockRestServiceServer.bindTo(builder).build();
ExchangeRateClient client = new ExchangeRateClient(builder.build());

server.expect(requestTo("http://rates/rate/EUR")).andRespond(withStatus(HttpStatus.NOT_FOUND));
assertThat(client.rate("EUR")).isEmpty();
server.verify();
```

Cover each status family the code distinguishes: 200 with a body, 200 with an empty body, 404,
another 4xx, 5xx (`withServerError()`), and an I/O failure (`withException(new IOException("down"))`).

Log output, when logging is the only effect of a branch, is not worth asserting; PIT does not
mutate logging calls by default.

## 4. Mutator → assertion that kills it

PIT `DEFAULTS` group (pitest.org/quickstart/mutators). The report's `mutator` column gives the class name.

| Mutator (report name) | What it changes | Test that kills it |
|---|---|---|
| ConditionalsBoundaryMutator | `<` ↔ `<=`, `>` ↔ `>=` | a case exactly at the boundary, plus one on each side |
| NegateConditionalsMutator | `==` ↔ `!=`, `<` ↔ `>=`, ... | both outcomes of every condition |
| RemoveConditionalMutator_EQUAL_ELSE (and _IF) | condition forced true/false | a case where the condition is false (or true) and the outcome differs |
| MathMutator | `+` ↔ `-`, `*` ↔ `/`, `%` ↔ `*` | asymmetric inputs: avoid 0, 1, 2+2, equal operands |
| IncrementsMutator | `i++` → `i--`, `+= 2` → `-= 2` | assert totals over at least two elements |
| InvertNegsMutator | `-x` → `x` | a non-zero input |
| ReturnValsMutator family: EmptyObjectReturnValsMutator, NullReturnValsMutator, PrimitiveReturnsMutator, BooleanTrueReturnValsMutator, BooleanFalseReturnValsMutator | returns `""`, empty collection, `Optional.empty()`, `null`, `0`, `true`, `false` | assert the exact value (`containsExactly`, `isEqualTo`), never only `isNotNull()`/`isNotEmpty()`; test both `true` and `false` results |
| VoidMethodCallMutator | removes a call to a void method | assert the call's effect: state of the object, `then(mock).should().save(captor.capture())` with assertions on the captured value |

## 5. Hard survivors

- **Void calls on a mock** (`repo.delete(x)`, `publisher.publishEvent(e)`, `entity.setStatus(...)`): the
  test must observe the effect. For setters on a returned entity assert the field; for calls on a mock
  verify the call with the exact argument.
- **Boolean returns** (`isEligible()`): a test where the true answer is `false`, and one where it is `true`.
- **Defaults and fallbacks** (`x == null ? DEFAULT : x`): a test that passes `null` and asserts the default.
- **`Math.max(a, 1)`/`Math.min`**: a case where the clamp changes the result.
- **Rounding** (`setScale(2, HALF_UP)`): an input with a third decimal of 5, asserted with
  `isEqualTo(new BigDecimal("x.yz"))`.
- **Exception messages built from values**: assert the whole message, so a mutated value or a removed
  branch shows.
- **Loops that stop early** (`break`, `return` in a loop): input where the early exit matters, and
  input where it is never reached.
- **A condition that only guards a log call** (`if (!list.isEmpty()) log.info(...)`): removing the
  condition changes nothing but logging. Propose it as an equivalent mutant; assert the log with
  `OutputCaptureExtension` only when that log line is an operational contract (alerts parse it).
- **Mutants in code no test can reach with valid input** (a guard behind another guard): that is a
  candidate equivalent mutant or dead code. Do not contort the test; record it for the user (see
  `mutation-loop.md`).

## 6. Never write these

These raise the numbers without testing anything. They are not allowed, even to reach the target.

- A test without an assertion, or with only `assertDoesNotThrow`, `isNotNull()` where the exact value is
  known, or `verify(mock).x(any())` as the only check.
- Expected values computed with the production formula. Expected values are literals.
- Reflection or bean testers for getters/setters/equals; calling `main()` or `SpringApplication.run`
  to cover the application class.
- `@SpringBootTest` used to "touch" code. The full context belongs in a small number of integration
  tests, run by Failsafe and kept out of PIT.
- Assertions that pin behaviour the Javadoc, method name or requirement says is wrong. Stop and report
  it as a suspected bug instead (SKILL.md, Guardrails).
- Catch-all `try { ... } catch (Exception e) {}` in tests, `Thread.sleep`, real time, real network,
  random data without a fixed seed.
- Several behaviours in one test; near-duplicate tests that kill nothing new.
- Anything `sonar_test_lint.py` reports (`sonar-test-rules.md`), and `// NOSONAR` or
  `@SuppressWarnings("java:S...")` to hide it. The shapes most often written by mistake:
  `public` test classes or methods (S5786), `Mockito.when(...)` instead of a static import (S8924),
  `mock(...)` fields under `MockitoExtension` (S9015), `assertThat(list.size()).isEqualTo(2)` instead
  of `hasSize(2)` (S5838), several `assertThat(x)` in a row on the same variable (S5853), arguments
  built inside an `assertThatThrownBy` lambda (S5778), every argument wrapped in `eq(...)` (S6068),
  three or more tests that differ only by values (S5976).
