#!/usr/bin/env bash
# Seed an adklaw workspace from templates/AGENTS.md.
#
# Usage:
#   bash scripts/init-workspace.sh                 # seeds ./workspace
#   bash scripts/init-workspace.sh ~/my-project    # seeds an absolute path
#
# Refuses to overwrite an existing AGENTS.md so re-running is safe.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TEMPLATE="$REPO_ROOT/templates/AGENTS.md"

TARGET="${1:-$REPO_ROOT/workspace}"

if [ ! -f "$TEMPLATE" ]; then
    echo "Template not found at $TEMPLATE" >&2
    exit 1
fi

mkdir -p "$TARGET"
mkdir -p "$TARGET/skills"

if [ -f "$TARGET/AGENTS.md" ]; then
    echo "Refusing to overwrite existing $TARGET/AGENTS.md." >&2
    echo "Edit it directly or remove it before re-running this script." >&2
    exit 1
fi

cp "$TEMPLATE" "$TARGET/AGENTS.md"
echo "Seeded workspace at $TARGET"
echo "  $TARGET/AGENTS.md  (from templates/AGENTS.md — edit to customize)"
echo "  $TARGET/skills/    (drop your private skill folders here)"
