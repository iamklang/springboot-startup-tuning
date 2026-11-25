package CHANGE_ME;

import io.micrometer.observation.ObservationPredicate;
import org.springframework.boot.autoconfigure.condition.ConditionalOnClass;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.http.server.observation.ServerRequestObservationContext;

/**
 * Keeps WarmupRunner's self-requests out of http.server.requests metrics and out of traces.
 *
 * Spring Boot applies every ObservationPredicate bean to the ObservationRegistry. Returning false
 * for a server request makes it a no-op observation: no timer sample, no span. Only requests whose
 * X-Warmup header equals this JVM's random token are dropped, so a client cannot hide its own
 * traffic by sending the header (a loopback-address check would not be enough: behind an Istio or
 * Envoy sidecar every request arrives from 127.0.0.1).
 *
 * Servlet stack only (Spring MVC). WebFlux uses
 * org.springframework.http.server.reactive.observation.ServerRequestObservationContext instead.
 * Tomcat's access log, if enabled, still records the requests.
 */
@Configuration(proxyBeanMethods = false)
@ConditionalOnClass(ObservationPredicate.class)
@ConditionalOnProperty(name = "app.warmup.enabled", havingValue = "true")
public class WarmupObservationFilter {

  @Bean
  ObservationPredicate ignoreWarmupRequests() {
    return (name, context) -> !(context instanceof ServerRequestObservationContext server
        && WarmupRunner.TOKEN.equals(server.getCarrier().getHeader("X-Warmup")));
  }
}
