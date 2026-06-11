#!/usr/bin/env python3
"""
Headless Raspberry Pi radio player for Andon FM (https://andonlabs.com/radio).

Two GPIO buttons:
  - STATION button: cycles through the four stations
  - POWER button:   toggles playback on/off

Playback is handled by mpv as a subprocess. The script watches mpv and
restarts it if the stream drops while the radio is "on". The last station
and power state survive reboots via a small state file.

Wiring (BCM numbering, buttons wired between the GPIO pin and GND):
  GPIO17 -> station button
  GPIO27 -> power button
Internal pull-ups are enabled, no external resistors needed.

Optional SSD1306 OLED status display (128x64, I2C, 4-pin):
  VCC -> 3V3, GND -> GND, SDA -> GPIO2 (SDA), SCL -> GPIO3 (SCL)
Shows power state and the current/selected station. If the display
isn't connected or fails to initialize, the radio runs normally without it.
"""

import json
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from gpiozero import Button

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

STATIONS = [
    ("Thinking Frequencies", "https://streaming.live365.com/a46431"),
    ("OpenAIR",              "https://streaming.live365.com/a81044"),
    ("Backlink Broadcast",   "https://streaming.live365.com/a13541"),
    ("Grok and Roll",        "https://streaming.live365.com/a15419"),
]

STATION_BUTTON_PIN = 17   # BCM
POWER_BUTTON_PIN = 27     # BCM
DEBOUNCE_SECONDS = 0.05

STATE_FILE = Path.home() / ".andon-radio-state.json"

# Audio device for mpv. None lets mpv/ALSA pick the system default.
# Examples: "alsa/hw:0,0" (HDMI/jack depending on Pi config),
#           "alsa/hw:1,0" (USB DAC), "pulse" or "pipewire" if running.
AUDIO_DEVICE = None

MPV_BASE_ARGS = [
    "mpv",
    "--no-video",
    "--really-quiet",
    "--cache=yes",
    "--cache-secs=10",
    "--demuxer-readahead-secs=10",
    "--network-timeout=15",
    "--stream-lavf-o=reconnect_streamed=1,reconnect_delay_max=10",
]

WATCHDOG_INTERVAL = 3      # seconds between mpv health checks
RESTART_BACKOFF_MAX = 30   # cap for reconnect backoff

# Optional SSD1306 OLED status display (set to False to disable entirely).
ENABLE_DISPLAY = True
DISPLAY_I2C_PORT = 1
DISPLAY_I2C_ADDRESS = 0x3C
DISPLAY_REFRESH_INTERVAL = 1   # seconds between display redraws


# ---------------------------------------------------------------------------
# Player
# ---------------------------------------------------------------------------

class Radio:
    def __init__(self):
        self.lock = threading.Lock()
        self.proc = None
        self.powered = True
        self.station_idx = 0
        self.backoff = 1
        self.display = None
        self._load_state()

    # -- state persistence --------------------------------------------------

    def _load_state(self):
        try:
            data = json.loads(STATE_FILE.read_text())
            self.station_idx = int(data.get("station", 0)) % len(STATIONS)
            self.powered = bool(data.get("powered", True))
        except Exception:
            pass

    def _save_state(self):
        try:
            STATE_FILE.write_text(json.dumps(
                {"station": self.station_idx, "powered": self.powered}))
        except Exception as e:
            print(f"warn: could not save state: {e}", flush=True)

    # -- mpv lifecycle ------------------------------------------------------

    def _start_mpv(self):
        name, url = STATIONS[self.station_idx]
        args = list(MPV_BASE_ARGS)
        if AUDIO_DEVICE:
            args.append(f"--audio-device={AUDIO_DEVICE}")
        args.append(url)
        print(f"playing: {name} ({url})", flush=True)
        self.proc = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def _stop_mpv(self):
        if self.proc is not None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
            self.proc = None

    # -- button handlers ----------------------------------------------------

    def next_station(self):
        with self.lock:
            self.station_idx = (self.station_idx + 1) % len(STATIONS)
            self.backoff = 1
            self._save_state()
            if self.powered:
                self._stop_mpv()
                self._start_mpv()
            else:
                print(f"selected (off): {STATIONS[self.station_idx][0]}",
                      flush=True)

    def toggle_power(self):
        with self.lock:
            self.powered = not self.powered
            self._save_state()
            if self.powered:
                print("power: on", flush=True)
                self.backoff = 1
                self._start_mpv()
            else:
                print("power: off", flush=True)
                self._stop_mpv()

    # -- watchdog -----------------------------------------------------------

    def watchdog(self):
        """Restart mpv with backoff if it dies while powered on."""
        while True:
            time.sleep(WATCHDOG_INTERVAL)
            with self.lock:
                if not self.powered:
                    continue
                if self.proc is not None and self.proc.poll() is None:
                    self.backoff = 1
                    continue
                wait = self.backoff
                self.backoff = min(self.backoff * 2, RESTART_BACKOFF_MAX)
            print(f"stream down, retrying in {wait}s", flush=True)
            time.sleep(wait)
            with self.lock:
                if self.powered and (self.proc is None
                                     or self.proc.poll() is not None):
                    self._start_mpv()

    # -- display --------------------------------------------------------

    def display_loop(self, device):
        """Periodically redraw the OLED with power state and station name."""
        from luma.core.render import canvas
        from PIL import ImageFont

        font = ImageFont.load_default()
        last_state = None
        while True:
            with self.lock:
                state = (self.powered, STATIONS[self.station_idx][0])
            if state != last_state:
                power_text = "ON" if state[0] else "OFF"
                station_text = state[1]
                with canvas(device) as draw:
                    draw.text((0, 0), power_text, font=font, fill="white")
                    draw.text((0, 16), station_text, font=font, fill="white")
                last_state = state
            time.sleep(DISPLAY_REFRESH_INTERVAL)

    # -- shutdown -----------------------------------------------------------

    def shutdown(self, *_):
        print("shutting down", flush=True)
        with self.lock:
            self._stop_mpv()
            if self.display is not None:
                try:
                    self.display.clear()
                except Exception:
                    pass
        sys.exit(0)


def main():
    radio = Radio()

    station_btn = Button(STATION_BUTTON_PIN, pull_up=True,
                         bounce_time=DEBOUNCE_SECONDS)
    power_btn = Button(POWER_BUTTON_PIN, pull_up=True,
                       bounce_time=DEBOUNCE_SECONDS)
    station_btn.when_pressed = radio.next_station
    power_btn.when_pressed = radio.toggle_power

    signal.signal(signal.SIGTERM, radio.shutdown)
    signal.signal(signal.SIGINT, radio.shutdown)

    if radio.powered:
        with radio.lock:
            radio._start_mpv()

    threading.Thread(target=radio.watchdog, daemon=True).start()

    if ENABLE_DISPLAY:
        try:
            from luma.core.interface.serial import i2c
            from luma.oled.device import ssd1306

            serial = i2c(port=DISPLAY_I2C_PORT, address=DISPLAY_I2C_ADDRESS)
            radio.display = ssd1306(serial)
            threading.Thread(target=radio.display_loop,
                              args=(radio.display,), daemon=True).start()
        except Exception as e:
            print(f"warn: display unavailable: {e}", flush=True)

    print("andon-radio ready. station button: GPIO"
          f"{STATION_BUTTON_PIN}, power button: GPIO{POWER_BUTTON_PIN}",
          flush=True)
    signal.pause()


if __name__ == "__main__":
    main()
