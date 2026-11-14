#!/usr/bin/env bash
# Scan a Spring Boot project for the facts the startup-tuning workflow needs.
# Usage: project_scan.sh [project-dir]
set -u
DIR="${1:-.}"
cd "$DIR" || { echo "cannot cd to $DIR" >&2; exit 2; }
ROOT="$(pwd)"

section() { printf '\n== %s ==\n' "$1"; }
have() { [ -e "$1" ]; }
grep_r() { grep -rn --include='*.java' --include='*.kt' --include='*.yml' --include='*.yaml' --include='*.properties' "$@" src/main 2>/dev/null; }

section "Project"
echo "dir: $ROOT"
BUILD=""
if have pom.xml; then BUILD="maven"; fi
if have build.gradle || have build.gradle.kts; then BUILD="${BUILD:+$BUILD+}gradle"; fi
echo "build tool: ${BUILD:-none found (no pom.xml / build.gradle*)}"
[ -x ./mvnw ] && echo "wrapper: ./mvnw"
[ -x ./gradlew ] && echo "wrapper: ./gradlew"

section "Versions"
if have pom.xml; then
  boot=$(grep -A3 '<parent>' pom.xml | grep -o '<version>[^<]*' | head -1 | sed 's/<version>//')
  [ -z "$boot" ] && boot=$(grep -o 'spring-boot[^<]*</artifactId>[[:space:]]*<version>[^<]*' pom.xml | grep -o '<version>[^<]*' | head -1 | sed 's/<version>//')
  echo "spring boot (pom): ${boot:-unknown}"
  jv=$(grep -oE '<(java\.version|maven\.compiler\.(source|release|target))>[^<]*' pom.xml | head -1 | sed 's/.*>//')
  echo "java target (pom): ${jv:-unknown}"
fi
for f in build.gradle build.gradle.kts; do
  if have "$f"; then
    boot=$(grep -oE "org\.springframework\.boot['\"]?\)?[[:space:]]+version[[:space:]]*['\"(]+[^'\")]+" "$f" | grep -oE "[0-9]+\.[0-9]+\.[0-9]+[^'\")]*" | head -1)
    echo "spring boot ($f): ${boot:-unknown}"
    jv=$(grep -oE 'JavaLanguageVersion\.of\([0-9]+\)|sourceCompatibility[^0-9]*[0-9]+|jvmToolchain\([0-9]+\)' "$f" | grep -oE '[0-9]+' | head -1)
    echo "java target ($f): ${jv:-unknown}"
  fi
done
echo "local java: $(java -version 2>&1 | head -1)"
if docker info >/dev/null 2>&1; then echo "docker: $(docker info --format "{{.ServerVersion}}" 2>/dev/null)"; else echo "docker: not running / not installed"; fi

section "Starters and notable dependencies"
if have pom.xml; then
  grep -oE '<artifactId>(spring-boot-starter[^<]*|spring-cloud-starter[^<]*|micrometer-registry[^<]*|flyway[^<]*|liquibase[^<]*|hibernate[^<]*|kotlin-reflect|aws-java-sdk[^<]*|spring-boot-devtools)' pom.xml | sed 's/<artifactId>/  /' | sort -u
fi
for f in build.gradle build.gradle.kts; do
  have "$f" && grep -oE "(spring-boot-starter[^'\":]*|spring-cloud-starter[^'\":]*|micrometer-registry[^'\":]*|flyway[^'\":]*|liquibase[^'\":]*|kotlin-reflect|aws-java-sdk[^'\":]*|spring-boot-devtools)" "$f" | sed 's/^/  /' | sort -u
done
ACT=no; grep -qsE 'spring-boot-starter-actuator' pom.xml build.gradle build.gradle.kts 2>/dev/null && ACT=yes
echo "actuator present: $ACT"

