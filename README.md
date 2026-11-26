# Spring Boot skills for Claude Code

| Skill | What it does |
|---|---|
| [`springboot-startup-tuning`](skills/springboot-startup-tuning/) | Review, measure and tune startup time, first-request warm-up and steady-state CPU, memory and latency; Kubernetes requests, limits and probes |
| [`springboot-unit-testing`](skills/springboot-unit-testing/) | Write unit tests until JaCoCo line and branch coverage and the raw PIT mutation score are at least 95 percent and the tests pass SonarQube's test rules, with guards against gaming the numbers |

## Install

As a plugin (skills appear as `/springboot-skills:<skill>`):

```bash
claude plugin marketplace add iamklang/springboot-startup-tuning
claude plugin install springboot-skills@iamklang-skills
```

As personal skills linked from a clone (skills appear under their own names, and `git pull` updates them):

```bash
git clone git@github.com:iamklang/springboot-startup-tuning.git
cd springboot-startup-tuning && ./install.sh
```

## Layout

```
.claude-plugin/plugin.json        plugin manifest (skills/ is scanned automatically)
.claude-plugin/marketplace.json   this repository as a marketplace with one plugin
skills/<skill-name>/SKILL.md      one folder per skill, with its scripts, references and assets
install.sh                        symlinks skills/* into ~/.claude/skills/
```

To add a skill: create `skills/<new-skill>/SKILL.md` (frontmatter `name` equal to the folder name),
then run `./install.sh <new-skill>` locally.
