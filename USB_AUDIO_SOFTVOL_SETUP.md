# Raspberry Pi Setup Guide: USB DAC Amplifiers (Woopker X24, Sinilink, & Generic USB Audio)

This guide provides step-by-step instructions for configuring a fresh Raspberry Pi OS / DietPi installation to work seamlessly with USB DAC power amplifiers (such as the **Woopker X24 TPA3116**, **Sinilink XY-AP50H**, and other generic USB audio chipsets).

---

## 1. Why `softvol` is Required for Generic USB DACs

Many affordable mini stereo / subwoofer amplifiers with USB audio inputs use generic USB Audio Class (UAC) controller chips (often identified as `Intel Corp. USB2.0 Device 8087:1024`, JieLi, or AppoTech).

### The Problem:
* The chip exposes a hardware mixer named `PCM` in ALSA (range 0–255).
* When Linux sends USB UAC volume control packets to adjust this register, the hardware chip's volume implementation is **non-linear and jumpy**:
  * Volume does not scale smoothly.
  * Adjusting the slider causes transient jumps, audio dropouts, or loud steps due to an internal chip limiter/Dynamic Range Compressor (DRC).
  * In Shairport-Sync, setting volume over D-Bus or AirPlay can trigger feedback loops where the audio jumps and instantly reverts.

### The Solution:
* Pin the USB DAC's physical `PCM` register at **100% clean unity gain** and leave it untouched.
* Configure an **ALSA `softvol` (Software Volume) virtual mixer** named `Master`.
* ALSA mathematically attenuates every audio sample in RAM before sending it to the DAC.
* Volume changes become **100% linear, instant, and completely glitch-free**.

---

## 2. Fresh OS Prerequisites (DietPi / Raspberry Pi OS)

On a freshly installed DietPi or Raspberry Pi OS (Debian 12 Bookworm / Debian 13 Trixie):

```bash
# 1. Update package lists
sudo apt update

# 2. Install ALSA utilities and tools
sudo apt install -y alsa-utils docker.io docker-compose-plugin

# 3. Add your user to the audio and docker groups (if not running as root)
sudo usermod -aG audio,docker $USER
```

---

## 3. Identify the USB Sound Card

Connect your USB DAC amplifier to the Raspberry Pi and run:

```bash
cat /proc/asound/cards
```

Example output:
```text
 0 [Device         ]: USB-Audio - USB2.0 Device
                      Generic USB2.0 Device at usb-3f980000.usb-1.3, full speed
```

Note the card number (typically `0` if onboard audio is disabled, or `1` if onboard audio is enabled). In this guide, we use **Card 0**.

Verify the native hardware controls:
```bash
amixer -c 0 scontrols
```
Output:
```text
Simple mixer control 'PCM',0
```

---

## 4. Configure ALSA `softvol` in `/etc/asound.conf`

Create or replace `/etc/asound.conf`:

```bash
sudo cat << 'EOF' > /etc/asound.conf
pcm.!default {
    type plug
    slave.pcm "softvol"
}

pcm.softvol {
    type softvol
    slave.pcm "plughw:0,0"
    control {
        name "Master"
        card 0
    }
}

ctl.!default {
    type hw
    card 0
}
EOF
```

> **Note**: If your USB DAC is card 1, replace `plughw:0,0` with `plughw:1,0` and `card 0` with `card 1`.

### Initialize the Virtual `Master` Slider

ALSA only allocates the `softvol` control upon first audio stream playback through the `default` device. Trigger it with a 1-second burst:

```bash
aplay -D default /dev/zero -d 1 -f S16_LE -r 48000 -c 2
```

Verify that both `Master` and `PCM` are now present:
```bash
amixer -c 0 scontrols
```
Output:
```text
Simple mixer control 'Master',0
Simple mixer control 'PCM',0
```

---

## 5. Pin Hardware DAC Volume & Physical Potentiometer

To prevent the DAC from clipping or glitching:

```bash
# 1. Pin the DAC's hardware PCM mixer to 100% and ensure it is unmuted
amixer -c 0 sset PCM 100% unmute

# 2. Store the ALSA mixer state so it automatically persists across reboots
alsactl store
```

### Physical Volume Knob Setup:
1. The potentiometer knob on top of the Woopker / Sinilink amplifier is a pure analog attenuator in front of the TPA3116 power amp IC.
2. Set this physical knob to approximately **65%–75%** of maximum.
3. Treat this physical knob as your fixed "master ceiling" (prevents blowing speakers at 100% digital volume).
4. All day-to-day volume adjustments will now be handled digitally through `Master` via Home Assistant / ESPHome.

---

## 6. Docker Container Configuration

In your `docker-compose.yml` directory (e.g. `/opt/docker`):

### `docker-compose.yml`
Ensure `/etc/asound.conf` is mounted read-only into the container so that ALSA `softvol` is accessible inside Docker:

