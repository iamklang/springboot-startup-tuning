package CHANGE_ME;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpRequest.BodyPublishers;
import java.net.http.HttpResponse.BodyHandlers;
import java.time.Duration;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.ApplicationArguments;
import org.springframework.boot.ApplicationRunner;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.boot.context.properties.bind.Bindable;
import org.springframework.boot.context.properties.bind.Binder;
import org.springframework.core.env.Environment;
import org.springframework.stereotype.Component;

/**
 * Calls this instance's own endpoints over loopback HTTP before it accepts traffic.
 *
 * Spring Boot publishes ReadinessState.ACCEPTING_TRAFFIC only after every ApplicationRunner has
 * returned, and the web server is already listening, so these calls run the whole request path
 * (filters, MVC, Jackson, validation, JPA) while readiness still answers 503. One to three rounds
 * are enough: the costs removed are one-time (class loading, per-type caches, query plans).
 *
 * Only side-effect-free requests: GETs, and POSTs to validation-only or dry-run endpoints.
 * Failures are logged, never thrown: a failed warm-up must not fail startup. Requests carry
 * X-Warmup: TOKEN, a random per-JVM value that WarmupObservationFilter uses to keep them out of
 * metrics and traces.
 *
 * app.warmup.enabled=true                         off when missing
 * app.warmup.paths=/api/orders/1,/api/orders?x=1  GETs; server.servlet.context-path is prepended
 * app.warmup.post[/api/quote]={"sku":"a","qty":1} optional POSTs (YAML: "[/api/quote]": '{...}')
 * app.warmup.iterations=3                         rounds over all requests
 * app.warmup.max-duration=20s                     hard budget; keep it inside the startupProbe
 */
@Component
@ConditionalOnProperty(name = "app.warmup.enabled", havingValue = "true")
public class WarmupRunner implements ApplicationRunner {

  private static final Logger log = LoggerFactory.getLogger(WarmupRunner.class);

  public static final String TOKEN = UUID.randomUUID().toString();

  private final Environment env;

  public WarmupRunner(Environment env) {
    this.env = env;
  }

  @Override
  public void run(ApplicationArguments args) {
    String port = env.getProperty("local.server.port");
    if (port == null || env.getProperty("server.ssl.enabled", Boolean.class, false)) {
      log.info("warm-up skipped: {}", port == null ? "no embedded web server" : "server uses TLS");
      return;
    }
    Binder binder = Binder.get(env);
    Map<String, String> requests = new LinkedHashMap<>();   // path -> POST body, null for GET
    binder.bind("app.warmup.paths", Bindable.listOf(String.class)).orElse(List.of())
        .forEach(p -> requests.put(p, null));
    requests.putAll(binder.bind("app.warmup.post", Bindable.mapOf(String.class, String.class)).orElse(Map.of()));
    if (requests.isEmpty()) {
      log.warn("warm-up enabled but app.warmup.paths and app.warmup.post are empty");
      return;
    }
    int iterations = env.getProperty("app.warmup.iterations", Integer.class, 3);
    long deadline = System.nanoTime() + binder.bind("app.warmup.max-duration", Duration.class)
        .orElse(Duration.ofSeconds(20)).toNanos();
    String host = env.getProperty("server.address", "127.0.0.1").replace("0.0.0.0", "127.0.0.1");
    String base = "http://" + host + ":" + port + env.getProperty("server.servlet.context-path", "");
    HttpClient client = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(2)).build();

    long start = System.nanoTime();
    int sent = 0, failed = 0, non2xx = 0;
    outer:
    for (int i = 0; i < iterations; i++) {
      for (Map.Entry<String, String> r : requests.entrySet()) {
        if (System.nanoTime() > deadline) {
          log.warn("warm-up stopped at app.warmup.max-duration");
          break outer;
        }
        sent++;
        try {
          HttpRequest.Builder b = HttpRequest.newBuilder(URI.create(base + r.getKey()))
              .timeout(Duration.ofSeconds(5)).header("X-Warmup", TOKEN);
          if (r.getValue() != null) {
            b.header("Content-Type", "application/json").POST(BodyPublishers.ofString(r.getValue()));
          }
          int status = client.send(b.build(), BodyHandlers.discarding()).statusCode();
          if (status >= 500 && failed++ == 0) {
            log.warn("warm-up {} returned {}", r.getKey(), status);
          } else if (status < 500 && (status < 200 || status >= 300) && non2xx++ == 0) {
            // 401/403 = needs auth, 404 = wrong path or missing id, 400 = bad body: only part of the path is warmed
            log.warn("warm-up {} returned {}; it warms only part of the path", r.getKey(), status);
          }
        } catch (InterruptedException e) {
          Thread.currentThread().interrupt();
          break outer;
        } catch (Exception e) {
          if (failed++ == 0) {
            log.warn("warm-up {} failed: {}", r.getKey(), e.toString());
          }
        }
      }
    }
    log.info("warm-up done: {} requests, {} failed, {} non-2xx, {} ms", sent, failed, non2xx,
        (System.nanoTime() - start) / 1_000_000);
  }
}
