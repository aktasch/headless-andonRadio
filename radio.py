#!/usr/bin/env python3
"""
Headless Raspberry Pi radio player for Andon FM (https://andonlabs.com/radio).

Controls:
  - KY-040 rotary encoder: rotate to cycle stations, push to toggle power
  - RESTART button (momentary, to GND): restarts the andon-radio systemd service

Playback is handled by mpv as a subprocess. The script watches mpv and
restarts it if the stream drops while the radio is "on". The last station
and power state survive reboots via a small state file.

The station list (name + stream URL) is fetched from stations.json in
this GitHub repo at every startup/restart, so stations can be added,
removed, or reordered by editing that file without touching radio.py.
If the fetch fails (e.g. no network yet at boot), the last successfully
fetched list is used from a local cache, falling back to a small
built-in default list as a last resort.

Wiring (BCM numbering):
  Station encoder CLK -> GPIO17
  Station encoder DT  -> GPIO18
  Station encoder SW  -> GPIO27  (push = power toggle, to GND)
  Station encoder +   -> 3.3V
  Station encoder GND -> GND
  Volume encoder CLK  -> GPIO23
  Volume encoder DT   -> GPIO24
  Volume encoder +    -> 3.3V
  Volume encoder GND  -> GND
  Restart button      -> GPIO22 to GND
Internal pull-ups are enabled on all pins; no external resistors needed.

The restart button runs `sudo systemctl restart andon-radio`. This requires
a passwordless sudo rule for the service user, e.g. in
/etc/sudoers.d/andon-radio:
  aktasch ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart andon-radio

Optional SSD1306 OLED status display (128x64, I2C, 4-pin):
  VCC -> 3V3, GND -> GND, SDA -> GPIO2 (SDA), SCL -> GPIO3 (SCL)
Shows power state, the current/selected station, and the now-playing
track (read from mpv's IPC socket). If the display isn't connected or
fails to initialize, the radio runs normally without it.

mpv is started with --input-ipc-server so external tools (e.g.
andon-radio-now) can query now-playing metadata over the same socket.
"""

import json
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

