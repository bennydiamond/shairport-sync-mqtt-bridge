#!/usr/bin/env python3
"""
Unified Bidirectional Volume & Mute Bridge for Shairport-Sync, ALSA, and Home Assistant (MQTT).

Supports both:
- Native PC/DAC hardware mixers (e.g. x86_64 HDA Intel 'Master' with hardware mute switch `pswitch`)
- Raspberry Pi `bcm2835 Headphones` ('PCM' 0..255 raw range -102.38dB..+4.00dB mapped to 30dB human-ear curve)

Preserves volume level across mute/unmute and keeps ALSA, Shairport-Sync D-Bus, and Home Assistant MQTT
synchronized in real time.
"""
import json
import logging
import logging.handlers
import os
import re
import select
import subprocess
import sys
import threading
import time

import alsaaudio
import dbus
import dbus.mainloop.glib
from gi.repository import GLib
import paho.mqtt.client as mqtt

# --- Configuration from Environment Variables ---
MQTT_HOST = os.getenv("MQTT_HOST", "127.0.0.1")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_USER = os.getenv("MQTT_USER", "shairport")
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD", "shairport")
DISCOVER_PREFIX = os.getenv("MQTT_DISCOVERY_PREFIX", "homeassistant")
TOPIC_PREFIX = os.getenv("MQTT_TOPIC_PREFIX", "garage_audio/")
if TOPIC_PREFIX and not TOPIC_PREFIX.endswith("/"):
    TOPIC_PREFIX += "/"
DEVICE_PREFIX = os.getenv("MQTT_DEVICE_PREFIX", "alsa_audio")
DEVICE_ID = os.getenv("DEVICE_ID", "garage_alsa_audio")
FRIENDLY_NAME = os.getenv("FRIENDLY_NAME", "Garage ALSA Audio")
DEVICE_NAME = os.getenv("DEVICE_NAME", FRIENDLY_NAME)
DEVICE_MANUFACTURER = os.getenv("DEVICE_MANUFACTURER", "ALSA")
DEVICE_MODEL = os.getenv("DEVICE_MODEL", "Hardware Mixer")
DEVICE_SW_VERSION = os.getenv("DEVICE_SW_VERSION", "2.0")

ALSA_CARD = int(os.getenv("ALSA_CARD", "0"))
ALSA_CONTROL = os.getenv("ALSA_CONTROL", "PCM")
ALSA_VOLUME_CURVE = os.getenv("ALSA_VOLUME_CURVE", "auto").lower()
VOLUME_SCALE = os.getenv("VOLUME_SCALE", "0_to_100").lower()  # "0_to_100" (0..100) or "0_to_1" (0.00..1.00)

SYSLOG_HOST = os.getenv("SYSLOG_HOST", "").strip()
SYSLOG_PORT = int(os.getenv("SYSLOG_PORT", "514"))

# --- Logging Setup ---
logger = logging.getLogger("volume_bridge")
logger.setLevel(logging.INFO)
formatter = logging.Formatter(f"{DEVICE_ID}-volume-bridge: %(levelname)s %(message)s")

if SYSLOG_HOST:
    try:
        syslog_handler = logging.handlers.SysLogHandler(
            address=(SYSLOG_HOST, SYSLOG_PORT),
            facility=logging.handlers.SysLogHandler.LOG_DAEMON,
        )
        syslog_handler.setFormatter(formatter)
        logger.addHandler(syslog_handler)
    except Exception as e:
        sys.stderr.write(f"Failed to attach syslog handler ({SYSLOG_HOST}:{SYSLOG_PORT}): {e}\n")

stdout_handler = logging.StreamHandler(sys.stdout)
stdout_handler.setFormatter(formatter)
logger.addHandler(stdout_handler)

# --- MQTT Topics ---
BASE_TOPIC = f"{TOPIC_PREFIX}{DEVICE_PREFIX}"
AVAILABILITY_TOPIC = f"{BASE_TOPIC}/availability"
VOL_STATE_TOPIC = f"{BASE_TOPIC}/{DEVICE_ID}/volume"
VOL_CMD_TOPIC = f"{BASE_TOPIC}/{DEVICE_ID}/set_volume"
MUTE_STATE_TOPIC = f"{BASE_TOPIC}/{DEVICE_ID}/mute"
MUTE_CMD_TOPIC = f"{BASE_TOPIC}/{DEVICE_ID}/set_mute"

