#!/usr/bin/env bash
# Link every skill in skills/ into ~/.claude/skills/ (personal skills, all projects).
# Usage: ./install.sh [skill-name ...]     (default: all skills)
# A skill folder in ~/.claude/skills may be a symlink; Claude Code reads SKILL.md from the target.
# An existing real directory of the same name is left alone and reported, never overwritten.
set -eu
ROOT="$(cd "$(dirname "$0")" && pwd)"
DEST="${CLAUDE_SKILLS_DIR:-$HOME/.claude/skills}"
mkdir -p "$DEST"
names=("$@")
if [ ${#names[@]} -eq 0 ]; then
  for d in "$ROOT"/skills/*/; do names+=("$(basename "$d")"); done
fi
for n in "${names[@]}"; do
  src="$ROOT/skills/$n"; dst="$DEST/$n"
  [ -f "$src/SKILL.md" ] || { echo "skip $n: no $src/SKILL.md"; continue; }
  if [ -L "$dst" ]; then
    ln -sfn "$src" "$dst"; echo "updated  $dst -> $src"
  elif [ -e "$dst" ]; then
    echo "SKIPPED  $dst exists and is not a symlink; move it away, then rerun"
  else
    ln -s "$src" "$dst"; echo "linked   $dst -> $src"
  fi
done