from gpiozero import Button, RotaryEncoder

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Station list is normally fetched from STATIONS_URL at startup (see
# load_stations() below). DEFAULT_STATIONS is only used if that fetch
# fails and no cached copy exists yet.
STATIONS_URL = ("https://raw.githubusercontent.com/aktasch/"
                 "headless-andonRadio/main/stations.json")
STATIONS_CACHE_FILE = Path.home() / ".andon-radio-stations.json"
STATIONS_FETCH_TIMEOUT = 10  # seconds

DEFAULT_STATIONS = [
    ("Thinking Frequencies", "https://streaming.live365.com/a46431"),
    ("OpenAIR",              "https://streaming.live365.com/a81044"),
    ("Backlink Broadcast",   "https://streaming.live365.com/a13541"),
    ("Improvisation Nation",  "https://streaming.live365.com/a35330"),
]

# Populated by load_stations() in main() before Radio() is constructed.
STATIONS = DEFAULT_STATIONS

ENCODER_CLK_PIN = 17      # BCM - station KY-040 CLK
ENCODER_DT_PIN = 18       # BCM - station KY-040 DT
ENCODER_SW_PIN = 27       # BCM - station KY-040 SW (push = power toggle)
VOLUME_CLK_PIN = 23       # BCM - volume KY-040 CLK
VOLUME_DT_PIN = 24        # BCM - volume KY-040 DT
RESTART_BUTTON_PIN = 22   # BCM - momentary button to GND
DEBOUNCE_SECONDS = 0.05
VOLUME_STEP = 5           # % per encoder step (clamped 0–100)

STATE_FILE = Path.home() / ".andon-radio-state.json"

# Audio device for mpv. None lets mpv/ALSA pick the system default.
# Examples: "alsa/hw:0,0" (HDMI/jack depending on Pi config),
#           "alsa/hw:1,0" (USB DAC), "pulse" or "pipewire" if running.
AUDIO_DEVICE = None

# ALSA mixer level (0-100) to set at every service start, since the
# saved mixer state can be restored below 100% on boot. Set to None to
# leave the mixer alone. Use `amixer -c <card> scontrols` to find the
# control name for your hardware (commonly "PCM" or "Master").
ALSA_VOLUME = 100
ALSA_CARD = 0
ALSA_CONTROL = "PCM"

MPV_SOCKET = "/tmp/andon-radio-mpv.sock"

MPV_BASE_ARGS = [
    "mpv",
    "--no-video",
    "--really-quiet",
    "--cache=yes",
    "--cache-secs=10",
    "--demuxer-readahead-secs=10",
    "--network-timeout=15",
    "--stream-lavf-o=reconnect_streamed=1,reconnect_delay_max=10",
    f"--input-ipc-server={MPV_SOCKET}",
]

WATCHDOG_INTERVAL = 3      # seconds between mpv health checks
RESTART_BACKOFF_MAX = 30   # cap for reconnect backoff

# Optional OLED status display (set to False to disable entirely).
ENABLE_DISPLAY = True
DISPLAY_I2C_PORT = 1
DISPLAY_I2C_ADDRESS = 0x3C
DISPLAY_REFRESH_INTERVAL = 0.3   # seconds between display ticks / scroll steps
NOW_PLAYING_POLL_INTERVAL = 5.0  # seconds between mpv now-playing queries
SCROLL_PAUSE_SECONDS = 5         # pause at start/end of a scrolling track title
DISPLAY_REINIT_BACKOFF = 2       # seconds to wait after a failed re-init attempt
# Controller driver: "ssd1306" or "sh1106". Many cheap 0.96" 4-pin I2C
# boards labeled SSD1306 actually use an SH1106 controller; if the screen
# stays blank with "ssd1306", try "sh1106".
DISPLAY_DRIVER = "ssd1306"
MAX_LINE_CHARS = 21   # approx characters that fit on one 128px line


# ---------------------------------------------------------------------------
# Stations
# ---------------------------------------------------------------------------

def load_stations():
    """Return the station list as (name, url) tuples.

    Fetches stations.json from STATIONS_URL. On success, caches it
    locally. On failure (e.g. no network yet at boot), falls back to the
    local cache, and finally to DEFAULT_STATIONS if no cache exists.
    """
    try:
        with urllib.request.urlopen(STATIONS_URL,
                                     timeout=STATIONS_FETCH_TIMEOUT) as resp:
            data = json.loads(resp.read().decode())
        stations = [(s["name"], s["url"]) for s in data]
        if not stations:
            raise ValueError("empty station list")
        try:
            STATIONS_CACHE_FILE.write_text(json.dumps(data))
        except OSError as e:
            print(f"warn: could not cache stations: {e}", flush=True)
        print(f"loaded {len(stations)} stations from {STATIONS_URL}",
              flush=True)
        return stations
    except Exception as e:
        print(f"warn: could not fetch stations from {STATIONS_URL}: {e}",
              flush=True)

    try:
        data = json.loads(STATIONS_CACHE_FILE.read_text())
        stations = [(s["name"], s["url"]) for s in data]
        if stations:
            print(f"using cached stations ({len(stations)})", flush=True)
            return stations
    except Exception:
        pass

    print(f"using built-in default stations ({len(DEFAULT_STATIONS)})",
          flush=True)
    return DEFAULT_STATIONS


def set_alsa_volume():
    """Set the ALSA mixer to ALSA_VOLUME (best-effort, logs on failure)."""
    if ALSA_VOLUME is None:
        return
    try:
        subprocess.run(
            ["amixer", "-c", str(ALSA_CARD), "set", ALSA_CONTROL,
             f"{ALSA_VOLUME}%"],
            check=True, capture_output=True,
        )
        print(f"set ALSA {ALSA_CONTROL} to {ALSA_VOLUME}%", flush=True)
    except (OSError, subprocess.CalledProcessError) as e:
        print(f"warn: could not set ALSA volume: {e}", flush=True)


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

    def _set_mpv_volume(self, delta):
        """Adjust mpv volume by delta % via IPC socket (best-effort)."""
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(0.5)
                s.connect(MPV_SOCKET)
                s.sendall(json.dumps(
                    {"command": ["add", "volume", delta]}).encode() + b"\n")
        except OSError:
            pass

    def volume_up(self):
        self._set_mpv_volume(VOLUME_STEP)

    def volume_down(self):
        self._set_mpv_volume(-VOLUME_STEP)

    @staticmethod
    def restart_service():
        print("restarting andon-radio service", flush=True)
        subprocess.Popen(["sudo", "systemctl", "restart", "andon-radio"])

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

    # -- now playing ----------------------------------------------------

    def _query_now_playing(self):
        """Return the current track title from mpv's IPC socket, or None."""
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(0.5)
                s.connect(MPV_SOCKET)
                title = None
                s.sendall(json.dumps(
                    {"command": ["get_property", "media-title"]}).encode() + b"\n")
                buf = b""
                while b"\n" not in buf:
                    chunk = s.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                for line in buf.decode(errors="replace").splitlines():
                    resp = json.loads(line)
                    if resp.get("error") == "success":
                        title = resp.get("data")
        except (OSError, ValueError):
            return None

        return title or None

    # -- display --------------------------------------------------------

    @staticmethod
    def _create_display_device():
        """Construct a fresh OLED device object (raises on failure)."""
        from luma.core.interface.serial import i2c
        from luma.oled.device import sh1106, ssd1306

        device_cls = sh1106 if DISPLAY_DRIVER == "sh1106" else ssd1306
        serial = i2c(port=DISPLAY_I2C_PORT, address=DISPLAY_I2C_ADDRESS)
        return device_cls(serial)

    def display_loop(self, device):
        """Periodically redraw the OLED with power state, station, artist,
        and track name.

        The now-playing title is split on the first " - " into artist
        (line 3, static, truncated) and track name (line 4, scrolling).
        The track name scrolls horizontally if longer than MAX_LINE_CHARS,
        pausing at the start and end of each pass for SCROLL_PAUSE_SECONDS.
        """
        from luma.core.render import canvas
        from PIL import ImageFont

        font = ImageFont.load_default()
        last_drawn = None
        track_title = ""
        artist_text = ""
        track_name = ""
        last_poll = 0
        scroll_pos = 0
        pause_until = 0
        while True:
            now = time.monotonic()
            with self.lock:
                powered, station_name = self.powered, STATIONS[self.station_idx][0]

            if powered and now - last_poll >= NOW_PLAYING_POLL_INTERVAL:
                new_title = self._query_now_playing() or ""
                if new_title != track_title:
                    track_title = new_title
                    if " - " in track_title:
                        artist, track_name = track_title.split(" - ", 1)
                    else:
                        artist, track_name = "", track_title
                    if len(artist) > MAX_LINE_CHARS:
                        artist_text = artist[:MAX_LINE_CHARS - 3] + "..."
                    else:
                        artist_text = artist
                    scroll_pos = 0
                    pause_until = now + SCROLL_PAUSE_SECONDS
                last_poll = now
            elif not powered:
                track_title = ""
                artist_text = ""
                track_name = ""
                last_poll = 0
                scroll_pos = 0
                pause_until = 0

            if len(track_name) <= MAX_LINE_CHARS:
                track_text = track_name
            else:
                track_text = track_name[scroll_pos:scroll_pos + MAX_LINE_CHARS]
                max_pos = len(track_name) - MAX_LINE_CHARS
                if now >= pause_until:
                    if scroll_pos >= max_pos:
                        scroll_pos = 0
                        pause_until = now + SCROLL_PAUSE_SECONDS
                    else:
                        scroll_pos += 1
                        if scroll_pos >= max_pos:
                            pause_until = now + SCROLL_PAUSE_SECONDS

            state = (powered, station_name, artist_text, track_text)
            if state != last_drawn:
                power_text = "ON" if powered else "OFF"
                try:
                    with canvas(device) as draw:
                        draw.text((0, 0), power_text, font=font, fill="white")
                        draw.text((0, 16), station_name, font=font, fill="white")
                        draw.text((0, 32), artist_text, font=font, fill="white")
                        draw.text((0, 48), track_text, font=font, fill="white")
                    last_drawn = state
                except Exception as e:
                    print(f"warn: display draw failed: {e}", flush=True)
                    try:
                        device = self._create_display_device()
                        self.display = device
                        last_drawn = None
                    except Exception as e2:
                        print(f"warn: display re-init failed: {e2}", flush=True)
                        time.sleep(DISPLAY_REINIT_BACKOFF)
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
    global STATIONS
    STATIONS = load_stations()
    set_alsa_volume()

    radio = Radio()

    encoder = RotaryEncoder(ENCODER_CLK_PIN, ENCODER_DT_PIN,
                            bounce_time=DEBOUNCE_SECONDS, wrap=True)
    encoder.when_rotated_clockwise = radio.next_station
    encoder.when_rotated_counter_clockwise = radio.next_station
    power_btn = Button(ENCODER_SW_PIN, pull_up=True,
                       bounce_time=DEBOUNCE_SECONDS)
    power_btn.when_pressed = radio.toggle_power
    vol_encoder = RotaryEncoder(VOLUME_CLK_PIN, VOLUME_DT_PIN,
                                bounce_time=DEBOUNCE_SECONDS, wrap=False)
    vol_encoder.when_rotated_clockwise = radio.volume_up
    vol_encoder.when_rotated_counter_clockwise = radio.volume_down
    restart_btn = Button(RESTART_BUTTON_PIN, pull_up=True,
                         bounce_time=DEBOUNCE_SECONDS)
    restart_btn.when_pressed = radio.restart_service

    signal.signal(signal.SIGTERM, radio.shutdown)
    signal.signal(signal.SIGINT, radio.shutdown)

    if radio.powered:
        with radio.lock:
            radio._start_mpv()

    threading.Thread(target=radio.watchdog, daemon=True).start()

    if ENABLE_DISPLAY:
        try:
            radio.display = radio._create_display_device()
            threading.Thread(target=radio.display_loop,
                              args=(radio.display,), daemon=True).start()
        except Exception as e:
            print(f"warn: display unavailable: {e}", flush=True)

    print(f"andon-radio ready. station encoder: GPIO{ENCODER_CLK_PIN}/{ENCODER_DT_PIN}, "
          f"power push: GPIO{ENCODER_SW_PIN}, "
          f"volume encoder: GPIO{VOLUME_CLK_PIN}/{VOLUME_DT_PIN}, "
          f"restart: GPIO{RESTART_BUTTON_PIN}",
          flush=True)
    signal.pause()


if __name__ == "__main__":
    main()