# --- Raspberry Pi bcm2835 Constants (Raw 0..255: -102.38 dB to +4.00 dB, 0.417 dB/step) ---
ALSA_RAW_MIN = 0
ALSA_RAW_30DB = 165  # ~ -30.0 dB
ALSA_RAW_0DB = 245   # 0.00 dB


def detect_alsa_capabilities():
    """Inspect ALSA control capabilities via amixer to determine curve and hardware mute support."""
    has_pswitch = False
    raw_max = 100
    try:
        out = subprocess.check_output(
            ["amixer", "-c", str(ALSA_CARD), "sget", ALSA_CONTROL], text=True
        )
        if "pswitch" in out:
            has_pswitch = True
        m = re.search(r"Limits:\s+(?:Playback\s+)?\d+\s*-\s*(\d+)", out)
        if m:
            raw_max = int(m.group(1))
    except Exception as e:
        logger.warning(f"Could not inspect ALSA capabilities via amixer: {e}")

    if ALSA_VOLUME_CURVE == "auto":
        if raw_max == 255 and not has_pswitch:
            curve = "bcm2835"
        else:
            curve = "native"
    else:
        curve = ALSA_VOLUME_CURVE

    logger.info(
        f"ALSA card={ALSA_CARD} control='{ALSA_CONTROL}': raw_max={raw_max}, "
        f"has_pswitch={has_pswitch}, selected_curve='{curve}'"
    )
    return curve, has_pswitch, raw_max


CURVE_MODE, HAS_HW_MUTE, RAW_MAX = detect_alsa_capabilities()


def pct_to_shairport_db(pct: int) -> float:
    """Convert 0..100% slider to Shairport-Sync AirPlay dB (-30.0 to 0.0, or -144.0 for 0%)."""
    if pct <= 0:
        return -144.0
    if pct >= 100:
        return 0.0
    return round(-30.0 + (pct / 100.0) * 30.0, 2)


def shairport_db_to_pct(db_val: float) -> int:
    """Convert Shairport-Sync AirPlay dB (-30.0 to 0.0, -144.0 mute) to 0..100%."""
    if db_val <= -30.0:
        return 0
    if db_val >= 0.0:
        return 100
    return int(round(((db_val + 30.0) / 30.0) * 100.0))


def pct_to_bcm2835_raw(pct: int) -> int:
    """Map 0..100% across the 30 dB human-ear dynamic range (165..245 raw on bcm2835 PCM)."""
    if pct <= 0:
        return ALSA_RAW_MIN
    if pct >= 100:
        return ALSA_RAW_0DB
    return int(round(ALSA_RAW_30DB + (pct / 100.0) * (ALSA_RAW_0DB - ALSA_RAW_30DB)))


def bcm2835_raw_to_pct(raw: int) -> int:
    """Map bcm2835 PCM raw value (0..255) back to 0..100%."""
    if raw <= ALSA_RAW_MIN:
        return 0
    if raw <= ALSA_RAW_30DB:
        return 1
    if raw >= ALSA_RAW_0DB:
        return 100
    return int(round(((raw - ALSA_RAW_30DB) / float(ALSA_RAW_0DB - ALSA_RAW_30DB)) * 100.0))


