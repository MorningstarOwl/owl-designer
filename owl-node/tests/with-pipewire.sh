#!/usr/bin/env bash
# Run a command against a private, headless PipeWire + WirePlumber (the live tests need one), then tear it down.
#   tests/with-pipewire.sh python3 -m unittest discover -s tests
# Needs pipewire, wireplumber and dbus-launch (Debian/Ubuntu: pipewire pipewire-bin wireplumber dbus-x11). The runtime
# dir is short on purpose: a PipeWire socket path must fit in 108 bytes. A null sink is the graph's driver.
set -euo pipefail
RT=$(mktemp -d /tmp/owlpw.XXXXXX)
chmod 700 "$RT"
export XDG_RUNTIME_DIR="$RT"
PIDS=()
cleanup() {
  for p in "${PIDS[@]}"; do kill "$p" 2>/dev/null || true; done
  [ -n "${DBUS_SESSION_BUS_PID:-}" ] && kill "$DBUS_SESSION_BUS_PID" 2>/dev/null || true
  rm -rf -- "$RT"
}
trap cleanup EXIT
eval "$(dbus-launch --sh-syntax)"
pipewire >"$RT/pipewire.log" 2>&1 & PIDS+=($!)
for _ in $(seq 50); do [ -S "$RT/pipewire-0" ] && break; sleep 0.1; done
[ -S "$RT/pipewire-0" ] || { echo "pipewire did not start:" >&2; cat "$RT/pipewire.log" >&2; exit 1; }
wireplumber >"$RT/wireplumber.log" 2>&1 & PIDS+=($!)
sleep 2
pw-cli create-node adapter '{ factory.name = support.null-audio-sink node.name = owl-test-driver media.class = Audio/Sink
  object.linger = true audio.position = [ FL FR ] node.driver = true priority.driver = 1000 }' >/dev/null
sleep 0.5
"$@"
