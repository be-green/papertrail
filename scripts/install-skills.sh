#!/bin/bash
# Install papertrail skills to ~/.claude/skills/
# Run from anywhere: ./path/to/papertrail/scripts/install-skills.sh

set -e

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SKILLS_DIR="${HOME}/.claude/skills"

mkdir -p "$SKILLS_DIR"

for skill_dir in "$REPO_DIR/skills"/*/; do
    name=$(basename "$skill_dir")
    ln -sfn "$skill_dir" "$SKILLS_DIR/$name"
    echo "Linked $name -> $skill_dir"
done

echo "Done. Skills installed to $SKILLS_DIR"
