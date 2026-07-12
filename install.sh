#!/usr/bin/env bash
# install.sh — install/update/uninstall the review-deck plugin.
#
# Claude Code auto-loads any directory under a "skills dir" that contains
# .claude-plugin/plugin.json as a full plugin (<name>@skills-dir):
#   global:  ~/.claude/skills/review-deck
#   local:   <project>/.claude/skills/review-deck
#
# Usage:
#   ./install.sh                     # global install (symlink — updates via git pull)
#   ./install.sh --local [DIR]      # install into DIR's .claude/skills (default: cwd's git root)
#   ./install.sh --copy             # copy instead of symlink (re-run to update)
#   ./install.sh uninstall [--local [DIR]]
#   ./install.sh -h|--help
set -euo pipefail

PLUGIN=review-deck
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

MODE=install
SCOPE=global
METHOD=link
LOCAL_DIR=""

usage() { sed -n '2,15p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

while [ $# -gt 0 ]; do
  case "$1" in
    install|update) MODE=install ;;
    uninstall)      MODE=uninstall ;;
    --local)        SCOPE=local
                    if [ $# -gt 1 ] && [ "${2#-}" = "$2" ]; then LOCAL_DIR="$2"; shift; fi ;;
    --global)       SCOPE=global ;;
    --copy)         METHOD=copy ;;
    -h|--help)      usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

[ -f "$SRC/.claude-plugin/plugin.json" ] || {
  echo "error: $SRC does not look like the plugin (missing .claude-plugin/plugin.json)" >&2; exit 1; }

if [ "$SCOPE" = global ]; then
  ROOT="$HOME/.claude/skills"
else
  if [ -z "$LOCAL_DIR" ]; then
    LOCAL_DIR="$(git rev-parse --show-toplevel 2>/dev/null)" || {
      echo "error: --local without DIR requires running inside a git repo" >&2; exit 1; }
  fi
  [ -d "$LOCAL_DIR" ] || { echo "error: no such directory: $LOCAL_DIR" >&2; exit 1; }
  ROOT="$LOCAL_DIR/.claude/skills"
fi
DEST="$ROOT/$PLUGIN"

# refuse to delete anything at DEST that isn't ours
is_ours() {
  [ -L "$DEST" ] && return 0
  [ -f "$DEST/.claude-plugin/plugin.json" ] && grep -q "\"$PLUGIN\"" "$DEST/.claude-plugin/plugin.json"
}

remove_dest() {
  if [ -L "$DEST" ]; then rm "$DEST"
  elif [ -e "$DEST" ]; then
    is_ours || { echo "error: $DEST exists and doesn't look like $PLUGIN — not touching it" >&2; exit 1; }
    rm -rf "$DEST"
  fi
}

if [ "$MODE" = uninstall ]; then
  if [ -e "$DEST" ] || [ -L "$DEST" ]; then
    remove_dest
    echo "removed $DEST"
    echo "restart your Claude Code session for the change to take effect"
  else
    echo "nothing installed at $DEST"
  fi
  exit 0
fi

# install / update
if command -v claude >/dev/null 2>&1; then
  claude plugin validate "$SRC" >/dev/null 2>&1 || {
    echo "error: 'claude plugin validate' failed for $SRC:" >&2
    claude plugin validate "$SRC" >&2 || true
    exit 1; }
fi

mkdir -p "$ROOT"

if [ "$METHOD" = link ]; then
  if [ -L "$DEST" ] && [ "$(readlink "$DEST")" = "$SRC" ]; then
    echo "already installed (symlink): $DEST -> $SRC"
    echo "symlink installs track this checkout — 'git pull' is your update"
    exit 0
  fi
  remove_dest
  ln -s "$SRC" "$DEST"
  echo "installed (symlink): $DEST -> $SRC"
  echo "updates: just 'git pull' in $SRC"
else
  remove_dest
  mkdir -p "$DEST"
  # copy plugin contents, skipping repo junk
  (cd "$SRC" && tar cf - --exclude='__pycache__' --exclude='*.pyc' --exclude='.git' --exclude='install.sh' .) | (cd "$DEST" && tar xf -)
  echo "installed (copy): $DEST"
  echo "updates: re-run this script after pulling changes"
fi

echo "restart your Claude Code session, then verify with: claude plugin details $PLUGIN@skills-dir"
