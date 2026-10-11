#!/usr/bin/env bash
#
# Janken tournament server installer.
#
#   ./install.sh
#
# Sets up the Python environment with uv, seeds the registry with the house
# bots, and generates systemd units for the dashboard + a periodic tournament.
# Re-runnable: it won't reseed a populated registry.
#
# Override defaults via env, e.g.:  JANKEN_PORT=80 JANKEN_INTERVAL=5min
#                                  JANKEN_NORM_INTERVAL_HOURS=12 ./install.sh
#
set -euo pipefail

# --- config -----------------------------------------------------------------
# Defaults are tuned for a cheap, always-on burstable box (e.g. t3.small):
# a generous interval keeps it mostly idle, and a random schedule keeps each
# run short at any field size.
PORT="${JANKEN_PORT:-6888}"            # dashboard port (6888 = "MUTT" on a keypad)
INTERVAL="${JANKEN_INTERVAL:-30min}"   # how often the tournament runs
ROUNDS="${JANKEN_ROUNDS:-200}"         # rounds per match in the periodic run
GAMES="${JANKEN_GAMES:-26}"            # games/bot random schedule (0 for a full round-robin)
WORKERS="${JANKEN_WORKERS:-2}"         # parallel workers (caps CPU/RAM for a 2-vCPU box)
# Hours between norm events — the selective tournaments that award IM/GM norms.
# Spaced so the invited field turns over between them; 0 holds one every run.
NORM_INTERVAL="${JANKEN_NORM_INTERVAL_HOURS:-24}"

# The dashboard estimates the *next* run as "last run + interval", and wants that
# interval in whole minutes. Only the plain "30min" form converts unambiguously —
# a bare number is *seconds* to systemd, and "1h30min" isn't one unit — so
# anything else is left unset and the dashboard falls back to its own default
# rather than being handed a number in the wrong unit.
INTERVAL_MIN=""
case "$INTERVAL" in
  *min) case "${INTERVAL%min}" in ""|*[!0-9]*) ;; *) INTERVAL_MIN="${INTERVAL%min}" ;; esac ;;
esac
INTERVAL_ENV=""
[ -n "$INTERVAL_MIN" ] && INTERVAL_ENV="Environment=JANKEN_INTERVAL_MIN=$INTERVAL_MIN"

# Google sign-in. With a client id + secret (and the redirect URI they were
# registered against) the installer writes .streamlit/secrets.toml, generating the
# cookie secret; without them the dashboard serves open, unless
# JANKEN_REQUIRE_SSO=1 tells it to refuse instead.
CLIENT_ID="${JANKEN_GOOGLE_CLIENT_ID:-}"
CLIENT_SECRET="${JANKEN_GOOGLE_CLIENT_SECRET:-}"
REDIRECT_URI="${JANKEN_REDIRECT_URI:-}"   # required when writing secrets.toml
DOMAINS="${JANKEN_ALLOWED_DOMAINS:-muttdata.ai}"
# ----------------------------------------------------------------------------

HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"
DB="$HERE/janken.db"
RUN_USER="$(id -un)"

echo "==> [1/7] uv"
if ! command -v uv >/dev/null 2>&1; then
  echo "    installing uv..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi
uv --version

echo "==> [2/7] dependencies (uv sync)"
uv sync
UV="$(command -v uv)"

echo "==> [3/7] sandbox (bubblewrap)"
# Bots run under bwrap: no network, read-only filesystem, home masked (see
# janken/match.py). Install it, then probe whether unprivileged user namespaces
# actually work here. The services default to *requiring* the sandbox, so a
# broken one fails loudly instead of silently running untrusted bots with full
# access to this box. Override with JANKEN_REQUIRE_SANDBOX=0 to allow the
# (unsafe) unsandboxed fallback.
if ! command -v bwrap >/dev/null 2>&1; then
  echo "    installing bubblewrap..."
  if command -v dnf >/dev/null 2>&1; then sudo dnf install -y bubblewrap || true
  elif command -v yum >/dev/null 2>&1; then sudo yum install -y bubblewrap || true
  elif command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update -qq && sudo apt-get install -y bubblewrap || true
  else
    echo "    no known package manager — install 'bubblewrap' manually." >&2
  fi
fi
REQUIRE_SANDBOX="${JANKEN_REQUIRE_SANDBOX:-1}"
PROBE_LOG="$(mktemp)"
trap 'rm -f "$PROBE_LOG"' EXIT
if uv run python -c "import sys; from janken import match; sys.exit(0 if match._sandbox_available() else 1)" 2>"$PROBE_LOG"; then
  echo "    bubblewrap OK — bots run sandboxed (no network, read-only FS, home masked)."
