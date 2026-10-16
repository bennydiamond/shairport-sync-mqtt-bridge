# Shairport-Sync + ALSA / D-Bus / MQTT Volume Bridge

A single-container, multi-architecture (`linux/arm64` & `linux/amd64`) AirPlay 2 receiver and bidirectional Home Assistant volume/mute bridge built on [`mikebrady/shairport-sync`](https://github.com/mikebrady/shairport-sync).

## Why This Exists

While `shairport-sync` supports publishing track metadata and cover art over MQTT, its MQTT remote control interface only accepts step commands (`volumeup`, `volumedown`, `mutetoggle`), so the Home Assistant Shairport-Sync integration does **not** expose an absolute `0%–100%` (`0.00–1.00`) volume slider or a stateful mute switch.

Additionally, on hardware like the Raspberry Pi (`bcm2835 Headphones`), the native ALSA `PCM` mixer spans `-102.38 dB` to `+4.00 dB` without a hardware mute switch (`pswitch`), making standard linear volume sliders nearly silent below 50% and causing desynchronization between ALSA and AirPlay volume reports.

This container solves both problems in a single self-contained image (`dbus-daemon`, `avahi-daemon`, `nqptp`, `shairport-sync`, and `volume_bridge.py`):

- **True Bidirectional Sync**:
  - Changing volume or mute in **Home Assistant** (via MQTT `0.00–1.00` slider or mute switch) immediately updates the **ALSA mixer** and **Shairport-Sync's D-Bus `Volume` property** (`-30.0..0.0 dB`, `-144.0 dB` for mute).
  - Changing volume or muting from an **AirPlay sender** (iPhone, Mac, Music Assistant) updates **Shairport-Sync**, which syncs to the **ALSA mixer** and reports the updated `0.00–1.00` level and mute state to **Home Assistant**.
  - Changing volume or mute directly via **ALSA** (`alsamixer` / `amixer`) is detected via `POLLIN` descriptors and pushed to both **Shairport-Sync D-Bus** and **Home Assistant MQTT**.
- **Volume Preservation Across Mute / Unmute**:
  - Muting via Home Assistant, AirPlay, or ALSA preserves the pre-mute volume level and restores that exact level upon unmuting.
- **Automatic Hardware Adaptation (`arm64` & `x86_64`)**:
  - **PC / DAC Hardware Mixers (`native` mode, e.g. `x86_64` `HDA Intel PCH` `Master`)**: Automatically detects hardware mute capability (`pswitch`), configures Shairport-Sync with `mixer_control_name` and `use_hardware_mute_if_available = "yes"`, and uses true ALSA hardware mute (`[on]`/`[off]`).
  - **Raspberry Pi `bcm2835 Headphones` (`bcm2835` mode, `PCM`)**: Maps `1%–100%` linearly in dB across the human-ear `-30.0 dB .. 0.0 dB` dynamic range (`165..245` raw on the `0..255` scale) and uses `0` (`-102.38 dB`) for hardware mute.
- **SD Card Protection**:
  - Runs ephemeral state in `tmpfs` (`/run`, `/var/run`, `/tmp`) and supports direct UDP syslog forwarding (`SYSLOG_HOST`) to a remote rsyslog/Loki/Alloy collector.

---

## Multi-Arch Docker Image & Versioning

This repository tracks upstream tagged releases of [`mikebrady/shairport-sync`](https://hub.docker.com/r/mikebrady/shairport-sync/tags) alongside a local `BUILD_INDEX` in [`version.env`](version.env):

```ini
SHAIRPORT_VERSION=5.6
BUILD_INDEX=1
```

- **Automatic Upstream Tracking**: The GitHub Actions workflow checks daily (`cron: '0 6 * * *'`) for new upstream `mikebrady/shairport-sync` releases on GitHub and Docker Hub. When a new release (e.g., `5.7`) appears, it automatically updates `version.env` and `Dockerfile`, resets `BUILD_INDEX=1`, and publishes `5.7-1`.
- **Local Iteration (`BUILD_INDEX`)**: Whenever you push changes to this repo (or manually bump `BUILD_INDEX` in `version.env`), the workflow builds against the pinned `SHAIRPORT_VERSION` and auto-increments `BUILD_INDEX` (`5.6-1` $\rightarrow$ `5.6-2`), creating a matching Git tag and GitHub Release (`v5.6-2`).

```bash
# Pull latest
docker pull ghcr.io/bennydiamond/shairport-sync-mqtt-bridge:latest

# Or pin to a specific Shairport-Sync version / build index
docker pull ghcr.io/bennydiamond/shairport-sync-mqtt-bridge:5.6-1
docker pull ghcr.io/bennydiamond/shairport-sync-mqtt-bridge:5.6
```

---

## Configuration (Environment Variables)

Copy `.env.example` to `.env` and customize for your host:

| Variable | Default | Description |
| :--- | :--- | :--- |
| `AIRPLAY_NAME` | `ShairportSync` | Name advertised to AirPlay clients (e.g. `Garage`, `Atelier`, `Patio`) |
| `ALSA_CARD` | `0` | ALSA soundcard index (`0`, `1`, etc.) |
| `ALSA_OUTPUT_DEVICE` | `hw:0` | Shairport-Sync ALSA `output_device` (e.g. `hw:0` or `plughw:PCH`) |
| `ALSA_CONTROL` | `PCM` | ALSA mixer control name (`PCM` on Raspberry Pi, `Master` on x86_64 Intel HDA) |
| `USE_HARDWARE_MIXER` | `auto` | `auto` (enables if `pswitch` detected), `true`, or `false` |
| `ALSA_VOLUME_CURVE` | `auto` | `auto`, `bcm2835` (Pi 30 dB curve), or `native` (0–100% + `pswitch`) |
| `ENABLE_VOLUME_BRIDGE` | `true` | Run `volume_bridge.py` to expose Home Assistant MQTT slider & mute switch |
| `VOLUME_SCALE` | `0_to_1` | `0_to_1` (publishes `0.00`–`1.00` for HA `volume_level`) or `0_to_100` |
| `MQTT_HOST` | `127.0.0.1` | MQTT broker hostname/IP |
| `MQTT_PORT` | `1883` | MQTT broker port |
| `MQTT_USER` | `shairport` | MQTT username |
| `MQTT_PASSWORD` | `shairport` | MQTT password |
| `MQTT_DISCOVERY_PREFIX` | `homeassistant` | Home Assistant MQTT discovery prefix |
| `MQTT_TOPIC_PREFIX` | `garage_audio/` | Base topic prefix for this room/unit |
| `SHAIRPORT_MQTT_TOPIC` | `<prefix>/shairport` | Topic where Shairport-Sync publishes metadata & cover art |
| `DEVICE_ID` | `garage_alsa_audio` | Unique ID for Home Assistant MQTT discovery (`number.<id>_volume`, `switch.<id>_mute`) |
| `FRIENDLY_NAME` | `Garage ALSA Audio` | Friendly name in Home Assistant |
| `SYSLOG_HOST` | *(empty)* | Optional remote syslog IP (e.g. `192.168.0.15`) |
| `SYSLOG_PORT` | `514` | Remote syslog UDP port |

---

## Proxmox LXC Audio Passthrough (`x86_64`)

When running inside a Proxmox LXC container (e.g. `atelier-audio` with Intel PCH HDA audio):

1. Ensure `snd_hda_intel` is **not** blacklisted in `/etc/modprobe.d/pve-blacklist.conf` on the Proxmox VE host, and load the module (`modprobe snd_hda_intel`).
2. Add the ALSA device cgroup and bind mount to `/etc/pve/lxc/<CTID>.conf` on the Proxmox host:
   ```ini
   lxc.cgroup2.devices.allow: c 116:* rwm
   lxc.mount.entry: /dev/snd dev/snd none bind,optional,create=dir 0 0
   ```

---

## Home Assistant Universal Media Player (UMP) Example

With `VOLUME_SCALE=0_to_100` (default), the MQTT `number` entity operates on the standard `0..100` scale (ideal for direct integration with ESPHome LVGL touch displays and hardware sliders). In Home Assistant's Universal Media Player, the `volume_level` attribute template normalizes to `0.00..1.00`, and `volume_set` scales back to `0..100`:

```yaml
- platform: universal
  name: Atelier UMP
  unique_id: atelier_player_homepod_mass_combined
  children:
    - media_player.mass_atelier
    - media_player.shairport_sync_atelier
  active_child_template: >
    {% if is_state('binary_sensor.atelier_audio_from_mass', 'on') %}
      media_player.mass_atelier
    {% else %}
      media_player.shairport_sync_atelier
    {% endif %}
  commands:
    volume_up:
      action: number.set_value
      target:
        entity_id: number.atelier_alsa_audio_atelier_alsa_audio_volume
      data:
        value: "{{ [100, (states('number.atelier_alsa_audio_atelier_alsa_audio_volume') | int(0) + 7)] | min }}"
    volume_down:
      action: number.set_value
      target:
        entity_id: number.atelier_alsa_audio_atelier_alsa_audio_volume
      data:
        value: "{{ [0, (states('number.atelier_alsa_audio_atelier_alsa_audio_volume') | int(0) - 7)] | max }}"
    volume_mute:
      action: switch.toggle
      target:
        entity_id: switch.atelier_alsa_audio_atelier_alsa_audio_mute
    volume_set:
      action: number.set_value
      target:
        entity_id: number.atelier_alsa_audio_atelier_alsa_audio_volume
      data:
        value: "{{ (volume_level * 100) | round | int }}"
  attributes:
    is_volume_muted: switch.atelier_alsa_audio_atelier_alsa_audio_mute
    volume_level: "{{ states('number.atelier_alsa_audio_atelier_alsa_audio_volume') | float(0) / 100.0 }}"
```
