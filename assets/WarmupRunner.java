package CHANGE_ME;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.time.Instant;
import java.util.List;
import java.util.UUID;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.ApplicationArguments;
import org.springframework.boot.ApplicationRunner;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.core.env.Environment;
import org.springframework.stereotype.Component;

/**
 * Sends real HTTP requests to this instance's own endpoints before it accepts traffic.
 *
 * Why an ApplicationRunner: Spring Boot publishes ReadinessState.ACCEPTING_TRAFFIC only after
 * all runners return, while the embedded server is already listening. So requests sent here
 * reach the full stack (filters, MVC, Jackson, validation, JPA, JIT) while the readiness probe
 * still answers 503 and no user request is routed to the pod. Liveness is already CORRECT, so
 * keep the warm-up well inside the startupProbe budget.
 *
 * Every request carries the header X-Warmup with a random token created at JVM start (TOKEN).
 * WarmupObservationFilter.java uses it to keep warm-up calls out of http.server.requests and
 * traces; a fixed value such as "true" could be sent by any client to hide its own requests.
 *
 * Iterations: the costs a warm-up removes are mostly one-time (class loading, Jackson and
 * validation caches, Hibernate plans, MVC init). Measured on a Boot 3.5 demo, one round brought
 * request 1 from 26/14/49 ms to 3.1/3.0/1.6 ms; twenty rounds only reached 1.7/1.9/1.1 ms while
 * adding 20x the self-traffic. Start at 1 to 3 and raise it only if measurement shows a gain.
 *
 * Rules: only idempotent requests (GET, or POSTs to endpoints with no side effects such as
 * validation-only or dry-run endpoints). Never call other services or write data. Failures are
 * logged and swallowed: a failed warm-up must never fail startup.
 */
@Component
@ConditionalOnProperty(name = "app.warmup.enabled", havingValue = "true", matchIfMissing = false)
public class WarmupRunner implements ApplicationRunner {

  private static final Logger log = LoggerFactory.getLogger(WarmupRunner.class);

  /** Per-JVM secret sent in the X-Warmup header; read by WarmupObservationFilter. */
  public static final String TOKEN = UUID.randomUUID().toString();

  private final Environment env;
  private final List<String> paths;
  private final int iterations;
  private final Duration budget;

  public WarmupRunner(Environment env,
      @Value("${app.warmup.paths:/actuator/health}") List<String> paths,
      @Value("${app.warmup.iterations:3}") int iterations,
      @Value("${app.warmup.max-duration:20s}") Duration budget) {
    this.env = env;
    this.paths = paths;
    this.iterations = iterations;
    this.budget = budget;
  }

  @Override
  public void run(ApplicationArguments args) {
    String port = env.getProperty("local.server.port");
    if (port == null) {
      log.info("warm-up skipped: no embedded web server port");
      return;
    }
    HttpClient client = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(2)).build();
    Instant deadline = Instant.now().plus(budget);
    Instant start = Instant.now();
    int sent = 0;
    int failed = 0;
    int non2xx = 0;
    outer:
    for (int i = 0; i < iterations; i++) {
      for (String path : paths) {
        if (Instant.now().isAfter(deadline)) {
          break outer;
        }
        try {
          HttpRequest req = HttpRequest.newBuilder(URI.create("http://127.0.0.1:" + port + path))
              .timeout(Duration.ofSeconds(5))
              .header("X-Warmup", TOKEN)
              .GET()
              .build();
          HttpResponse<Void> res = client.send(req, HttpResponse.BodyHandlers.discarding());
          int status = res.statusCode();
          if (status >= 500) {
            failed++;
            if (failed == 1) {
              log.warn("warm-up request to {} returned {}", path, status);
            }
          } else if (status < 200 || status >= 300) {
            // 401/403: endpoint needs auth; 404: wrong path or missing ID. Either way it warms
            // only part of the request path.
            non2xx++;
            if (non2xx == 1) {
              log.warn("warm-up request to {} returned {}; it warms only part of the path", path, status);
            }
          }
        } catch (Exception e) {
          failed++;
          if (failed == 1) {
            log.warn("warm-up request to {} failed: {}", path, e.toString());
          }
        }
        sent++;
      }
    }
    log.info("warm-up done: {} requests, {} failed, {} non-2xx, {} ms", sent, failed, non2xx,
        Duration.between(start, Instant.now()).toMillis());
  }
}
