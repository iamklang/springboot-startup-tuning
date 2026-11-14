package CHANGE_ME;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.time.Instant;
import java.util.List;

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
 * Rules: only idempotent requests (GET, or POSTs to endpoints with no side effects such as
 * validation-only or dry-run endpoints). Never call other services or write data. Failures are
 * logged and swallowed: a failed warm-up must never fail startup.
 */
@Component
@ConditionalOnProperty(name = "app.warmup.enabled", havingValue = "true", matchIfMissing = false)
public class WarmupRunner implements ApplicationRunner {

  private static final Logger log = LoggerFactory.getLogger(WarmupRunner.class);

  private final Environment env;
  private final List<String> paths;
  private final int iterations;
  private final Duration budget;

  public WarmupRunner(Environment env,
      @Value("${app.warmup.paths:/actuator/health}") List<String> paths,
      @Value("${app.warmup.iterations:50}") int iterations,
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
    outer:
    for (int i = 0; i < iterations; i++) {
      for (String path : paths) {
        if (Instant.now().isAfter(deadline)) {
          break outer;
        }
        try {
          HttpRequest req = HttpRequest.newBuilder(URI.create("http://127.0.0.1:" + port + path))
              .timeout(Duration.ofSeconds(5))
              .header("X-Warmup", "true")
              .GET()
              .build();
          HttpResponse<Void> res = client.send(req, HttpResponse.BodyHandlers.discarding());
          if (res.statusCode() >= 500) {
            failed++;
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
    log.info("warm-up done: {} requests, {} failed, {} ms", sent, failed,
        Duration.between(start, Instant.now()).toMillis());
  }
}
