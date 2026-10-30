#!/usr/bin/env bash
# Render the Asterisk config from the SIP_* keys in $HERMES_HOME/.env and (re)create the Asterisk container.
set -euo pipefail

usage() {
  cat <<EOF
Usage: $(basename "$0") [options]

Reads SIP_SERVER, SIP_USER, SIP_PASSWORD and SIP_IDENTIFY_EXTRA from \$HERMES_HOME/.env
(HERMES_HOME defaults to ~/.hermes), renders the Asterisk config into
\$HERMES_HOME/phone/asterisk/ and (re)creates the Asterisk container.

Options:
  --name NAME              container name (default: hermes-3310-asterisk)
  --sip-port PORT          local SIP UDP port (default: 5070)
  --rtp-range START-END    RTP UDP port range (default: 16000-16200)
  --audiosocket-port PORT  phone adapter AudioSocket port on 127.0.0.1 (default: 9092)
  --calls-dir DIR          caller number handoff dir (default: \$HERMES_HOME/phone/calls)
  --outgoing-dir DIR       call files from the plugin (default: \$HERMES_HOME/phone/outgoing)
  --image IMAGE            Asterisk image (default: andrius/asterisk:23.5.0_debian-trixie, the tested one)
  --render-only            render the config and stop, without Docker
  -h, --help               show this help
EOF
}

die() {
  echo "$(basename "$0"): $*" >&2
  exit 1
}

hermes_home=${HERMES_HOME:-$HOME/.hermes}
name=hermes-3310-asterisk
sip_port=5070
rtp_range=16000-16200
audiosocket_port=9092
calls_dir=$hermes_home/phone/calls
outgoing_dir=$hermes_home/phone/outgoing
image=andrius/asterisk:23.5.0_debian-trixie
render_only=false

while (($#)); do
  case $1 in
    --render-only) render_only=true; shift; continue ;;
    -h | --help) usage; exit 0 ;;
  esac
  (($# >= 2)) || die "unknown option or missing value: $1 (see --help)"
  case $1 in
    --name) name=$2 ;;
    --sip-port) sip_port=$2 ;;
    --rtp-range) rtp_range=$2 ;;
    --audiosocket-port) audiosocket_port=$2 ;;
    --calls-dir) calls_dir=$2 ;;
    --outgoing-dir) outgoing_dir=$2 ;;
    --image) image=$2 ;;
    *) die "unknown option: $1 (see --help)" ;;
  esac
  shift 2
done

[[ $sip_port =~ ^[0-9]+$ && $audiosocket_port =~ ^[0-9]+$ ]] || die "ports must be numbers"
[[ $rtp_range =~ ^([0-9]+)-([0-9]+)$ ]] || die "--rtp-range must look like 16000-16200"
rtp_start=${BASH_REMATCH[1]}
rtp_end=${BASH_REMATCH[2]}

env_file=$hermes_home/.env
[[ -r $env_file ]] || die "cannot read $env_file"

# Sets the variable named $1 from the last "$1=value" line of .env, if there is one, read the way
# Hermes (python-dotenv) reads it: optional "export ", quotes with \" and \\ escapes inside double
# quotes, and a " #" comment after the value. The file is never executed.
read_env_key() {
  local line value double='^"(([^"\\]|\\.)*)"' single="^'([^']*)'"
  line=$(grep -E "^[[:space:]]*(export[[:space:]]+)?$1[[:space:]]*=" "$env_file" | tail -n 1) || return 0
  value=${line#*=}
  value=${value#"${value%%[![:space:]]*}"}
  if [[ $value =~ $double ]]; then
    value=$(printf '%s' "${BASH_REMATCH[1]}" | sed -E 's/\\(["\\])/\1/g')
  elif [[ $value =~ $single ]]; then
    value=${BASH_REMATCH[1]}
  else
    value=${value%%[[:space:]]#*}
    value=${value%"${value##*[![:space:]]}"}
  fi
  printf -v "$1" '%s' "$value"
}

unset SIP_SERVER SIP_USER SIP_PASSWORD SIP_IDENTIFY_EXTRA
for key in SIP_SERVER SIP_USER SIP_PASSWORD SIP_IDENTIFY_EXTRA; do
  read_env_key "$key"
done
for key in SIP_SERVER SIP_USER SIP_PASSWORD; do
  [[ -n ${!key-} ]] || die "$key is missing in $env_file"
done
# Odorik's media servers are outside sip.odorik.cz's address. An explicit empty value disables this.
if [[ $SIP_SERVER == sip.odorik.cz && -z ${SIP_IDENTIFY_EXTRA+x} ]]; then
  SIP_IDENTIFY_EXTRA=match=81.31.45.0/24
fi
export SIP_SERVER SIP_USER SIP_PASSWORD SIP_IDENTIFY_EXTRA=${SIP_IDENTIFY_EXTRA-}
export SIP_PORT=$sip_port RTP_START=$rtp_start RTP_END=$rtp_end AUDIOSOCKET_PORT=$audiosocket_port

umask 077 # pjsip.conf contains the SIP password
conf_dir=$hermes_home/phone/asterisk
mkdir -p "$conf_dir" "$calls_dir" "$outgoing_dir"
chmod 700 "$conf_dir" "$calls_dir" "$outgoing_dir" # any call file put there is dialled
# docker -v needs absolute paths
conf_dir=$(cd "$conf_dir" && pwd)
calls_dir=$(cd "$calls_dir" && pwd)
outgoing_dir=$(cd "$outgoing_dir" && pwd)
script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

# Only our variables are substituted; Asterisk's own ${...} expressions stay intact.
vars='${SIP_SERVER} ${SIP_USER} ${SIP_PASSWORD} ${SIP_IDENTIFY_EXTRA} ${SIP_PORT} ${RTP_START} ${RTP_END} ${AUDIOSOCKET_PORT}'
mounts=()
for src in "$script_dir"/asterisk/*; do
  dest=$conf_dir/$(basename "$src" .tpl)
  if [[ $src == *.tpl ]]; then
    envsubst "$vars" <"$src" >"$dest"
  else
    cp "$src" "$dest"
  fi
  mounts+=(-v "$dest:/etc/asterisk/${dest##*/}:ro")
done
echo "Rendered the Asterisk config into $conf_dir"
$render_only && exit 0

# The image's entrypoint remaps its asterisk user to PUID:PGID. Running Asterisk as the host user
# lets it read the 0600 configs, create caller files in the 0700 calls dir that we can delete, and
# take the plugin's call files from the 0700 outgoing dir.
docker rm -f -v "$name" >/dev/null 2>&1 || true   # -v: its volumes too, which hold the old log
docker run -d --name "$name" --restart unless-stopped --network host \
  -e ASTERISK_TERMINAL_OPTS=-n -e PUID="$(id -u)" -e PGID="$(id -g)" \
  "${mounts[@]}" -v "$calls_dir:/calls" -v "$outgoing_dir:/var/spool/asterisk/outgoing" "$image" >/dev/null

echo "Started container $name. Check the SIP registration with:"
echo "  docker exec $name asterisk -rx 'pjsip show registrations'"