class VolumeBridge:
    def __init__(self):
        self.lock = threading.Lock()
        self.saved_volume_pct = 50
        self.current_volume_pct = 50
        self.is_muted = False

        self.ignore_alsa_until = 0.0
        self.ignore_dbus_until = 0.0

        self.bus = None
        self.mqtt_client = None

        self._init_from_alsa()

    def _get_alsa_state(self):
        """Return (pct: int, is_muted: bool) from the ALSA mixer."""
        if CURVE_MODE == "bcm2835":
            try:
                out = subprocess.check_output(
                    ["amixer", "-c", str(ALSA_CARD), "sget", ALSA_CONTROL], text=True
                )
                m = re.search(r"Playback\s+(\d+)\s+\[", out)
                if m:
                    raw = int(m.group(1))
                    pct = bcm2835_raw_to_pct(raw)
                    return pct, (raw <= ALSA_RAW_MIN)
            except Exception as e:
                logger.error(f"Failed to read bcm2835 ALSA raw volume: {e}")
            return self.current_volume_pct, self.is_muted
        else:
            try:
                mixer = alsaaudio.Mixer(control=ALSA_CONTROL, cardindex=ALSA_CARD)
                vols = mixer.getvolume()
                pct = int(vols[0]) if vols else self.current_volume_pct
                muted = False
                if HAS_HW_MUTE:
                    try:
                        mutes = mixer.getmute()
                        if mutes and int(mutes[0]) == 1:
                            muted = True
                    except alsaaudio.ALSAAudioError:
                        pass
                else:
                    muted = pct == 0
                return pct, muted
            except Exception as e:
                logger.error(f"Failed to read native ALSA mixer: {e}")
                return self.current_volume_pct, self.is_muted

    def _set_alsa_state(self, pct: int, mute: bool):
        """Apply volume (0..100%) and mute state to the ALSA hardware mixer."""
        self.ignore_alsa_until = time.monotonic() + 0.6
        if CURVE_MODE == "bcm2835":
            raw = ALSA_RAW_MIN if mute else pct_to_bcm2835_raw(pct)
            try:
                subprocess.run(
                    ["amixer", "-q", "-c", str(ALSA_CARD), "sset", ALSA_CONTROL, str(raw)],
                    check=True,
                )
            except Exception as e:
                logger.error(f"Failed to set bcm2835 ALSA raw volume to {raw}: {e}")
        else:
            try:
                mixer = alsaaudio.Mixer(control=ALSA_CONTROL, cardindex=ALSA_CARD)
                if HAS_HW_MUTE:
                    if mute:
                        mixer.setmute(1)
                    else:
                        mixer.setvolume(int(pct))
                        mixer.setmute(0)
                else:
                    mixer.setvolume(0 if mute else int(pct))
            except Exception as e:
                logger.error(f"Failed to set native ALSA mixer (pct={pct}, mute={mute}): {e}")

    def _init_from_alsa(self):
        pct, muted = self._get_alsa_state()
        if muted or pct == 0:
            self.is_muted = True
            self.current_volume_pct = 0
            self.saved_volume_pct = pct if pct > 0 else 50
        else:
            self.is_muted = False
            self.current_volume_pct = pct
            self.saved_volume_pct = pct
        logger.info(
            f"Initialized from ALSA: volume={self.current_volume_pct}%, "
            f"muted={self.is_muted}, saved={self.saved_volume_pct}%"
        )

    def _set_shairport_dbus_volume(self, db_val: float):
        if not self.bus:
            return
        self.ignore_dbus_until = time.monotonic() + 0.6
        for iface_name in ("org.gnome.ShairportSync", "org.gnome.ShairportSync.RemoteControl"):
            try:
                obj = self.bus.get_object("org.gnome.ShairportSync", "/org/gnome/ShairportSync")
                props = dbus.Interface(obj, "org.freedesktop.DBus.Properties")
                props.Set(iface_name, "Volume", dbus.Double(db_val))
                return
            except Exception:
                continue

    def _format_mqtt_volume(self, pct: int) -> str:
        if VOLUME_SCALE == "0_to_100":
            return str(int(round(pct)))
        return f"{pct / 100.0:.2f}"

    def publish_state(self):
        if not self.mqtt_client:
            return
        vol_payload = self._format_mqtt_volume(self.current_volume_pct)
        mute_payload = "ON" if self.is_muted else "OFF"
        attr_payload = json.dumps({
            "volume_level": round(self.current_volume_pct / 100.0, 2),
            "volume_pct": self.current_volume_pct,
        })
        self.mqtt_client.publish(VOL_STATE_TOPIC, vol_payload, qos=1, retain=True)
        self.mqtt_client.publish(MUTE_STATE_TOPIC, mute_payload, qos=1, retain=True)
        self.mqtt_client.publish(f"{BASE_TOPIC}/{DEVICE_ID}/attributes", attr_payload, qos=1, retain=True)
        logger.info(
            f"Published MQTT state: volume={vol_payload} ({self.current_volume_pct}%), "
            f"mute={mute_payload} (saved={self.saved_volume_pct}%)"
        )

    def apply_volume_from_mqtt(self, pct: int):
        with self.lock:
            pct = max(0, min(100, int(round(pct))))
            if pct == 0:
                self.is_muted = True
                self.current_volume_pct = 0
                self._set_alsa_state(self.saved_volume_pct, True)
                self._set_shairport_dbus_volume(-144.0)
            else:
                self.is_muted = False
                self.current_volume_pct = pct
                self.saved_volume_pct = pct
                self._set_alsa_state(pct, False)
                self._set_shairport_dbus_volume(pct_to_shairport_db(pct))
            self.publish_state()

    def apply_mute_from_mqtt(self, mute: bool):
        with self.lock:
            if mute:
                if not self.is_muted and self.current_volume_pct > 0:
                    self.saved_volume_pct = self.current_volume_pct
                self.is_muted = True
                self.current_volume_pct = 0
                self._set_alsa_state(self.saved_volume_pct, True)
                self._set_shairport_dbus_volume(-144.0)
            else:
                self.is_muted = False
                restore_pct = self.saved_volume_pct if self.saved_volume_pct > 0 else 50
                self.current_volume_pct = restore_pct
                self._set_alsa_state(restore_pct, False)
                self._set_shairport_dbus_volume(pct_to_shairport_db(restore_pct))
            self.publish_state()

    def on_dbus_volume_changed(self, db_val: float):
        with self.lock:
            if time.monotonic() < self.ignore_dbus_until:
                return
            logger.info(f"Shairport-Sync D-Bus Volume changed to {db_val:.2f} dB")
            if db_val <= -140.0 or db_val <= -30.0:
                if not self.is_muted and self.current_volume_pct > 0:
                    self.saved_volume_pct = self.current_volume_pct
                self.is_muted = True
                self.current_volume_pct = 0
                self._set_alsa_state(self.saved_volume_pct, True)
            else:
                pct = shairport_db_to_pct(db_val)
                self.is_muted = False
                self.current_volume_pct = pct
                if pct > 0:
                    self.saved_volume_pct = pct
                self._set_alsa_state(pct, False)
            self.publish_state()

    def on_alsa_mixer_changed(self):
        with self.lock:
            if time.monotonic() < self.ignore_alsa_until:
                return
            pct, muted = self._get_alsa_state()
            if muted or pct == 0:
                if self.is_muted and self.current_volume_pct == 0:
                    return
                logger.info(f"External ALSA mixer change detected: muted={muted}, pct={pct}%")
                if not self.is_muted and self.current_volume_pct > 0:
                    self.saved_volume_pct = self.current_volume_pct
                self.is_muted = True
                self.current_volume_pct = 0
                self._set_shairport_dbus_volume(-144.0)
                self.publish_state()
            else:
                if not self.is_muted and pct == self.current_volume_pct:
                    return
                logger.info(f"External ALSA mixer change detected: pct={pct}% (unmuted)")
                self.is_muted = False
                self.current_volume_pct = pct
                self.saved_volume_pct = pct
                self._set_shairport_dbus_volume(pct_to_shairport_db(pct))
                self.publish_state()


