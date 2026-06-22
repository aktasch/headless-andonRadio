# WiFi Onboarding for AndonRadio

## Context

This is a product (headless Pi internet radio). New devices ship with no WiFi configured. The user has no keyboard or monitor — only a rotary encoder, OLED display, and buttons. We need a first-boot onboarding flow that lets them set up WiFi from their phone.

## Approach

**WiFi AP + Captive Portal** (industry standard for headless IoT):
- On first boot, Pi creates a temporary WiFi hotspot ("AndonRadio-XXXX")
- User connects phone to that hotspot
- Phone's captive portal detection auto-opens a browser page (or user goes to 192.168.4.1)
- User selects their WiFi network and enters password
- Pi connects to WiFi, tears down hotspot, radio starts

**Architecture: separate `onboarding.py` + `andon-onboarding.service`**
- Runs as root (required for hostapd, dnsmasq, ip commands)
- Ordered `Before=andon-radio.service` in systemd — radio waits until onboarding exits
- Self-gates: if WiFi already connected, exits 0 immediately (fast path on every subsequent boot)
- `radio.py` needs no changes to startup logic

## New Files

### `onboarding.py`

**Constants:**
```python
HOTSPOT_IP = "192.168.4.1"
HOTSPOT_SSID_PREFIX = "AndonRadio"   # + last 4 of MAC → "AndonRadio-A3F2"
PORTAL_PORT = 80
WIFI_IFACE = "wlan0"
HOSTAPD_CONF = Path("/tmp/andon-hostapd.conf")
DNSMASQ_CONF = Path("/tmp/andon-dnsmasq.conf")
WIFI_CONFIGURED_FLAG = Path("/etc/andon-radio/.wifi-configured")
```

**Key functions:**
- `is_wifi_connected()` — checks `ip -4 addr show wlan0` for `"inet "`, then pings 8.8.8.8
- `hotspot_ssid()` — reads `/sys/class/net/wlan0/address`, returns `AndonRadio-{last4MAC}`
- `write_hostapd_conf(ssid)` — open AP, channel 6, no password
- `write_dnsmasq_conf()` — DHCP range 192.168.4.2–20, catch-all DNS (`address=/#/192.168.4.1`) to trigger captive portal on phones
- `start_hotspot(ssid)` — stops wpa_supplicant, assigns IP to wlan0, launches hostapd + dnsmasq
- `stop_hotspot()` — kills hostapd/dnsmasq, flushes wlan0, restarts wpa_supplicant
- `save_wifi_credentials(ssid, password)` — tries `nmcli device wifi connect <ssid> password <pw>`, falls back to writing `/etc/wpa_supplicant/wpa_supplicant.conf`
- `show_oled(line0, line1, line2, line3)` — writes 4 lines to OLED using same luma.oled driver as radio.py; silently skips if OLED unavailable
- `run_portal() → dict` — starts `HTTPServer` on 192.168.4.1:80 in a thread; blocks until user submits form; returns `{"ssid": ..., "password": ...}`
- `PortalHandler(BaseHTTPRequestHandler)` — `do_GET` serves HTML form with WiFi SSID input, `do_POST` captures credentials and signals done

**OLED status progression:**
| State | Line 0 | Line 1 | Line 2 | Line 3 |
|-------|--------|--------|--------|--------|
| AP up | `"Setup mode"` | `"AndonRadio-A3F2"` | `"192.168.4.1"` | `""` |
| Connecting | `"Connecting..."` | `"<SSID>"` | `""` | `""` |
| Connected | `"Connected!"` | `"Starting..."` | `""` | `""` |
| Failed | `"WiFi failed"` | `"Restart radio"` | `""` | `""` |

**`main()` flow:**
1. Must run as root — exit 1 if not
2. `is_wifi_connected()` → exit 0 immediately if yes (fast path)
3. `show_oled("Setup mode", ssid, "192.168.4.1", "")`
4. `start_hotspot(ssid)`
5. `creds = run_portal()` (blocks until user submits)
6. `show_oled("Connecting...", creds["ssid"], "", "")`
7. `stop_hotspot()`
8. `save_wifi_credentials(creds["ssid"], creds["password"])`
9. Poll `is_wifi_connected()` every 2s for up to 60s
10. On success: `show_oled("Connected!", "Starting...", "", "")`, touch `WIFI_CONFIGURED_FLAG`, exit 0
11. On timeout: `show_oled("WiFi failed", "Restart radio", "", "")`, exit 1 (systemd Restart will retry)

### `andon-onboarding.service`

```ini
[Unit]
Description=Andon Radio WiFi onboarding
Before=andon-radio.service
Wants=andon-radio.service
DefaultDependencies=no
After=local-fs.target

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /home/aktasch/andon-radio/onboarding.py
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

## Modified Files

### `andon-radio.service`

Change `[Unit]` ordering — remove `network-online.target` (onboarding ensures network is up before radio starts, avoiding a deadlock where radio waits for network but network needs onboarding):

```ini
[Unit]
After=andon-onboarding.service sound.target
Wants=andon-onboarding.service
```

### `radio.py`

Add `enter_setup_mode()` to `Radio` class — triggered by **3-second hold** on the restart button (GPIO22), allowing re-onboarding on an already-configured device:

```python
def enter_setup_mode(self):
    print("entering WiFi setup mode", flush=True)
    with self.lock:
        self._stop_mpv()
    subprocess.Popen(["sudo", "systemctl", "start", "andon-onboarding"])
    time.sleep(0.5)
    subprocess.Popen(["sudo", "systemctl", "stop", "andon-radio"])
```

Change restart button wiring in `main()`:
```python
restart_btn = Button(RESTART_BUTTON_PIN, pull_up=True,
                     bounce_time=DEBOUNCE_SECONDS,
                     hold_time=3.0, hold_repeat=False)
restart_btn.when_pressed = radio.restart_service   # short press → restart
restart_btn.when_held = radio.enter_setup_mode     # 3s hold → WiFi setup
```

## OS Setup (one-time, documented in README)

```bash
sudo apt install hostapd dnsmasq
sudo systemctl disable hostapd dnsmasq
sudo systemctl mask hostapd dnsmasq   # prevent auto-start; onboarding.py manages them

# Fix dnsmasq port 53 conflict with systemd-resolved
echo "[Resolve]
DNSStubListener=no" | sudo tee -a /etc/systemd/resolved.conf
sudo systemctl restart systemd-resolved

# Sudoers additions
echo "aktasch ALL=(ALL) NOPASSWD: /usr/bin/systemctl start andon-onboarding
aktasch ALL=(ALL) NOPASSWD: /usr/bin/systemctl stop andon-radio" \
  | sudo tee -a /etc/sudoers.d/andon-radio
```

## Verification

1. Delete `~/.andon-radio-state.json` and disconnect WiFi (`nmcli radio wifi off`)
2. Reboot — `andon-onboarding.service` should start, OLED shows "Setup mode / AndonRadio-XXXX"
3. Connect phone to "AndonRadio-XXXX" hotspot — captive portal dialog should auto-appear
4. Enter real WiFi credentials → submit
5. OLED shows "Connecting..." then "Connected! / Starting..."
6. Radio starts playing within ~10s
7. Reboot again — onboarding exits 0 immediately (WiFi already connected), radio starts normally
8. Hold GPIO22 for 3s → OLED blanks, onboarding restarts → confirm setup mode appears again
