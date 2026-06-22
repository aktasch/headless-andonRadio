package com.andonlabs.radio

// Placeholder: LAN discovery and control of the Raspberry Pi radio.
// The Pi exposes mpv control via a Unix socket; a small HTTP bridge
// running on the Pi will be needed to forward commands over the LAN.
object PiRemote {
    // TODO: implement mDNS/Bonjour discovery of the Pi on the local network
    // TODO: implement HTTP or WebSocket commands: next_station, prev_station, toggle_power
}
