#!/bin/sh
# owl-node container entrypoint: owl-node <agent|audio|video-enc|video-dec|controller|dsp|version|shell>
set -e
role="${1:-agent}"
case "$role" in
  shell) shift; exec /bin/bash "$@";;
  audio|dsp)
    # the host's PipeWire: /run/owl-host-user is the host user's runtime dir, mounted read-only
    : "${PIPEWIRE_RUNTIME_DIR:=/run/owl-host-user}"; export PIPEWIRE_RUNTIME_DIR
    [ -S "$PIPEWIRE_RUNTIME_DIR/pipewire-0" ] || { echo "no PipeWire socket at $PIPEWIRE_RUNTIME_DIR/pipewire-0 (is the host user's PipeWire running?)" >&2; exit 1; }
    ;;
esac
exec python3 -m owlnode "$@"
