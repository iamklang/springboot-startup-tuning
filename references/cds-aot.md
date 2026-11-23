# Build-time startup optimisation: Spring AOT, CDS, JDK AOT cache

These three stack. Spring AOT removes runtime bean-definition work; CDS or the JDK AOT cache
removes class loading/linking work. Together they routinely cut `process running for` by 40 to
60 percent with zero code changes. The costs are a longer image build and strict version
discipline between the training run and production.

## Decide by version

| Boot | JDK | Use |
|---|---|---|
| 3.3 to 3.5 | 17 to 23 | Spring AOT + CDS (`-XX:SharedArchiveFile`) |
| 3.3 to 3.5 | 24+ | Spring AOT + JDK AOT cache (`-XX:AOTCache`); CDS still works but the AOT cache supersedes it |
| 4.x | 25+ | Spring AOT + JDK AOT cache, one-step `-XX:AOTCacheOutput` |
| 3.0 to 3.2 | 17+ | Spring AOT, plus CDS by hand (see "CDS on Boot 3.0 to 3.2" below); recommend upgrading to 3.3+ |
| 2.x | any | Neither; report the upgrade as the real fix |

## Spring AOT (JVM mode)

Maven: the `spring-boot-maven-plugin` has a `process-aot` goal.

```xml
<plugin>
  <groupId>org.springframework.boot</groupId>
  <artifactId>spring-boot-maven-plugin</artifactId>
  <executions>
    <execution>
      <id>process-aot</id>
      <goals><goal>process-aot</goal></goals>
    </execution>
  </executions>
</plugin>
```

Gradle: the Spring Boot plugin registers `processAot` automatically; `bootJar` depends on it when the
`org.graalvm.buildtools.native` plugin is applied, otherwise run `./gradlew processAot bootJar`.

Run with `java -Dspring.aot.enabled=true -jar app.jar`. Verify with the log line
`Starting AOT-processed <App>`; without it the flag was ignored.

What changes at runtime:
- Bean definitions, conditions and profiles are **fixed at build time**. `spring.profiles.active` and `@ConditionalOnProperty` values present during `process-aot` are what prod gets. Pass the production profile to the goal (`<profiles>` in the plugin config or `-Dspring.profiles.active` on the AOT JVM args) or the app fails to start with a bean mismatch.
- `@Profile`-switched beans, `@Conditional` on environment, and `BeanFactoryPostProcessor`s that register beans dynamically may need `RuntimeHints` or restructuring. If the app refuses to start under AOT, the error names the bean; check that first.
- **Property-driven switches stop working.** `spring.flyway.enabled=false`, `spring.jpa.enabled`, `management.*.enabled` and any `@ConditionalOnProperty` toggle are evaluated during `process-aot`. Setting them at runtime or for the training run changes nothing. To skip work only during the training run, keep the bean and make its *behaviour* depend on a property value (see the Flyway pattern below).
- **Cloud platform detection is fixed too.** `@ConditionalOnCloudPlatform(KUBERNETES)` is evaluated at build time, where the build is not on Kubernetes. The liveness/readiness health groups that Boot adds automatically on Kubernetes are then missing and probes on `/actuator/health/liveness` return 404. Set `management.endpoint.health.probes.enabled=true` explicitly in the production config.
- **Spring Cloud.** `@RefreshScope` is not supported under AOT: `process-aot` fails or the refresh behaviour silently stops. Before trying AOT on an app with `spring-cloud-config` (or any `spring-cloud-*-config`), grep for `@RefreshScope` (`project_scan.sh` counts it). If refresh is not used at runtime, set `spring.cloud.refresh.enabled=false` for both `process-aot` and the run; if it is used, skip Spring AOT and do CDS only. `process-aot` runs the main method and prepares the context (without creating the beans), so config imports and the properties that conditions read must resolve at build time, where no config server is reachable; give it the same stub file as the measurement runs (`config_stub.py`) and `spring.cloud.config.enabled=false`.
- **Passing properties to `process-aot`.** Pass them as JVM system properties. In one project `-Dspring-boot.aot.arguments=--key=value` did not reach the app while `-Dspring-boot.aot.jvmArguments="-Dkey=value -Dother=value"` (or `<jvmArguments>` in the `process-aot` execution) did. Gradle: `tasks.named("processAot") { jvmArgs("-Dkey=value") }`. Verify by checking the generated `__BeanDefinitions` sources or the startup log for the profile.
- Dev tools, `@MockBean`-heavy test setups and some older libraries are not AOT-friendly. Keep AOT off for the test classpath unless the project already runs tests that way.

