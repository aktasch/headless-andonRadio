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
configured. On real hardware, deploy and check logs with:

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