def publish_discovery(client: mqtt.Client):
    import json

    device_info = {
        "identifiers": [DEVICE_ID],
        "name": DEVICE_NAME,
        "manufacturer": DEVICE_MANUFACTURER,
        "model": DEVICE_MODEL,
        "sw_version": DEVICE_SW_VERSION,
    }
    if VOLUME_SCALE == "0_to_100":
        min_val, max_val, step_val = 0, 100, 1
    else:
        min_val, max_val, step_val = 0.0, 1.0, 0.01

    vol_config = {
        "name": f"{FRIENDLY_NAME} Volume",
        "unique_id": f"{DEVICE_ID}_volume",
        "state_topic": VOL_STATE_TOPIC,
        "command_topic": VOL_CMD_TOPIC,
        "json_attributes_topic": f"{BASE_TOPIC}/{DEVICE_ID}/attributes",
        "availability_topic": AVAILABILITY_TOPIC,
        "min": min_val,
        "max": max_val,
        "step": step_val,
        "mode": "slider",
        "icon": "mdi:volume-high",
        "device": device_info,
    }
    mute_config = {
        "name": f"{FRIENDLY_NAME} Mute",
        "unique_id": f"{DEVICE_ID}_mute",
        "state_topic": MUTE_STATE_TOPIC,
        "command_topic": MUTE_CMD_TOPIC,
        "availability_topic": AVAILABILITY_TOPIC,
        "payload_on": "ON",
        "payload_off": "OFF",
        "state_on": "ON",
        "state_off": "OFF",
        "icon": "mdi:volume-mute",
        "device": device_info,
    }
    # Clear any legacy alternate discovery topics
    client.publish(f"{DISCOVER_PREFIX}/number/{DEVICE_ID}/volume/config", "", qos=1, retain=True)
    client.publish(f"{DISCOVER_PREFIX}/switch/{DEVICE_ID}/mute/config", "", qos=1, retain=True)

    client.publish(
        f"{DISCOVER_PREFIX}/number/{DEVICE_PREFIX}/{DEVICE_ID}_volume/config",
        json.dumps(vol_config),
        qos=1,
        retain=True,
    )
    client.publish(
        f"{DISCOVER_PREFIX}/switch/{DEVICE_PREFIX}/{DEVICE_ID}_mute/config",
        json.dumps(mute_config),
        qos=1,
        retain=True,
    )
    client.publish(AVAILABILITY_TOPIC, "online", qos=1, retain=True)