## CDS (Class Data Sharing), Boot 3.3+

1. Extract the jar so the classpath is stable between runs:

```bash
java -Djarmode=tools -jar target/<artifact>-<version>.jar extract --destination target/extracted --application-filename app.jar
```

The extracted jar keeps the original file name unless `--application-filename` is given; every command below assumes `app.jar`.

2. Training run. The app must start fully and then exit; `-XX:ArchiveClassesAtExit` writes the
archive when the JVM exits. Boot's `extract` layout plus `-Dspring.context.exit=onRefresh` makes the
context start, refresh and exit immediately:

```bash
java -XX:ArchiveClassesAtExit=target/app.jsa \
     -Dspring.aot.enabled=true \
     -Dspring.context.exit=onRefresh \
     -jar target/extracted/app.jar
```

3. Run with the archive:

```bash
java -XX:SharedArchiveFile=target/app.jsa -Dspring.aot.enabled=true -jar target/extracted/app.jar
```

A cheap check without starting the app: `java -Xshare:on -XX:SharedArchiveFile=target/app.jsa -XX:+PrintSharedArchiveAndExit -jar target/extracted/app.jar` ends with `archive is valid`, or exits non-zero with the mismatch reason.

Add `-Xshare:on` while verifying: with it the JVM fails loudly when the archive is rejected
instead of silently falling back. Remove it for production (`-Xshare:auto` default) so a mismatch
degrades instead of crashing.

The archive is rejected when any of these differ between training and run: JDK build, CPU
architecture, classpath entries or their order, or the jar contents (hence the extracted layout).
Build the archive inside the image build, never on a developer laptop.

## CDS on Boot 3.0 to 3.2 (by hand)

Boot 3.0 to 3.2 has no `-Djarmode=tools extract`, and CDS cannot archive classes loaded from
nested jars in a fat jar. Unpack the jar and run it with a plain classpath. Two rules that are easy
to miss:

- **No directories on the classpath.** The JDK refuses to create or use a dynamic archive when the
  classpath contains a non-empty directory, so `BOOT-INF/classes` must be packed into a jar first.
- **Keep the classpath order of `BOOT-INF/classpath.idx`.** It is the order the fat jar uses; a
  different order can pick a different class when two jars contain the same one, and any change of
  order between training and run rejects the archive.

```bash
rm -rf target/cds && mkdir -p target/cds && cd target/cds
unzip -q ../<artifact>-<version>.jar
jar cf app-classes.jar -C BOOT-INF/classes .
MAIN=$(grep '^Start-Class:' META-INF/MANIFEST.MF | cut -d' ' -f2 | tr -d '\r')
CP="app-classes.jar:$(sed -E 's/^- "(.*)"$/\1/' BOOT-INF/classpath.idx | paste -sd: -)"

# training run (Boot 3.2: spring.context.exit exists; 3.0/3.1: start, wait for "Started", then kill -TERM, not -9)
java -XX:ArchiveClassesAtExit=app.jsa -Dspring.context.exit=onRefresh -cp "$CP" "$MAIN"
# run, with the exact same classpath string
java -XX:SharedArchiveFile=app.jsa -cp "$CP" "$MAIN"
```