else
  # Show *why*, in bwrap's own words. Swallowing this stderr sends the operator
  # hunting kernel settings when the message usually names the real cause
  # outright (a missing bwrap, a blocked namespace, an unbindable interpreter).
  if [ -s "$PROBE_LOG" ]; then
    echo "    the sandbox probe reported:" >&2
    sed 's/^/      /' "$PROBE_LOG" >&2
  fi
  if [ "$REQUIRE_SANDBOX" = "1" ]; then
    echo "    WARNING: sandboxing does NOT work here and JANKEN_REQUIRE_SANDBOX=1, so the" >&2
    echo "    services will refuse to run bots until it is fixed. If the message above is" >&2
    echo "    about namespaces, enable unprivileged user namespaces and re-run:" >&2
    echo "      sudo sysctl -w user.max_user_namespaces=15000" >&2
    echo "      # Debian/Ubuntu also: sudo sysctl -w kernel.unprivileged_userns_clone=1" >&2
    echo "    Or set JANKEN_REQUIRE_SANDBOX=0 to allow the (unsafe) unsandboxed fallback." >&2
  else
    echo "    WARNING: sandboxing does not work and JANKEN_REQUIRE_SANDBOX=0 —" >&2
    echo "    bots will run WITHOUT isolation (full access to this box)." >&2
  fi
fi
# Passed to both services; empty (no line) when the operator opts out.
SANDBOX_ENV=""
[ "$REQUIRE_SANDBOX" = "1" ] && SANDBOX_ENV="Environment=JANKEN_REQUIRE_SANDBOX=1"

echo "==> [4/7] registry"
mkdir -p "$HERE/submissions"
EMPTY="$(uv run python - "$DB" <<'PY'
import sys
sys.path.insert(0, ".")
from janken.registry import Registry
r = Registry(sys.argv[1])
print("empty" if not r.list_bots() else "has")
r.close()
PY
)"
if [ "$EMPTY" = "empty" ]; then
  echo "    seeding house bots..."
  uv run python -m janken run "$HERE/bots" --db "$DB" --rounds 100 --matches 1 >/dev/null
else
  echo "    already populated — leaving as is"
fi

echo "==> [5/7] sign-in (Google SSO)"
SECRETS="$HERE/.streamlit/secrets.toml"
if [ -f "$SECRETS" ]; then
  # Never overwritten: rewriting it would rotate cookie_secret (logging everyone
  # out) and throw away any hand-edited settings.
  echo "    .streamlit/secrets.toml already present — left untouched."
elif [ -n "$CLIENT_ID" ] && [ -n "$CLIENT_SECRET" ]; then
  # No default: the callback URL is specific to wherever this box is reachable,
  # and it has to match the OAuth client character for character. Guessing it
  # would only surface later as Google's opaque redirect_uri_mismatch.
  if [ -z "$REDIRECT_URI" ]; then
    echo "    ERROR: set JANKEN_REDIRECT_URI to this deployment's callback URL," >&2
    echo "    e.g. JANKEN_REDIRECT_URI=https://<your-host>/oauth2callback — the same" >&2
    echo "    value you added as an authorized redirect URI on the OAuth client." >&2
    exit 1
  fi
  # Google rejects plain http and raw IPs, and Streamlit only serves the callback
  # at .../oauth2callback — catch a malformed URL here too.
  case "$REDIRECT_URI" in
    https://*/oauth2callback) ;;
    http://localhost/oauth2callback|http://localhost:*/oauth2callback) ;;
    http://127.0.0.1/oauth2callback|http://127.0.0.1:*/oauth2callback) ;;
    *)
      echo "    ERROR: JANKEN_REDIRECT_URI is $REDIRECT_URI" >&2
      echo "    Expected https://<host>/oauth2callback (plain http only for" >&2
      echo "    localhost), no trailing slash, matching the OAuth client exactly." >&2
      exit 1
      ;;
  esac
  echo "    writing .streamlit/secrets.toml for $REDIRECT_URI"
  mkdir -p "$HERE/.streamlit"
  # uv's Python, since the box may not have its own python3.
  COOKIE_SECRET="$(uv run python -c 'import secrets; print(secrets.token_urlsafe(48))')"
  # `hd` only pre-selects a Workspace on Google's account chooser, so it makes
  # sense for a single domain; with several, leave the chooser alone and let
  # janken/auth.py admit them all.
  HD_LINE=""
  case "$DOMAINS" in
    *,*) ;;
    *) HD_LINE="authorize_params = { hd = \"$DOMAINS\" }" ;;
  esac
  # Subshell umask: the file is never briefly world-readable.
  (
    umask 077
    cat > "$SECRETS" <<EOF
# Written by install.sh. Credentials — keep out of git (.gitignore covers it).

[auth]
redirect_uri = "$REDIRECT_URI"
cookie_secret = "$COOKIE_SECRET"

[auth.google]
client_id = "$CLIENT_ID"
client_secret = "$CLIENT_SECRET"
server_metadata_url = "https://accounts.google.com/.well-known/openid-configuration"
$HD_LINE
client_kwargs = { prompt = "select_account" }
EOF
  )
  echo "    done — sign-in is on, admitting $DOMAINS accounts only."
  echo "    Make sure $REDIRECT_URI is an authorized redirect URI on the OAuth client."
