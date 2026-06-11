# Andon FM Raspberry Pi Radio

Headless radio player that streams the four AI-run Andon FM stations
(https://andonlabs.com/radio) with two physical buttons.

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

| Station | Host model | Stream |
|---|---|---|
| Thinking Frequencies | Claude | https://streaming.live365.com/a46431 |
| OpenAIR | GPT | https://streaming.live365.com/a81044 |
| Backlink Broadcast | Gemini | https://streaming.live365.com/a13541 |
| Grok and Roll | Grok | https://streaming.live365.com/a15419 |

Note: Grok and Roll is currently paused by Andon Labs and may stream silence.
You can comment it out of the STATIONS list in radio.py if you prefer.

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
cp radio.py /home/aktasch/andon-radio/
chmod +x /home/aktasch/andon-radio/radio.py

sudo cp andon-radio.service /etc/systemd/system/
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
- Set a sane level with `alsamixer` and persist it across reboots with
  `sudo alsactl store`.
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