Add `-Dspring.aot.enabled=true` to both lines when the jar was built with `process-aot`. Check the
archive with `-Xshare:on` as below. In a Dockerfile, copy `app-classes.jar` and `BOOT-INF/lib/`,
keep the same relative paths, and build `CP` the same way in the training stage and the
`ENTRYPOINT`. Measure the unpacked layout without the archive first (step (a) of tier 3), because
dropping the nested-jar loader is a gain of its own.

## Memory during training runs

A training run is a full application start plus archive dumping, and it needs more memory than a
normal start. Run one JVM at a time: do not train while measurement JVMs or other containers are
running. Exit code 137 means the process was killed (SIGKILL), usually by the OOM killer or Docker
Desktop's memory limit. Close other JVMs, check free memory and the Docker VM's memory setting,
and run it again alone before concluding anything about CDS.

## JDK AOT cache (Project Leyden), JDK 24+

JDK 24 (JEP 483): two steps, record then create.

```bash
java -XX:AOTMode=record -XX:AOTConfiguration=app.aotconf -Dspring.context.exit=onRefresh -jar target/extracted/app.jar
java -XX:AOTMode=create -XX:AOTConfiguration=app.aotconf -XX:AOTCache=app.aot -jar target/extracted/app.jar
java -XX:AOTCache=app.aot -Dspring.aot.enabled=true -jar target/extracted/app.jar
```

JDK 25 (JEP 514/515): one step, and the cache also holds method profiles so JIT starts warmer.

```bash
java -XX:AOTCacheOutput=app.aot -Dspring.context.exit=onRefresh -jar target/extracted/app.jar
java -XX:AOTCache=app.aot -Dspring.aot.enabled=true -jar target/extracted/app.jar
```

Same compatibility rules as CDS, plus the cache embeds the exact JDK build. A JDK patch upgrade
in the base image without a rebuilt cache means the cache is dropped silently; a `-Xlog:aot` or
`-Xlog:cds` line at start confirms whether it loaded.

## Dockerfile pattern (multi-stage, JDK 21, CDS)

```dockerfile
FROM eclipse-temurin:21-jdk AS build
WORKDIR /app
COPY . .
RUN ./mvnw -q -DskipTests package
RUN java -Djarmode=tools -jar target/*.jar extract --destination target/extracted --application-filename app.jar

FROM eclipse-temurin:21-jre AS train
WORKDIR /app
COPY --from=build /app/target/extracted/ ./
RUN java -XX:ArchiveClassesAtExit=app.jsa -Dspring.aot.enabled=true -Dspring.context.exit=onRefresh -jar app.jar

FROM eclipse-temurin:21-jre
WORKDIR /app
COPY --from=build /app/target/extracted/lib ./lib
COPY --from=build /app/target/extracted/app.jar ./app.jar
COPY --from=train /app/app.jsa ./app.jsa
ENV JAVA_TOOL_OPTIONS="-XX:SharedArchiveFile=app.jsa -Dspring.aot.enabled=true -XX:MaxRAMPercentage=75"
ENTRYPOINT ["java","-jar","app.jar"]
```

The `train` stage must use the **same base image** as the final stage. Using one `ARG BASE_IMAGE` for both `FROM` lines makes drift impossible. A cheap build-time guard in the final stage fails the build when the archive does not match, without starting the app:

```dockerfile
RUN java -Xshare:on -XX:SharedArchiveFile=app.jsa -XX:+PrintSharedArchiveAndExit -jar app.jar
```

Put the CDS/AOT flags in `ENTRYPOINT` rather than only in `JAVA_TOOL_OPTIONS`: a pod spec that sets its own `JAVA_TOOL_OPTIONS` (APM agent, heap size) replaces the image's value and silently drops `-XX:SharedArchiveFile`.

### Flyway (and other startup I/O) during the training run

`-Dspring.context.exit=onRefresh` still creates every singleton, and Flyway migrates inside bean creation. There is no database during `docker build`. With Spring AOT on, `spring.flyway.enabled=false` does nothing (see above), so gate the migration on a property value instead:

