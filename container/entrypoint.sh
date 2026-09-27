#!/bin/sh
set -eu
umask 077
export XDG_RUNTIME_DIR=${XDG_RUNTIME_DIR:-/tmp/voip-runtime-$(id -u)}
mkdir -p "$XDG_RUNTIME_DIR" "${HOME:-/tmp/voice-home}"
chmod 700 "$XDG_RUNTIME_DIR"
export PULSE_SERVER="unix:$XDG_RUNTIME_DIR/pulse/native"
exec dbus-run-session -- python3 /opt/voip/container/audio_runner.py "$@"
