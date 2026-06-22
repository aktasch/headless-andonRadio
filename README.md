# Andon FM Radio

Headless Raspberry Pi radio player + Android companion app for the AI-run
[Andon FM stations](https://andonlabs.com/radio).

## Repo layout

```text
headless-andonRadio/
├── stations.json        # shared station list — Pi and Android both use this
├── pi/                  # Raspberry Pi player (systemd service + mpv)
│   ├── radio.py
│   ├── onboarding.py    # planned: WiFi captive-portal setup
│   ├── andon-radio.service
│   └── andon-onboarding.service
├── android/             # Android Studio project (standalone player + Pi remote)
│   ├── app/src/main/java/com/andonlabs/radio/
│   └── ...
└── hardware/            # 3-D printable enclosure files
    ├── retro_radio_case.stl
    └── retro_radio_backplate.stl
```

See `pi/` for Raspberry Pi setup instructions and `android/` to open the app
in Android Studio.

---

Target hardware: Raspberry Pi 3 (works on any Pi, including Pi 5).

## OS choice (Pi 3)

Use Raspberry Pi OS Lite, 64-bit (Debian Trixie based). Lite has no
desktop, which suits the Pi 3's 1 GB of RAM, boots faster, and provides
a plain ALSA audio stack that matches this setup. 64-bit runs fine on
the Pi 3 and has the best current package support; 32-bit also works if
you ever hit an edge case.

Flash with Raspberry Pi Imager and use the OS customisation screen
before writing the card:

- set the hostname
- enable SSH
- enter your Wi-Fi credentials

The Pi then comes up on the network with no monitor or keyboard
attached, and you SSH in to run the install steps below.

Note: Lite does not preinstall gpiozero, so the apt install step below
is required, not optional.

## Stations

The live station list is [stations.json](stations.json) in this repo, a
JSON array of `{"name": ..., "url": ...}` entries:

| Station | Host model | Stream |
|---|---|---|
| Thinking Frequencies | Claude | https://streaming.live365.com/a46431 |
| OpenAIR | GPT | https://streaming.live365.com/a81044 |
| Backlink Broadcast | Gemini | https://streaming.live365.com/a13541 |
| Improvisation Nation | - | https://streaming.live365.com/a35330 |

radio.py fetches stations.json from this repo's `main` branch over HTTPS
on every startup/restart, so add, remove, or reorder stations by editing
that file and pushing to GitHub - no changes to radio.py or redeploying
are needed, just `sudo systemctl restart andon-radio` (or a reboot).

If the fetch fails (e.g. no network yet at boot), radio.py falls back to
the last successfully fetched copy, cached at
`~/.andon-radio-stations.json`, and finally to a small built-in default
list if no cache exists yet either.

Note: Andon Labs occasionally pauses stations (e.g. "Grok and Roll" may
stream silence); just remove the entry from stations.json if that
happens.

## Wiring (BCM pin numbers)

Buttons are wired between the GPIO pin and any GND pin. Internal pull-ups
are used, so no resistors are needed.

```
Station button:  GPIO17 (physical pin 11)  <-->  GND (physical pin 9)
Power button:    GPIO27 (physical pin 13)  <-->  GND (physical pin 14)
```

Change the pins at the top of radio.py if your layout differs.

## Install

```bash
sudo apt update
sudo apt install -y mpv python3-gpiozero

mkdir -p /home/aktasch/andon-radio
cp pi/radio.py /home/aktasch/andon-radio/
chmod +x /home/aktasch/andon-radio/radio.py

sudo cp pi/andon-radio.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now andon-radio
```

gpiozero works on all Pi models including the Pi 5, but is not
preinstalled on Raspberry Pi OS Lite, hence the apt line above. If your
user is not "pi", edit User= and the path in the service file.

## Audio output

mpv uses the ALSA default device. On the Pi 3, audio can default to
HDMI; if you use the 3.5mm jack, switch it explicitly:

- Run `sudo raspi-config`, then System Options > Audio, and pick the
  headphone output (or set the default in /etc/asound.conf).
- mpv's own volume (`--volume`, default 100) is just unity gain - the
  real ceiling is the ALSA hardware mixer. Check and raise it with
  `amixer`:

  ```bash
  aplay -l                  # list sound cards
  amixer -c 0 scontrols     # list mixer controls on card 0 (e.g. PCM, Master)
  amixer -c 0 get PCM       # check current level
  amixer -c 0 set PCM 100%  # raise it (use Master/Speaker if that's the control)
  ```

  Or use the interactive `alsamixer` (arrow keys to select a control and
  adjust, Esc to quit). Either way, persist the change across reboots
  with `sudo alsactl store` (Raspberry Pi OS restores it at boot via the
  `alsa-restore` service).
- radio.py also sets the mixer level itself on every service start via
  `ALSA_VOLUME`/`ALSA_CARD`/`ALSA_CONTROL` (default: card 0, `PCM`,
  100%), since the level restored by `alsactl` at boot can drift below
  100%. Set `ALSA_VOLUME = None` to disable this, or adjust `ALSA_CARD`/
  `ALSA_CONTROL` if your hardware exposes a different mixer
  (`amixer -c <card> scontrols` to list them).
- The Pi 3's onboard jack is serviceable but noisy. If the hum bothers
  you, a cheap USB DAC is the easy upgrade: list devices with
  `mpv --audio-device=help` and set AUDIO_DEVICE in radio.py, for
  example `alsa/hw:1,0`.

## Behavior

- Station button: single press cycles to the next station in order.
- Power button: toggles streaming on/off. The Pi stays on; only audio
  playback stops. Press again to resume the last station.
- Last station and power state persist across reboots
  (~/.andon-radio-state.json).
- If the stream or network drops, a watchdog reconnects with exponential
  backoff (max 30s).

## Check status / logs

```bash
systemctl status andon-radio
journalctl -u andon-radio -f
```