```java
@Bean
FlywayMigrationStrategy flywayMigrationStrategy(@Value("${app.flyway.migrate-on-startup:true}") boolean migrate) {
    return flyway -> { if (migrate) flyway.migrate(); };
}
```

Pass `-Dapp.flyway.migrate-on-startup=false` only on the training `RUN` line, with dummy but syntactically valid `spring.datasource.*` values. Point the dummy URL at a closed port such as `jdbc:postgresql://127.0.0.1:1/cds-training`: anything that really tries to connect then fails the build at once instead of hanging until a timeout. Hikari and Spring Data do not connect during refresh; Hibernate does unless `database-platform` is set and `hibernate.boot.allow_jdbc_metadata_access=false`. Never give the image build production credentials: a training run that can reach the real database can migrate it.

Without Spring AOT, `-Dspring.flyway.enabled=false` on the training run works, at the cost of Flyway classes not being in the archive. If the training run needs a
database or a profile, pass `-Dspring.profiles.active=...` and point it at a disposable instance,
or structure the context so `onRefresh` happens before external connections (Hikari and Spring
Data connect lazily by default; Flyway does not).

For JDK 24/25 replace the `ArchiveClassesAtExit` and `SharedArchiveFile` flags with the AOT
cache flags above.

### Training with real requests, when the archive should cover the request path

`-Dspring.context.exit=onRefresh` exits before the app serves anything, so no connector, dispatch
or serialization class from the request path is in the archive. To include them, the training run
has to exercise a request and then exit *normally*, because the archive is written at JVM exit.
Drive it from the app's own warm-up component rather than from shell polling, which needs a
`wget`/`curl` in the base image and log parsing:

```java
// in the warm-up runner; the property is set only on the training RUN line
@EventListener(ApplicationReadyEvent.class)
void shutdownAfterTrainingRun(ApplicationReadyEvent event) {
    if (properties.shutdownAfterWarmup()) {
        event.getApplicationContext().close();
    }
}
```

```dockerfile
RUN java -XX:ArchiveClassesAtExit=app.jsa @cp.args com.example.Application \
    --spring.config.additional-location=file:./cds-training.properties \
    --app.warmup.enabled=true --app.warmup.iterations=1 \
    --app.warmup.shutdown-after-warmup=true
```

Close the context from `ApplicationReadyEvent`, not from inside the runner: a runner still runs
inside `SpringApplication.run()`, and closing there leaves Spring publishing events on a closed
context. Verify three things before trusting it: the `java` process really exits (a lingering
non-daemon thread from a broker retry can hang the build; fall back to `System.exit(0)` after
`close()`), how much build time the warm-up adds (expect seconds per unreachable dependency at its
connect timeout), and that the existing `-Xshare:on -XX:+PrintSharedArchiveAndExit` check still
passes.

Remember what this does and does not buy: an archive carries **classes, not cache state**, and the
tasks that need a database fail during `docker build`. See `warmup.md` sections 6 and 8 for the measured
class-count differences.

## Buildpacks

```bash
./mvnw spring-boot:build-image -Dspring-boot.build-image.env.BP_JVM_CDS_ENABLED=true -Dspring-boot.build-image.env.BP_SPRING_AOT_ENABLED=true
```

Gradle: `bootBuildImage { environment["BP_JVM_CDS_ENABLED"] = "true"; environment["BP_SPRING_AOT_ENABLED"] = "true" }`.
The buildpack runs the training with `-Dspring.context.exit=onRefresh`; set `CDS_TRAINING_JAVA_TOOL_OPTIONS` for profile or property overrides during training.

## How to verify it worked

- Log shows `Starting AOT-processed`.
- `-Xshare:on` run does not fail, or `-Xlog:cds` / `-Xlog:aot` reports the archive mapped.
- `measure_startup.sh` median for `process running for` drops; the Spring-side `in X seconds` drops less, which is expected because class loading is where CDS wins.
- Class-load count (`-Xlog:class+load`) shows most classes with `source: shared objects file`.