section "Main class"
MAIN=$(grep -rl --include='*.java' --include='*.kt' '@SpringBootApplication' src/main 2>/dev/null | head -3)
echo "${MAIN:-not found}"
for m in $MAIN; do
  grep -nE 'scanBasePackages|exclude|BufferingApplicationStartup|setLazyInitialization|@EnableAsync|@EnableScheduling|@EnableCaching|@ComponentScan' "$m" | sed 's/^/  /'
done

section "Config files"
find src/main/resources -maxdepth 1 \( -name 'application*.yml' -o -name 'application*.yaml' -o -name 'application*.properties' -o -name 'bootstrap*.yml' -o -name 'bootstrap*.properties' \) 2>/dev/null | sed 's/^/  /'

section "Existing tuning-relevant properties (any profile)"
grep_r -E 'lazy-initialization|lazy_initialization|ddl-auto|open-in-view|allow_jdbc_metadata_access|database-platform|autoconfigure\.exclude|jmx\.enabled|minimum-idle|initialization-fail-timeout|bootstrap-mode|sql\.init\.mode|defer-datasource-initialization|flyway\.enabled|liquibase\.enabled|exposure\.include|logging\.level\.root|^debug|banner-mode|spring\.aot|spring\.cache\.type' | sed 's/^/  /' || echo "  none"

section "Code patterns worth a look"
echo "-- @PostConstruct / runners / startup listeners:"
grep_r -lE '@PostConstruct|CommandLineRunner|ApplicationRunner|ContextRefreshedEvent|ApplicationReadyEvent|InitializingBean' | sed 's/^/  /'
echo "-- @Scheduled with initialDelay 0 or none:"
{ grep_r -nE '@Scheduled\(' | grep -v initialDelay; grep_r -nE 'initialDelay\s*=\s*0[^0-9]'; } | head -10 | sed 's/^/  /'
echo "-- enable annotations:"
grep_r -ohE '@Enable[A-Za-z]+' | sort | uniq -c | sort -rn | sed 's/^/  /'
echo "-- blocking calls in beans (first 10):"
grep_r -nE '\.block\(\)|Thread\.sleep\(|\.get\(\)\s*;' | head -10 | sed 's/^/  /'

section "Container / deployment"
for f in Dockerfile* docker/Dockerfile* src/main/docker/Dockerfile* ; do
  [ -f "$f" ] || continue
  echo "-- $f"
  grep -nE '^FROM|ENTRYPOINT|CMD|JAVA_TOOL_OPTIONS|JAVA_OPTS|-XX:|-Xshare|-Xm[sx]|jarmode|AOTCache|SharedArchiveFile|spring\.aot' "$f" | sed 's/^/  /'
done
grep -qsE 'build-image|bootBuildImage|BP_JVM_CDS|BP_SPRING_AOT' pom.xml build.gradle build.gradle.kts 2>/dev/null && echo "buildpacks config present (check BP_JVM_CDS_ENABLED / BP_SPRING_AOT_ENABLED)"
K8S=$(grep -rlE 'kind:\s*(Deployment|StatefulSet|Rollout|CronJob)' --include='*.yml' --include='*.yaml' . 2>/dev/null | grep -vE 'node_modules|/target/|/build/|src/main/resources' | head -10)
if [ -n "$K8S" ]; then
  for f in $K8S; do
    echo "-- $f"
    grep -nE 'cpu:|memory:|startupProbe|readinessProbe|livenessProbe|initialDelaySeconds|failureThreshold|periodSeconds|JAVA_TOOL_OPTIONS|JAVA_OPTS' "$f" | sed 's/^/  /'
  done
else
  echo "no k8s Deployment manifests found under $ROOT (helm values may hold them: $(ls -d */values*.y*ml helm 2>/dev/null | tr '\n' ' '))"
fi

section "Built artifacts"
ls -1 target/*.jar build/libs/*.jar 2>/dev/null | grep -vE 'plain|sources|javadoc' | sed 's/^/  /' || echo "  none built yet"
