#!/usr/bin/env python3
"""Summarise the CONDITIONS EVALUATION REPORT from a Spring Boot log started with --debug.

Usage:
  autoconfig_report.py <logfile> [--all]

Prints the positive (active) auto-configurations grouped by area, flags ones that are often
unnecessary, and counts the negative ones. Without --all only the flagged and grouped summary is
shown. Produce the log with:  java -jar app.jar --debug > debug.log   (or debug=true in properties)
"""
import re
import sys
from collections import defaultdict

# (regex on the auto-configuration class simple name, hint). Keep hints factual; the model decides.
SUSPECTS = [
    (r"Jmx", "JMX export; spring.jmx.enabled=false unless something scrapes MBeans"),
    (r"^(Ldap|Mongo|Redis|Cassandra|Neo4j|Elasticsearch|Couchbase|R2dbc|Rabbit|Kafka|Jms|Artemis|ActiveMQ|Pulsar)", "data/messaging client; remove the starter if this store or broker is not used"),
    (r"^Mail", "mail sender; remove spring-boot-starter-mail if unused"),
    (r"^(FreeMarker|Thymeleaf|Mustache|Groovy)Template", "template engine; remove if the service is API-only"),
    (r"^(Prometheus|Datadog|NewRelic|Statsd|Graphite|Influx|Wavefront|Dynatrace|SignalFx|Elastic|Humio|AppOptics|Ganglia|Kairos|Otlp)Metrics", "metrics registry; keep only the one that is scraped"),
    (r"^(Jolokia|Flyway|Liquibase)", "runs work at startup; confirm it is intended here"),
    (r"^Hibernate(Jpa|Metrics)", "JPA bootstrap; check ddl-auto, allow_jdbc_metadata_access, open-in-view"),
    (r"^(Session|SpringSession|RedisSession|JdbcSession)", "session store; set spring.session.store-type explicitly or remove"),
    (r"^(Batch|Integration)", "batch/integration infrastructure; heavy if unused"),
    (r"^(OAuth2|Saml2|Security)", "security; expected if the app is secured, otherwise remove starter"),
    (r"^(Websocket|WebSocket|Rsocket|RSocket)", "extra server stack; remove if unused"),
    (r"^(Cloud|Config|Eureka|Consul|Vault|Zookeeper|Bus|Sleuth|Zipkin)", "Spring Cloud; bootstrap context and remote calls before main context"),
    (r"^(DevTools|LocalDevTools|RemoteDevTools)", "devtools in this classpath; must not ship in the production image"),
    (r"^(Hazelcast|Infinispan|Ehcache|Caffeine|JCache)", "cache provider; set spring.cache.type explicitly"),
    (r"^(GraphQl|GraphQL)", "GraphQL; remove if unused"),
    (r"^(Quartz)", "scheduler with DB job store; check it is needed"),
]

AREA_RULES = [
    ("data", r"Jpa|Jdbc|DataSource|Hibernate|Flyway|Liquibase|R2dbc|Mongo|Redis|Cassandra|Neo4j|Elasticsearch|Couchbase|Ldap|Transaction|Sql"),
    ("web", r"Web|Servlet|Tomcat|Jetty|Undertow|Netty|Http|Dispatcher|Multipart|Error|Rest|WebClient|Jackson|Gson|Jsonb|Codec|Cors"),
    ("security", r"Security|OAuth2|Saml2|User"),
    ("messaging", r"Rabbit|Kafka|Jms|Artemis|ActiveMQ|Pulsar|Amqp|Integration"),
    ("actuator/metrics", r"Metrics|Actuator|Endpoint|Health|Info|Observation|Tracing|Micrometer|Prometheus|Datadog|Management|Audit|Startup|Logfile|Heapdump|Threaddump|Beans|Conditions|Env|Mappings|Scheduled|Quartz|Loggers|Shutdown|Sbom|Caches|Flyway|Liquibase|Jmx"),
    ("templating", r"Template|FreeMarker|Thymeleaf|Mustache|Groovy"),
    ("cache/session", r"Cache|Session|Hazelcast|Infinispan|Caffeine"),
    ("cloud", r"Cloud|Eureka|Consul|Vault|Bus|Discovery|ConfigClient|ConfigServer"),
    ("validation/aop/task", r"Validation|Aop|Task|Async|Scheduling|Lifecycle|Ssl|Context|Message|Property|Project|Configuration|Codec|Availability|Application"),
]


def area_of(simple):
    for area, rx in AREA_RULES:
        if re.search(rx, simple):
            return area
    return "other"


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    show_all = "--all" in argv
    text = open(argv[1], errors="replace").read()
    m = re.search(r"CONDITIONS EVALUATION REPORT(.*?)(?:Exclusions:|Unconditional classes:|\Z)", text, re.S)
    if not m:
        print("no CONDITIONS EVALUATION REPORT in log; run the app with --debug (or debug=true)")
        return 1
    report = m.group(1)
    pos_block = re.search(r"Positive matches:(.*?)Negative matches:", report, re.S)
    neg_block = re.search(r"Negative matches:(.*)", report, re.S)
    if not pos_block:
        print("report found but could not split positive/negative sections")
        return 1
    # Class entries look like "   SomethingAutoConfiguration matched:" or nested "SomethingAutoConfiguration#bean matched:"
    pos = re.findall(r"^\s{3}([A-Za-z0-9_.$]+?) matched:", pos_block.group(1), re.M)
    neg = re.findall(r"^\s{3}([A-Za-z0-9_.$]+?):\s*$", neg_block.group(1), re.M) if neg_block else []
    pos_top = sorted({p.split("#")[0].split(".")[-1] for p in pos})
    neg_top = sorted({n.split("#")[0].split(".")[-1] for n in neg})
    excl = re.search(r"Exclusions:\s*-+\s*(.*?)(?:Unconditional classes:|\Z)", text, re.S)
    excl_list = [] if not excl else [x.strip() for x in excl.group(1).splitlines() if x.strip() and x.strip() != "None"]

    print(f"positive auto-configurations: {len(pos_top)}   negative: {len(neg_top)}   explicit exclusions: {len(excl_list)}")

    flagged = []
    for simple in pos_top:
        for rx, hint in SUSPECTS:
            if re.search(rx, simple):
                flagged.append((simple, hint))
                break
    print("\n== Active auto-configurations worth questioning ==")
    if not flagged:
        print("  none matched the suspect list")
    for simple, hint in flagged:
        print(f"  {simple:<55} {hint}")

    groups = defaultdict(list)
    for simple in pos_top:
        groups[area_of(simple)].append(simple)
    print("\n== Active auto-configurations by area ==")
    for area, items in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        print(f"  {area:<18} {len(items):3d}" + ("" if not show_all else "\n" + "\n".join(f"      {i}" for i in items)))
    if not show_all:
        print("  (pass --all to list every class)")
    if excl_list:
        print("\n== Already excluded ==")
        for x in excl_list:
            print(f"  {x}")
    print("\nTo exclude: spring.autoconfigure.exclude=<fully qualified class>  (prefer removing the dependency)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