def alsa_watcher_loop(bridge: VolumeBridge):
    while True:
        try:
            mixer = alsaaudio.Mixer(control=ALSA_CONTROL, cardindex=ALSA_CARD)
            descriptors = mixer.polldescriptors()
            if not descriptors:
                time.sleep(2)
                continue
            fd, event_mask = descriptors[0]
            poller = select.poll()
            poller.register(fd, event_mask)
            while True:
                events = poller.poll(2000)
                if events:
                    mixer.handleevents()
                    bridge.on_alsa_mixer_changed()
        except Exception as e:
            logger.error(f"ALSA watcher error: {e}")
            time.sleep(2)


def main():
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bridge = VolumeBridge()

    # Connect to System D-Bus
    for _ in range(30):
        try:
            bridge.bus = dbus.SystemBus()
            break
        except Exception:
            time.sleep(1)

    if bridge.bus:
        def properties_changed_cb(interface, changed, invalidated):
            if interface in (
                "org.gnome.ShairportSync",
                "org.gnome.ShairportSync.RemoteControl",
                "org.gnome.ShairportSync.AdvancedRemoteControl",
            ):
                if "Volume" in changed:
                    bridge.on_dbus_volume_changed(float(changed["Volume"]))

        bridge.bus.add_signal_receiver(
            properties_changed_cb,
            dbus_interface="org.freedesktop.DBus.Properties",
            signal_name="PropertiesChanged",
            path="/org/gnome/ShairportSync",
        )
        if bridge.is_muted:
            bridge._set_shairport_dbus_volume(-144.0)
        else:
            bridge._set_shairport_dbus_volume(pct_to_shairport_db(bridge.current_volume_pct))

    # Connect to MQTT Broker (compatible with both paho-mqtt 1.x and 2.x)
    if hasattr(mqtt, "CallbackAPIVersion"):
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id=f"{DEVICE_ID}_bridge")
    else:
        client = mqtt.Client(client_id=f"{DEVICE_ID}_bridge")
    if MQTT_USER:
        client.username_pw_set(MQTT_USER, MQTT_PASSWORD)
    client.will_set(AVAILABILITY_TOPIC, "offline", qos=1, retain=True)

    def on_connect(c, userdata, flags, rc, *args):
        logger.info(f"Connected to MQTT broker {MQTT_HOST}:{MQTT_PORT} (rc={rc})")
        c.subscribe([(VOL_CMD_TOPIC, 1), (MUTE_CMD_TOPIC, 1)])
        publish_discovery(c)
        bridge.publish_state()

    def on_message(c, userdata, msg):
        payload = msg.payload.decode("utf-8", errors="ignore").strip()
        logger.info(f"MQTT command on {msg.topic}: {payload}")
        if msg.topic == VOL_CMD_TOPIC:
            try:
                val = float(payload)
                # Accept both 0.0..1.0 float and 1..100 integer percentages
                if VOLUME_SCALE != "0_to_100" and 0.0 < val <= 1.0:
                    pct = int(round(val * 100.0))
                else:
                    pct = int(round(val))
                bridge.apply_volume_from_mqtt(pct)
            except ValueError:
                pass
        elif msg.topic == MUTE_CMD_TOPIC:
            bridge.apply_mute_from_mqtt(payload.upper() in ("ON", "TRUE", "1"))

    client.on_connect = on_connect
    client.on_message = on_message
    bridge.mqtt_client = client

    while True:
        try:
            client.connect(MQTT_HOST, MQTT_PORT, 60)
            break
        except Exception as e:
            logger.warning(f"Waiting for MQTT broker {MQTT_HOST}:{MQTT_PORT}: {e}")
            time.sleep(2)

    client.loop_start()

    alsa_thread = threading.Thread(target=alsa_watcher_loop, args=(bridge,), daemon=True)
    alsa_thread.start()

    loop = GLib.MainLoop()
    loop.run()


if __name__ == "__main__":
    main()