```yaml
services:
  mosquitto:
    image: eclipse-mosquitto:2
    container_name: mosquitto-garage
    restart: unless-stopped
    network_mode: host
    volumes:
      - ./mosquitto/config:/mosquitto/config:ro
    tmpfs:
      - /mosquitto/data
      - /mosquitto/log

  shairport-sync:
    image: ghcr.io/bennydiamond/shairport-sync-mqtt-bridge:latest
    container_name: shairport-sync-garage
    restart: unless-stopped
    network_mode: host
    privileged: true
    depends_on:
      - mosquitto
    devices:
      - /dev/snd:/dev/snd
    volumes:
      - /etc/asound.conf:/etc/asound.conf:ro
    env_file:
      - .env
    tmpfs:
      - /run
      - /var/run
      - /tmp
```

### `.env`
Configure `.env` to route through `default` (`softvol`) and bind the bridge to `Master`:

```env
# --- AirPlay & Audio Settings ---
AIRPLAY_NAME=Garage
ALSA_CARD=0
ALSA_OUTPUT_DEVICE=default
ALSA_CONTROL=Master
USE_HARDWARE_MIXER=false
ALSA_VOLUME_CURVE=mapped
IGNORE_VOLUME_CONTROL=yes

# --- MQTT & Home Assistant Discovery ---
MQTT_HOST=127.0.0.1
MQTT_PORT=1883
MQTT_USER=shairport
MQTT_PASSWORD=shairport
MQTT_DISCOVERY_PREFIX=homeassistant
MQTT_TOPIC_PREFIX=garage_audio/
MQTT_DEVICE_PREFIX=alsa_audio
SHAIRPORT_MQTT_TOPIC=garage_audio/shairport

# --- Home Assistant Entity Metadata ---
ENABLE_VOLUME_BRIDGE=true
DEVICE_ID=garage_alsa_audio
FRIENDLY_NAME=Garage ALSA Audio
DEVICE_MANUFACTURER=Woopker
DEVICE_MODEL=X24 TPA3116
VOLUME_SCALE=0_to_100

# --- Remote Syslog (Protects SD Card from log wear) ---
SYSLOG_HOST=192.168.0.15
SYSLOG_PORT=514
TZ=America/Toronto
```

Start or recreate the stack:
```bash
docker compose up -d --force-recreate
```

---

## 7. Music Assistant Streaming Settings

In the **Music Assistant** Web UI (`Settings` -> `Players` -> `Garage`):

1. **Audio Protocol / Mode**: Set to **`AirPlay 2 - compatibility mode`** (or `AirPlay 1`).
   * *Why*: Strict native AirPlay 2 requires bidirectional IEEE 1588 PTP clock packet negotiation over UDP ports 319/320. Many Wi-Fi access points or software AirPlay 2 daemons can stall on initial sync. Compatibility mode uses standard RTSP/RTP audio streaming with full lossless CD quality (16-bit / 44.1 kHz ALAC) and starts playing instantly without delays.
2. **Volume Control**: Leave managed by the player or Home Assistant.

---

## 8. Wi-Fi & Network Considerations (UniFi / Enterprise APs)

If you experience audio stream drops or AirPlay discovery latency:

1. **UAPSD (Power Save)**: Disable UAPSD on your IoT/Audio SSID in the UniFi controller (`Settings` -> `WiFi` -> Select SSID -> Uncheck `UAPSD`). Power-saving modes can sleep Wi-Fi radios and buffer multicast/PTP timing frames.
2. **Multicast Enhancement (IGMPv3)**: Enable `Multicast Enhancement` on the network so Avahi mDNS (`_airplay._tcp`, `_raop._tcp`) broadcasts reliably to all devices.
3. **Minimum Data Rate Control**: Leave at default or set to `Auto` to avoid dropping 2.4 GHz packets from clients near fringe coverage.

---

## 9. Home Assistant & ESPHome Integration

The volume bridge automatically publishes Home Assistant MQTT Discovery topics:

* **Volume Slider (0–100%)**: `number.<device_id>_volume` (e.g. `number.garage_alsa_audio_garage_alsa_audio_volume`)
* **Mute Switch (ON/OFF)**: `switch.<device_id>_mute` (e.g. `switch.garage_alsa_audio_garage_alsa_audio_mute`)

### ESPHome LVGL Touch Panel Configuration (`substitutions.yaml`)

```yaml
substitutions:
  volume_control_garage_entity_id: "number.garage_alsa_audio_garage_alsa_audio_volume"
  mute_state_garage_entity_id: "switch.garage_alsa_audio_garage_alsa_audio_mute"
  media_player_garage_entity_id: "media_player.garage_ump"
```

The touch panel slider moves `Master` smoothly in real time without audio glitches, and the mute button cleanly silences audio without pops or DAC state desynchronization.
