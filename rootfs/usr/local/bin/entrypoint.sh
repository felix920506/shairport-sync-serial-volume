#!/bin/bash
# Starts the private D-Bus system bus, Avahi, NQPTP, Shairport Sync and the
# volume bridge. If any long-running process exits, the container exits so the
# orchestrator (restart policy) can bring everything back up together.
#
# Environment:
#   ENABLE_AVAHI=1          start avahi-daemon inside the container
#   ENABLE_NQPTP=1          start nqptp (disable if the host already runs one)
#   ENABLE_VOLUME_BRIDGE=1  start the volume bridge
#   VOLUME_BRIDGE_CONFIG    path to the bridge's TOML config
#   SPS_NAME                AirPlay service name (overrides general.name)
#   ALSA_DEVICE             ALSA output device (overrides alsa.output_device)
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

sps_args=()
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
