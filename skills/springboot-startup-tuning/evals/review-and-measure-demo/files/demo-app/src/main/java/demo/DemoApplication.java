package demo;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.boot.context.metrics.buffering.BufferingApplicationStartup;

@SpringBootApplication
public class DemoApplication {
  public static void main(String[] args) {
    SpringApplication app = new SpringApplication(DemoApplication.class);
    app.setApplicationStartup(new BufferingApplicationStartup(4096));
    app.run(args);
  }
}
