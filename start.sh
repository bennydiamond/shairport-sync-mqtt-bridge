#!/bin/sh
set -e

export AIRPLAY_NAME="${AIRPLAY_NAME:-ShairportSync}"
export ALSA_CARD="${ALSA_CARD:-0}"
export ALSA_OUTPUT_DEVICE="${ALSA_OUTPUT_DEVICE:-hw:${ALSA_CARD}}"
export ALSA_CONTROL="${ALSA_CONTROL:-PCM}"
export USE_HARDWARE_MIXER="${USE_HARDWARE_MIXER:-auto}"
export MQTT_HOST="${MQTT_HOST:-127.0.0.1}"
export MQTT_PORT="${MQTT_PORT:-1883}"
export MQTT_USER="${MQTT_USER:-shairport}"
export MQTT_PASSWORD="${MQTT_PASSWORD:-shairport}"
export MQTT_DISCOVERY_PREFIX="${MQTT_DISCOVERY_PREFIX:-homeassistant}"
export MQTT_TOPIC_PREFIX="${MQTT_TOPIC_PREFIX:-garage_audio/}"
export SHAIRPORT_MQTT_TOPIC="${SHAIRPORT_MQTT_TOPIC:-${MQTT_TOPIC_PREFIX%/}/shairport}"
export ENABLE_VOLUME_BRIDGE="${ENABLE_VOLUME_BRIDGE:-true}"

# Auto-detect whether the ALSA control has hardware mute (`pswitch`) if USE_HARDWARE_MIXER=auto
if [ "$USE_HARDWARE_MIXER" = "auto" ]; then
    if amixer -c "$ALSA_CARD" sget "$ALSA_CONTROL" 2>/dev/null | grep -q "pswitch"; then
        USE_HARDWARE_MIXER="true"
    else
        USE_HARDWARE_MIXER="false"
    fi
fi

if [ "$USE_HARDWARE_MIXER" = "true" ]; then
    export VOLUME_RANGE_DB="${VOLUME_RANGE_DB:-64}"
    export ALSA_MIXER_BLOCK="    mixer_control_name = \"${ALSA_CONTROL}\";
    use_hardware_mute_if_available = \"yes\";"
else
    export VOLUME_RANGE_DB="${VOLUME_RANGE_DB:-30}"
    export ALSA_MIXER_BLOCK="    // Software volume controlled via D-Bus and ALSA volume_bridge.py"
fi

# Render /etc/shairport-sync.conf unless a custom config is mounted with USE_CUSTOM_CONFIG=true
if [ "${USE_CUSTOM_CONFIG:-false}" != "true" ] && [ -f /etc/shairport-sync.conf.template ]; then
    envsubst '${AIRPLAY_NAME} ${VOLUME_RANGE_DB} ${ALSA_OUTPUT_DEVICE} ${ALSA_MIXER_BLOCK} ${MQTT_HOST} ${MQTT_PORT} ${MQTT_USER} ${MQTT_PASSWORD} ${SHAIRPORT_MQTT_TOPIC} ${MQTT_DISCOVERY_PREFIX}' \
        < /etc/shairport-sync.conf.template > /etc/shairport-sync.conf
fi

# Clean up any stale runtime PID files
mkdir -p /var/run/dbus /var/run/avahi-daemon
rm -f /var/run/dbus/dbus.pid /var/run/dbus/pid /var/run/avahi-daemon/pid

# 1. Start System D-Bus
dbus-uuidgen --ensure
dbus-daemon --system
sleep 1

# 2. Start Avahi mDNS daemon
avahi-daemon --no-drop-root -D
sleep 1

# 3. Start NQPTP (AirPlay 2 timing)
nqptp &

# 4. Start Shairport-Sync
shairport-sync -c /etc/shairport-sync.conf &
SPS_PID=$!

# 5. Start ALSA <-> D-Bus <-> MQTT Volume Bridge (if enabled)
if [ "$ENABLE_VOLUME_BRIDGE" = "true" ]; then
    sleep 2
    python3 -u /usr/local/bin/volume_bridge.py &
    BRIDGE_PID=$!
    while kill -0 "$SPS_PID" 2>/dev/null && kill -0 "$BRIDGE_PID" 2>/dev/null; do
        sleep 2
    done
    exit 1
else
    wait "$SPS_PID"
fi
