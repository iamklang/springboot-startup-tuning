package demo;

import jakarta.annotation.PostConstruct;
import org.springframework.stereotype.Component;

@Component
public class SlowBean {
  @PostConstruct
  void warm() throws InterruptedException { Thread.sleep(700); }
}
