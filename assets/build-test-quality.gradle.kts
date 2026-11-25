// Test-quality tooling for Gradle (Kotlin DSL). Verified with Gradle 9.3.0, Spring Boot 3.5.7,
// io.spring.dependency-management 1.1.7, JDK 21: JaCoCo 0.8.16, PIT 1.30.2 via
// info.solidsoft.pitest 1.19.0 (its default PIT is older, so pitestVersion is pinned below).
//
// 1. Add to plugins { }:   jacoco   and   id("info.solidsoft.pitest") version "1.19.0"
// 2. Make sure the test runtime has the launcher: testRuntimeOnly("org.junit.platform:junit-platform-launcher")
// 3. Paste the block below and set targetClasses to the application's base package.
//
// Commands: ./gradlew test (report: build/reports/jacoco/test/jacocoTestReport.xml)
//           ./gradlew check (coverage gate)   ./gradlew pitest (build/reports/pitest/mutations.xml)
//           ./gradlew pitest -PpitTargetClasses=com.x.Foo -PpitTargetTests='com.x.Foo*Test' -PpitThreshold=0

// ---- test-quality: begin ----
val coverageMinimum = "0.95".toBigDecimal()
val coverageExcludes = listOf("**/*Application.class")   // every entry needs the user's approval
val mockitoAgent: Configuration by configurations.creating
dependencies { mockitoAgent("org.mockito:mockito-core") { isTransitive = false } }

jacoco { toolVersion = "0.8.16" }
tasks.test {
  useJUnitPlatform()
  jvmArgs("-javaagent:${mockitoAgent.asPath}", "-Xshare:off")
  finalizedBy(tasks.jacocoTestReport)
}
tasks.jacocoTestReport {
  dependsOn(tasks.test)
  reports { xml.required = true; html.required = true }
  classDirectories.setFrom(files(classDirectories.files.map { fileTree(it) { exclude(coverageExcludes) } }))
}
tasks.jacocoTestCoverageVerification {
  dependsOn(tasks.test)
  classDirectories.setFrom(files(classDirectories.files.map { fileTree(it) { exclude(coverageExcludes) } }))
  violationRules {
    rule {
      limit { counter = "LINE"; minimum = coverageMinimum }
      limit { counter = "BRANCH"; minimum = coverageMinimum }
    }
  }
}
tasks.check { dependsOn(tasks.jacocoTestCoverageVerification) }   // not wired by default
pitest {
  pitestVersion = "1.30.2"
  junit5PluginVersion = "1.2.3"
  // per run: -PpitTargetClasses=com.x.Foo -PpitTargetTests='com.x.Foo*Test' -PpitThreshold=0
  targetClasses = setOf(providers.gradleProperty("pitTargetClasses").getOrElse("com.example.*"))
  providers.gradleProperty("pitTargetTests").orNull?.let { targetTests = setOf(it) }
  excludedClasses = setOf("*Application")
  excludedTestClasses = setOf("*IT", "*IntegrationTest")
  mutators = setOf("DEFAULTS")
  mutationThreshold = providers.gradleProperty("pitThreshold").getOrElse("95").toInt()
  outputFormats = setOf("XML", "HTML")
  timestampedReports = false
  threads = Runtime.getRuntime().availableProcessors()
  jvmArgs = listOf("-javaagent:${mockitoAgent.asPath}", "-Xshare:off")
}
// ---- test-quality: end ----
