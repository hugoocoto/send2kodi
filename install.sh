#!/usr/bin/env bash
#
# send2kodi installer.
#
# Puts two things in your home directory:
#   ~/.local/bin/send2kodi        the tool itself
#   ~/.config/send2kodi/config    the Kodi address, plus any other setting
#
# The address is asked for once and checked against Kodi before anything is
# written.
#
# https://github.com/hugoocoto/send2kodi

set -euo pipefail

SRC_DIR="$(cd -- "$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}")")" && pwd)"
SRC="$SRC_DIR/send2kodi"

BIN_DIR="${BIN_DIR:-$HOME/.local/bin}"
CFG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/send2kodi"
CFG="$CFG_DIR/config"

KODI_HOST="${KODI_HOST:-}"
KODI_PORT="${KODI_PORT:-8080}"
ASSUME_YES=0
UNINSTALL=0

die()  { printf 'install: %s\n' "$*" >&2; exit 1; }
warn() { printf 'warning: %s\n' "$*" >&2; }
ok()   { printf '  %s\n' "$*"; }

usage() {
  cat <<EOF
send2kodi installer - install send2kodi and write its settings file.

USAGE
  ./install.sh [options]

OPTIONS
      --host <addr>    Address or hostname of the Kodi box. Asked for
                       interactively if omitted.
      --port <p>       Kodi's web server port (default $KODI_PORT).
      --bin-dir <path> Where to put the executable (default $BIN_DIR).
  -y, --yes            Never prompt. Requires --host, or a KODI_HOST already
                       in $CFG.
      --uninstall      Remove everything this script installed.
  -h, --help           This message.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host)      KODI_HOST="${2:-}"; shift 2 ;;
    --port)      KODI_PORT="${2:-}"; shift 2 ;;
    --bin-dir)   BIN_DIR="${2:-}";   shift 2 ;;
    -y|--yes)    ASSUME_YES=1;       shift ;;
    --uninstall) UNINSTALL=1;        shift ;;
    -h|--help)   usage; exit 0 ;;
    *) echo "install: unknown argument: $1" >&2
       echo "Try: ./install.sh --help" >&2; exit 1 ;;
  esac
done

# --------------------------------------------------------------- uninstall --
if (( UNINSTALL )); then
  echo "Removing send2kodi..."
  if [[ -e "$BIN_DIR/send2kodi" ]]; then
    rm -f "$BIN_DIR/send2kodi"; ok "removed $BIN_DIR/send2kodi"
  fi
  if [[ -e "$CFG" ]]; then
    if (( ASSUME_YES )); then
      ok "kept $CFG"
    else
      read -rp "Remove the config at $CFG too? [y/N] " reply || reply=n
      case "$reply" in
        [yY]*) rm -rf "$CFG_DIR"; ok "removed $CFG_DIR" ;;
        *)     ok "kept $CFG" ;;
      esac
    fi
  fi
  echo "Done."
  exit 0
fi

# ------------------------------------------------------------ prerequisites --
[[ -f "$SRC" ]] || die "cannot find the send2kodi script next to this installer (looked in $SRC_DIR)."
command -v python3 >/dev/null 2>&1 || die "send2kodi needs python3."
python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' ||
  die "send2kodi needs Python 3.9 or newer."

# ------------------------------------------------------------ the Kodi box --
valid_host() {
  local h=$1 o
  if [[ "$h" =~ ^[0-9]+(\.[0-9]+){3}$ ]]; then
    for o in ${h//./ }; do (( 10#$o <= 255 )) || return 1; done
    return 0
  fi
  [[ "$h" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ ]]
}

rpc() {
  python3 - "$1" "$2" <<'EOF'
import json, sys, urllib.request
url, method = sys.argv[1:]
req = urllib.request.Request(url, headers={"Content-Type": "application/json"},
    data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                     "params": {"type": "xbmc.python.pluginsource"}
                     if method == "Addons.GetAddons" else {}}).encode())
try:
    with urllib.request.urlopen(req, timeout=5) as r:
        print(json.dumps(json.load(r)))
except urllib.error.HTTPError as e:
    print(f"HTTP {e.code}")
except OSError:
    pass
EOF
}

# 0 = Kodi answered, 1 = nothing answering, 2 = Kodi wants a login.
probe_kodi() {
  local out
  out=$(rpc "http://$1:$KODI_PORT/jsonrpc" JSONRPC.Ping)
  [[ "$out" == *pong* ]] && return 0
  [[ "$out" == "HTTP 401" ]] && return 2
  return 1
}

CURRENT=""
[[ -r "$CFG" ]] && CURRENT=$(sed -n 's/^[[:space:]]*KODI_HOST=//p' "$CFG" | tail -1 | tr -d '"'"'" || true)
[[ -n "$KODI_HOST" ]] || KODI_HOST="$CURRENT"

if [[ -z "$KODI_HOST" ]] || ! valid_host "$KODI_HOST"; then
  (( ASSUME_YES )) && die "--host is required with --yes."
  [[ -t 0 ]] || die "no terminal to ask on. Pass --host <addr>."