else
  echo "    no credentials and no .streamlit/secrets.toml — the dashboard will be OPEN"
  echo "    to anyone who can reach port $PORT. To switch sign-in on, re-run with:"
  echo "      JANKEN_GOOGLE_CLIENT_ID=... JANKEN_GOOGLE_CLIENT_SECRET=... ./install.sh"
  echo "    See 'Sign-in (SSO)' in README.md."
fi

echo "==> [6/7] systemd units -> deploy/"
mkdir -p "$HERE/deploy"
chmod +x "$HERE/scripts/run_tournament.sh"

# Ports below 1024 are privileged. Rather than run the dashboard as root, grant
# the service just the one capability it needs to bind such a port — useful when
# a load balancer's security group only reaches the instance on port 80.
CAP_LINE=""
case "$PORT" in
  ''|*[!0-9]*) echo "JANKEN_PORT must be a number, got '$PORT'" >&2; exit 1 ;;
esac
if [ "$PORT" -lt 1024 ]; then
  CAP_LINE="AmbientCapabilities=CAP_NET_BIND_SERVICE"
fi

# Sign-in settings, passed through only when set (see "Sign-in (SSO)" in the
# README). The credentials themselves live in .streamlit/secrets.toml, which the
# service picks up from WorkingDirectory.
SSO_ENV=""
[ -n "${JANKEN_REQUIRE_SSO:-}" ] &&
  SSO_ENV="Environment=JANKEN_REQUIRE_SSO=$JANKEN_REQUIRE_SSO"
[ -n "${JANKEN_ALLOWED_DOMAINS:-}" ] &&
  SSO_ENV="$SSO_ENV
Environment=JANKEN_ALLOWED_DOMAINS=$JANKEN_ALLOWED_DOMAINS"

cat > "$HERE/deploy/janken-dashboard.service" <<EOF
[Unit]
Description=Janken dashboard (Streamlit)
After=network.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$HERE
$CAP_LINE
$SANDBOX_ENV
$SSO_ENV
$INTERVAL_ENV
ExecStart=$UV run streamlit run $HERE/app.py --server.port $PORT --server.address 0.0.0.0 --server.headless true --browser.gatherUsageStats false -- --db $DB
Restart=on-failure

[Install]
WantedBy=multi-user.target
EOF

# A round-robin used to be spelled JANKEN_GAMES= (empty). It is 0 now, since the
# CLI's own default is 26 and an unset variable no longer means "everybody plays
# everybody". Both spellings still work: empty becomes 0 here.
[ -z "$GAMES" ] && GAMES=0
GAMES_ENV="Environment=JANKEN_GAMES=$GAMES"
cat > "$HERE/deploy/janken-tournament.service" <<EOF
[Unit]
Description=Janken periodic tournament (fielded from the registry)

[Service]
Type=oneshot
User=$RUN_USER
WorkingDirectory=$HERE
$SANDBOX_ENV
Environment=JANKEN_DB=$DB
Environment=JANKEN_ROUNDS=$ROUNDS
Environment=JANKEN_WORKERS=$WORKERS
Environment=JANKEN_NORM_INTERVAL_HOURS=$NORM_INTERVAL
$GAMES_ENV
ExecStart=$HERE/scripts/run_tournament.sh
EOF

cat > "$HERE/deploy/janken-tournament.timer" <<EOF
[Unit]
Description=Run the Janken tournament every $INTERVAL

[Timer]
OnBootSec=2min
OnUnitActiveSec=$INTERVAL
Persistent=true

[Install]
WantedBy=timers.target
EOF

echo "==> [7/7] enable services"
UNITS="deploy/janken-dashboard.service deploy/janken-tournament.service deploy/janken-tournament.timer"
if [ "${JANKEN_NO_SERVICE:-0}" = "1" ]; then
  echo "    JANKEN_NO_SERVICE=1 — skipping (units are in deploy/)."
elif command -v systemctl >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
  echo "    installing + starting via systemd..."
  sudo cp $UNITS /etc/systemd/system/
  sudo systemctl daemon-reload
  sudo systemctl enable --now janken-dashboard.service
  sudo systemctl enable --now janken-tournament.timer
  cat <<EOF

Done — both are running.
  Dashboard : http://<server-ip>:$PORT   (open port $PORT in the EC2 security group
              — to the load balancer's group only if one fronts this box)
  Tournament: every $INTERVAL
  Check     : systemctl status janken-dashboard ; systemctl list-timers 'janken*'
EOF
elif command -v systemctl >/dev/null 2>&1; then
  cat <<EOF

Environment ready. Passwordless sudo unavailable — finish with:
  sudo cp $UNITS /etc/systemd/system/
  sudo systemctl daemon-reload
  sudo systemctl enable --now janken-dashboard.service janken-tournament.timer
EOF
else
  cat <<EOF

Environment ready, but no systemd here. Launch the dashboard with:
  python3 -m janken dashboard --db $DB --port $PORT
and run the tournament from cron:
  */10 * * * * $HERE/scripts/run_tournament.sh >> $HERE/tournament.log 2>&1
EOF
fi
