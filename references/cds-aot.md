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
| 3.0 to 3.2 | any | Spring AOT only (CDS by hand is possible but Boot has no `extract` tooling; recommend upgrading to 3.3+) |
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
- Dev tools, `@MockBean`-heavy test setups and some older libraries are not AOT-friendly. Keep AOT off for the test classpath unless the project already runs tests that way.

## CDS (Class Data Sharing), Boot 3.3+

1. Extract the jar so the classpath is stable between runs:

```bash
java -Djarmode=tools -jar target/app.jar extract --destination target/extracted
```

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

Add `-Xshare:on` while verifying: with it the JVM fails loudly when the archive is rejected
instead of silently falling back. Remove it for production (`-Xshare:auto` default) so a mismatch
degrades instead of crashing.

The archive is rejected when any of these differ between training and run: JDK build, CPU
architecture, classpath entries or their order, or the jar contents (hence the extracted layout).
Build the archive inside the image build, never on a developer laptop.

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
RUN java -Djarmode=tools -jar target/*.jar extract --destination target/extracted

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
