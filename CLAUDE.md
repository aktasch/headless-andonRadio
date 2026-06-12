# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

A headless Raspberry Pi radio player for Andon FM (https://andonlabs.com/radio), an
internet radio service streaming four AI-themed stations. The entire application is
a single Python script (`radio.py`) plus a systemd unit file (`andon-radio.service`).
There is no build system, package manager, or test suite — this is a small embedded
control script meant to run as a systemd service on a Raspberry Pi.

## Architecture

`radio.py` has three cooperating pieces, all coordinated through a single `Radio`
object guarded by `self.lock`:

- **Playback**: mpv runs as a subprocess (`_start_mpv`/`_stop_mpv`) with `--no-video`
  and stream-reconnect flags. Station list and stream URLs live in the `STATIONS`
  constant at the top of the file.
- **Input**: two gpiozero `Button`s (BCM pins, configured via `STATION_BUTTON_PIN`
  and `POWER_BUTTON_PIN`) drive `next_station()` (cycle stations) and `toggle_power()`
  (start/stop playback without powering off the Pi).
- **Watchdog thread**: `Radio.watchdog()` runs forever in a daemon thread, checking
  every `WATCHDOG_INTERVAL` seconds whether mpv died while `powered` is true, and
  restarting it with exponential backoff (capped at `RESTART_BACKOFF_MAX`).
- **Display thread** (optional): `Radio.display_loop()` runs in a daemon thread and
  redraws a 128x64 SSD1306 I2C OLED (via `luma.oled`) across four lines: power
  state, station name, artist, and track name. Now-playing info comes from
  `_query_now_playing()`, which queries mpv's IPC socket (`MPV_SOCKET`) every
  `NOW_PLAYING_POLL_INTERVAL` seconds and is split on the first `" - "` into
  artist (line 3, static, truncated if over `MAX_LINE_CHARS`) and track name
  (line 4). Track names longer than `MAX_LINE_CHARS` scroll horizontally (one
  character per `DISPLAY_REFRESH_INTERVAL` tick), pausing for
  `SCROLL_PAUSE_SECONDS` at the start and end of each pass. Controlled by
  `ENABLE_DISPLAY`; if the OLED isn't connected or fails to initialize, a
  warning is logged and the radio runs normally without it. If a draw call
  fails (e.g. a transient I2C error leaves the bus handle unusable), the
  device is re-initialized via `_create_display_device()`; if re-init also
  fails, it retries after `DISPLAY_REINIT_BACKOFF` seconds.

State (current station index + power on/off) persists to
`~/.andon-radio-state.json` via `_save_state`/`_load_state` so it survives reboots.

Signal handlers (`SIGTERM`/`SIGINT`) call `radio.shutdown()` to cleanly terminate
the mpv subprocess before exit — important since this runs under systemd with
`Restart=always`.

## Running / testing changes

There is no automated test suite. To validate changes:

```bash
python3 radio.py
```

This requires `mpv` and `python3-gpiozero` installed, and will fail outside of a
Pi (or any Linux box) without GPIO hardware unless gpiozero's mock pin factory is
configured. The optional OLED display requires `luma.oled`, `luma.core`, and
`pillow`. On Debian Bookworm's externally-managed Python, install with
`pip install --user --break-system-packages luma.oled luma.core pillow`, plus
an I2C bus enabled via `raspi-config`; if unavailable, set
`ENABLE_DISPLAY = False` or just ignore the startup warning.
On real hardware, deploy and check logs with:

```bash
sudo systemctl restart andon-radio
journalctl -u andon-radio -f
```

## Key configuration constants (top of radio.py)

- `STATIONS` — list of (name, stream URL) tuples; order determines cycle order.
- `STATION_BUTTON_PIN` / `POWER_BUTTON_PIN` — BCM GPIO pin numbers.
- `AUDIO_DEVICE` — mpv ALSA device override (e.g. `"alsa/hw:1,0"` for a USB DAC);
  `None` uses the system default.
- `MPV_BASE_ARGS` — base mpv invocation; reconnect/caching behavior lives here.
- `MPV_SOCKET` — path to mpv's `--input-ipc-server` socket, used by
  `_query_now_playing()` and the standalone `andon-radio-now` script.
- `ENABLE_DISPLAY`, `DISPLAY_I2C_PORT`, `DISPLAY_I2C_ADDRESS`,
  `DISPLAY_REFRESH_INTERVAL`, `NOW_PLAYING_POLL_INTERVAL`, `SCROLL_PAUSE_SECONDS`,
  `DISPLAY_REINIT_BACKOFF`, `DISPLAY_DRIVER`, `MAX_LINE_CHARS` — OLED status
  display settings. `DISPLAY_DRIVER` is `"ssd1306"` or `"sh1106"` — many cheap
  0.96" 4-pin I2C boards sold as SSD1306 are actually SH1106 controllers and
  stay blank with the wrong driver.
