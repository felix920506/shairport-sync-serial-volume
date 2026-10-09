#!/bin/bash
# Starts the private D-Bus system bus, Avahi, NQPTP, ser2net (optional),
# Shairport Sync and the volume bridge. If any long-running process exits, the container exits so the
# orchestrator (restart policy) can bring everything back up together.
#
# Environment:
#   ENABLE_AVAHI=1          start avahi-daemon inside the container
#   ENABLE_NQPTP=1          start nqptp (disable if the host already runs one)
#   ENABLE_SER2NET=0        start ser2net: shares the amplifier's serial port
#                           (or a single-client serial server) between TCP clients
#   SER2NET_CONFIG          path to ser2net's YAML config
#   ENABLE_VOLUME_BRIDGE=1  start the volume bridge
#   VOLUME_BRIDGE_CONFIG    path to the bridge's TOML config
#   SPS_NAME                AirPlay service name (overrides general.name)
#   ALSA_DEVICE             ALSA output device (overrides alsa.output_device)
#   ALSA_OUTPUT_FORMAT      ALSA sample format, e.g. S24_3LE (overrides
#                           alsa.output_format, normally "auto": the deepest
#                           format the device offers)
# Any arguments are passed through to shairport-sync.
# su-exec is given only the user (no group) so supplementary groups such as
# "audio" are kept.

set -euo pipefail

log() { echo "[entrypoint] $*"; }

pids=()

shutdown() {
  log "shutting down"
  # In reverse start order, each one after the previous has exited, so nothing
  # loses a service it depends on (e.g. the D-Bus bus) while still running.
  for ((i = ${#pids[@]} - 1; i >= 0; i--)); do
    kill -TERM "${pids[i]}" 2>/dev/null || true
    wait "${pids[i]}" 2>/dev/null || true
  done
}
trap shutdown TERM INT

rm -f /run/dbus/dbus.pid /run/avahi-daemon/pid
dbus-uuidgen --ensure
dbus-daemon --system --nofork --nopidfile &
pids+=($!)
until [ -S /run/dbus/system_bus_socket ]; do sleep 0.1; done
log "D-Bus system bus is up"

if [ "${ENABLE_AVAHI:-1}" = "1" ]; then
  avahi-daemon --no-chroot --no-drop-root --no-rlimits &
  pids+=($!)
  log "Avahi started"
fi

if [ "${ENABLE_NQPTP:-1}" = "1" ]; then
  # nqptp must bind UDP ports 319/320, so it stays root.
  /usr/local/bin/nqptp &
  pids+=($!)
  log "NQPTP started"
fi

if [ "${ENABLE_SER2NET:-0}" = "1" ]; then
  # Before the volume bridge, which connects through it. -d: stay in the
  # foreground and log to stdout; -u: no UUCP lock files (they'd need root, and
  # nothing else in the container uses the port).
  su-exec shairport-sync ser2net -d -u -c "${SER2NET_CONFIG:-/etc/ser2net/ser2net.yaml}" &
  pids+=($!)
  log "ser2net started"
fi

sps_args=()
if [ -n "${ALSA_OUTPUT_FORMAT:-}" ]; then
  # Shairport Sync has no command-line option for the output format, so run it
  # with a copy of the config that sets it.
  if [[ ! "$ALSA_OUTPUT_FORMAT" =~ ^[A-Za-z0-9_]+$ ]]; then
    log "invalid ALSA_OUTPUT_FORMAT \"$ALSA_OUTPUT_FORMAT\""
    exit 1
  fi
  sed -E "s/^([[:space:]]*output_format[[:space:]]*=[[:space:]]*)\"[^\"]*\";/\1\"${ALSA_OUTPUT_FORMAT}\";/" \
    /etc/shairport-sync.conf > /run/shairport-sync.conf
  if ! grep -Eq "^[[:space:]]*output_format[[:space:]]*=[[:space:]]*\"${ALSA_OUTPUT_FORMAT}\";" /run/shairport-sync.conf; then
    log "ALSA_OUTPUT_FORMAT: no output_format setting found in /etc/shairport-sync.conf"
    exit 1
  fi
  sps_args+=(--configfile=/run/shairport-sync.conf)
  log "ALSA output format: $ALSA_OUTPUT_FORMAT"
fi
[ -n "${SPS_NAME:-}" ] && sps_args+=(--name "$SPS_NAME")
sps_args+=("$@")
if [ -n "${ALSA_DEVICE:-}" ]; then
  sps_args+=(-- -d "$ALSA_DEVICE")
fi

su-exec shairport-sync /usr/local/bin/shairport-sync "${sps_args[@]}" &
pids+=($!)
log "Shairport Sync started"

if [ "${ENABLE_VOLUME_BRIDGE:-1}" = "1" ]; then
  su-exec shairport-sync \
    /opt/volume-bridge/bin/volume-bridge --config "${VOLUME_BRIDGE_CONFIG:-/etc/volume-bridge.toml}" &
  pids+=($!)
  log "Volume bridge started"
fi

# Exit as soon as any child exits.
set +e
wait -n "${pids[@]}"
status=$?
log "a service exited with status $status"
shutdown
exit "$status"
