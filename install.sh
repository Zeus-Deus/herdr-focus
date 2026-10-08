#!/bin/sh
# Herdr Focus installer.
#
#   curl -fsSL https://raw.githubusercontent.com/Zeus-Deus/herdr-focus/main/install.sh | sh
#   curl -fsSL https://raw.githubusercontent.com/Zeus-Deus/herdr-focus/main/install.sh | sh -s -- --uninstall
#
# Options (after `sh -s --` when piping):
#   --link DIR   link a local checkout instead of installing from GitHub
#   --uninstall  remove the plugin, its sidebar tokens, its config block and its state
#
# Safe to run again: it updates the plugin and never adds the config block twice.
# config.toml is backed up before every change.
set -eu

REPO="Zeus-Deus/herdr-focus"
herdr="${HERDR_BIN_PATH:-herdr}"
link=""
uninstall=0

# Run from a checkout (./install.sh)? Then link that checkout.
case "$0" in
  *install.sh) here="$(cd "$(dirname "$0")" && pwd)"; [ -f "$here/herdr-plugin.toml" ] && link="$here" ;;
esac

while [ $# -gt 0 ]; do
  case "$1" in
    --link) shift; link="$(cd "$1" && pwd)" ;;
    --uninstall) uninstall=1 ;;
    -h|--help) sed -n '2,12p' "$0" 2>/dev/null || true; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

say() { printf '\033[1;33m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

command -v "$herdr" >/dev/null 2>&1 || die "herdr is not installed (https://herdr.dev)"
command -v python3 >/dev/null 2>&1 || die "Herdr Focus needs python3"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' || die "Herdr Focus needs Python 3.11 or newer"

# Field of the registered plugin: plugin_info root | plugin_info kind (github or local).
plugin_info() {
  "$herdr" plugin list --json 2>/dev/null | python3 -c '
import json, sys
try:
    plugins = json.load(sys.stdin)["result"]["plugins"]
except Exception:
    sys.exit(0)
for p in plugins:
    if p.get("plugin_id") == "focus":
        print(p.get("plugin_root") or "" if sys.argv[1] == "root" else (p.get("source") or {}).get("kind", ""))
' "$1"
}
plugin_root() { plugin_info root; }

if [ "$uninstall" = 1 ]; then
  root="$(plugin_root)"
  [ -n "$root" ] || root="$link"
  if [ -n "$root" ] && [ -d "$root/focus" ]; then
    # Stops the daemon, clears its sidebar tokens and Agents view, removes the config block.
    (cd "$root" && python3 -m focus uninstall)
  fi
  if [ -n "$(plugin_root)" ]; then
    "$herdr" plugin uninstall focus >/dev/null 2>&1 || "$herdr" plugin unlink focus >/dev/null
    say "Removed the plugin"
  fi
  rm -rf "${XDG_STATE_HOME:-$HOME/.local/state}/herdr/plugins/focus"
  settings="${XDG_CONFIG_HOME:-$HOME/.config}/herdr/plugins/config/focus"
  if [ -d "$settings" ] && ! rmdir "$settings" 2>/dev/null; then
    say "Kept your settings in $settings"
  fi
  say "Uninstalled."
  exit 0
fi

if [ -n "$link" ]; then
  say "Linking $link"
  "$herdr" plugin link "$link" >/dev/null
else
  existing="$(plugin_root)"
  if [ -n "$existing" ] && [ "$(plugin_info kind)" != "github" ]; then
    die "Focus is linked from $existing. Run $existing/install.sh to update it, or uninstall first."
  fi
  say "Installing from GitHub ($REPO)"
  "$herdr" plugin install "$REPO" --yes >/dev/null
fi

root="$(plugin_root)"
[ -n "$root" ] || die "Herdr did not register the plugin; see \`herdr plugin list\`"
(cd "$root" && python3 -m focus install-config)
if "$herdr" plugin action invoke focus.restart >/dev/null 2>&1; then
  say "Started"
else
  say "Herdr isn't running; Focus starts with it next time"
fi
say "Done. In Herdr press prefix+a for the attention menu."