fi

needs_login=0
while true; do
  if [[ -z "$KODI_HOST" ]] || ! valid_host "$KODI_HOST"; then
    [[ -n "$KODI_HOST" ]] && echo "That does not look like an address or hostname."
    read -rp "Address of the Kodi box${CURRENT:+ [$CURRENT]}: " KODI_HOST || die "cancelled."
    [[ -n "$KODI_HOST" ]] || KODI_HOST="$CURRENT"
    continue
  fi

  echo -n "Checking $KODI_HOST:$KODI_PORT ... "
  set +e; probe_kodi "$KODI_HOST"; rc=$?; set -e
  case $rc in
    0) echo "Kodi is there."; break ;;
    2) echo "Kodi is there, and wants a login."
       echo "  Set KODI_USER and KODI_PASS in $CFG after installing."
       needs_login=1; break ;;
    1) echo "no answer."
       echo "  Is the box on, and is Settings > Services > Control >"
       echo "  'Allow remote control via HTTP' enabled?" ;;
  esac
  if (( ASSUME_YES )) || [[ ! -t 0 ]]; then
    warn "installing anyway; fix the receiver before the first run."
    break
  fi
  read -rp "Use $KODI_HOST anyway? [y/N/other address] " reply || die "cancelled."
  case "$reply" in
    [yY]*) break ;;
    ""|[nN]*) KODI_HOST=""; CURRENT="" ;;
    *) KODI_HOST="$reply" ;;
  esac
done

# The two add-ons do the actual URL resolving on the Kodi side.
if (( rc == 0 )); then
  addons=$(rpc "http://$KODI_HOST:$KODI_PORT/jsonrpc" Addons.GetAddons)
  [[ "$addons" == *plugin.video.sendtokodi* ]] ||
    warn "the SendToKodi add-on is not installed on Kodi; only YouTube links, links to media files and local files will work. See https://github.com/firsttris/plugin.video.sendtokodi"
  [[ "$addons" == *plugin.video.youtube* ]] ||
    warn "the YouTube add-on is not installed on Kodi; YouTube links will go through SendToKodi, which is slower to start."
fi

# -------------------------------------------------------------- install it --
echo
echo "Installing:"

install -Dm755 "$SRC" "$BIN_DIR/send2kodi"
ok "$BIN_DIR/send2kodi"

mkdir -p "$CFG_DIR"
if [[ -f "$CFG" ]]; then
  if grep -q '^[[:space:]]*KODI_HOST=' "$CFG"; then
    sed -i "s|^[[:space:]]*KODI_HOST=.*|KODI_HOST=$KODI_HOST|" "$CFG"
  else
    printf 'KODI_HOST=%s\n' "$KODI_HOST" >>"$CFG"
  fi
  if grep -q '^[[:space:]]*KODI_PORT=' "$CFG"; then
    sed -i "s|^[[:space:]]*KODI_PORT=.*|KODI_PORT=$KODI_PORT|" "$CFG"
  elif [[ "$KODI_PORT" != 8080 ]]; then
    printf 'KODI_PORT=%s\n' "$KODI_PORT" >>"$CFG"
  fi
  ok "$CFG (updated KODI_HOST)"
else
  port_line="# KODI_PORT=8080"
  [[ "$KODI_PORT" != 8080 ]] && port_line="KODI_PORT=$KODI_PORT"
  cat >"$CFG" <<EOF
# send2kodi settings. Every setting also works as an environment variable,
# which takes priority over this file; command-line flags beat both.

KODI_HOST=$KODI_HOST
$port_line

# Kodi web server login, if you set one in Settings > Services > Control.
# KODI_USER=kodi
# KODI_PASS=

# YOUTUBE=addon       # addon: YouTube add-on (fast); ytdlp: SendToKodi
# BACKGROUND=no       # yes: always serve local files in the background
# START_TIMEOUT=60    # seconds to wait for playback before reporting failure
# SERVE_PORT=8765     # port local files are served on (any free one if taken)
# SERVE_IP=           # address Kodi reaches this machine on (autodetected)
EOF
  chmod 600 "$CFG"
  ok "$CFG"
fi

# ------------------------------------------------------------------ report --
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) echo
     warn "$BIN_DIR is not in your PATH. Add to your shell rc:"
     warn "    export PATH=\"\$PATH:$BIN_DIR\"" ;;
esac

cat <<EOF

Done. Sending to $KODI_HOST.

  send2kodi https://youtu.be/...      play a link
  send2kodi ~/Videos/movie.mkv        play a file from this machine
  send2kodi ~/Videos/Some.Show/       play a folder, in order
  send2kodi -q URL_OR_FILE ...        add to the queue
  send2kodi --help                    everything else
  Settings    $CFG
EOF
(( needs_login )) && warn "remember to set KODI_USER and KODI_PASS in $CFG."
exit 0
