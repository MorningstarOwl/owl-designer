<h1 align="center">Owl Designer</h1>

<p align="center">
  <b>A Q-SYS-style audio designer for your whole house</b> — every speaker, microphone, app and camera on every machine,
  on one canvas, wired by drag and drop. Built on PipeWire, AES67 and AMWA NMOS, made for Home Assistant.
</p>

<p align="center">
  <a href="https://github.com/MorningstarOwl/owl-designer/releases"><img alt="release" src="https://img.shields.io/github/v/release/MorningstarOwl/owl-designer?sort=semver"></a>
  <a href="LICENSE"><img alt="license: MIT" src="https://img.shields.io/badge/license-MIT-blue"></a>
  <a href="#home-assistant-add-on"><img alt="Home Assistant add-on" src="https://img.shields.io/badge/Home%20Assistant-add--on-41BDF5?logo=homeassistant&logoColor=white"></a>
  <img alt="amd64 | aarch64" src="https://img.shields.io/badge/arch-amd64%20%7C%20aarch64-lightgrey">
</p>

![The Owl Designer canvas in Run mode: devices of three machines, processing blocks, live meters](docs/images/designer-run.png)

Owl Designer turns the Linux machines in your house into one audio system. Each machine runs **owl-node**: its sound
cards, USB microphones and app streams appear in the designer as blocks with ports, next to processing blocks — EQ,
compressor, ducker, limiter, an **echo canceller**, a **Home Assistant voice satellite**, **RTSP cameras**. Draw wires,
press **Deploy**, and the hub turns them into PipeWire links on each machine and AES67 network streams between machines.

## Contents

[Features](#features) · [How it fits together](#how-it-fits-together) · [Requirements](#requirements) ·
[Install](#install) · [Home Assistant](#connect-home-assistant) · [Your first design](#your-first-design) ·
[Voice assistant](#echo-cancellation-and-the-voice-assistant) · [Cameras](#rtsp-cameras) · [Blocks](#blocks) ·
[Updating](#updating) · [Ports](#network-ports) · [Troubleshooting](#troubleshooting) · [Status](#status-and-roadmap)

## Features

- **One canvas for every machine.** Add a machine by typing its IP; its inputs and outputs appear. Wires between
  machines become AES67 streams (every machine on one PTP clock) and NMOS patches — you never pick a transport.
- **Processing blocks**, live where it makes sense: parametric and DJ EQ, compressor, limiter, ducker, gain, delay,
  mixer, router, test tone, meters. They run on the hub or on any node. A block type is one YAML file.
- **Echo cancellation for voice.** The WebRTC AEC3 block removes what the speakers and the TV play from the microphone,
  so the wake word works with the music on. Feed it the TV through a cheap optical-to-USB receiver.
- **Home Assistant voice satellite** as a block: wire the clean mic into it; the wake word is chosen in HA; replies duck
  the music.
- **Home Assistant controls.** Every named control and meter becomes an HA entity over MQTT; Panels give you
  phone-friendly pages of faders, buttons and meters.
- **RTSP cameras.** Paste a camera link: its audio becomes a source you can wire anywhere, its video can be previewed
  and sent to any node's video receiver (hardware HEVC decoding on a Raspberry Pi 5).
- **🎧 Listening probe.** Drag the probe onto any port, block or wire and hear that point on the computer you are using.
- **Safe deploys.** Every change is shown before it is applied, replacing a whole design is called out, and a failed
  deploy rolls back by itself.
- **Video (experimental):** H.264/HEVC over SRT and JPEG XS over RTP between nodes, with browser previews.

<table>
  <tr>
    <td width="50%"><img src="docs/images/signal-chain.png" alt="A living-room signal chain: music through a ducker, volume and limiter to the speaker; mic and TV into the AEC; clean mic into the voice assistant"><br><sub><b>A living room:</b> music → ducker → volume → limiter → speaker; the room mic and the TV into the echo canceller; its clean mic into the voice assistant.</sub></td>
    <td width="50%"><img src="docs/images/inspector-voice.png" alt="The inspector of the voice assistant block"><br><sub><b>The inspector</b> explains each block and, for the voice assistant, what it hears, where it replies and its link to Home Assistant.</sub></td>
  </tr>
  <tr>
    <td><img src="docs/images/probe.png" alt="The listening probe playing the echo canceller's clean mic"><br><sub><b>🎧 Probe:</b> hear any point of the graph in your browser — here the echo canceller's clean output.</sub></td>
    <td><img src="docs/images/routing.png" alt="The NMOS routing grid with PTP status per node"><br><sub><b>Routing:</b> every NMOS sender and receiver, with PTP lock, packet rates and loss.</sub></td>
  </tr>
</table>

## How it fits together

```mermaid
flowchart LR
  subgraph HA["Home Assistant"]
    addon["Owl Designer add-on<br/>(the designer in the sidebar)"]
    mqtt["MQTT broker<br/>(entities for every control)"]
    wyoming["Wyoming integration<br/>(voice satellite)"]
  end
  subgraph HUB["Hub — owl-node install.sh --hub"]
    designer["Designer :8361"]
    engine["DSP engine :8362<br/>PipeWire graph, blocks"]
    registry["NMOS registry :8010"]
    mtx["MediaMTX<br/>(previews, camera relay)"]
  end
  subgraph NODES["Nodes — owl-node install.sh"]
    n1["Raspberry Pi 5<br/>speakers, mics, video out"]
    n2["PC / laptop<br/>sound card, HDMI"]
  end
  cam["RTSP cameras"]
  addon -- "remote engine" --> engine
  engine -- "entities" --> mqtt
  wyoming -- "TCP 10700" --> engine
  engine -- "AES67 audio + PTP" --- n1
  engine -- "AES67 audio + PTP" --- n2
  n1 & n2 -- "IS-04 registration" --> registry
  cam -- "RTSP" --> mtx
```

| | What it runs | Where |
|---|---|---|
| **Hub** | the designer (`http://<hub>:8361/`), the DSP engine, the NMOS registry, the media server | one Linux machine with sound hardware and some CPU |
| **Nodes** | AES67 audio, the machine's own devices in the designer, optional processing and video | every other Linux machine (a Pi 5 works) |
| **Home Assistant add-on** | the designer in HA's sidebar, behind HA's login; the engine stays on the hub | HA OS / Supervised, amd64 or aarch64 |

## Requirements

- **Linux with PipeWire** on every machine (a desktop install has it; in use on Ubuntu 26.04 and on Raspberry Pi OS /
  Debian 12) and **Docker Engine with the compose plugin**.
- **A wired network.** AES67 and PTP do not work over Wi-Fi. A network card with a **PTP hardware clock** (most Intel
  i210/i225/i226 and the Raspberry Pi 5's built-in port) keeps every machine on the same clock; without one a machine
  still shows its devices but its network audio drifts. Receivers play out adaptively by default: clean, but not
  sample-aligned between rooms (one direction slips a 1 ms packet every ~11.5 s). Timestamp-locked playout
  (`ts_direct: true` in `node.yaml`) is experimental: it is not yet reliable across restarts.
- For the voice assistant: Home Assistant with an Assist pipeline, the **Wyoming** integration and a wake word engine
  (openWakeWord); for HA entities: an **MQTT broker** (the Mosquitto add-on).

## Install

### 1. The hub

```bash
git clone https://github.com/MorningstarOwl/owl-designer.git && cd owl-designer
owl-node/install.sh --hub
```

Run it as your desktop user (it asks for sudo itself). It finds the wired interface, the PTP clock and its role, builds
the image (a few minutes on a PC), asks whether to start at boot and ends with the designer's address. Add
`--dry-run` first to see every file it would write.

### 2. Every other machine

```bash
git clone https://github.com/MorningstarOwl/owl-designer.git && cd owl-designer
owl-node/install.sh
```

Same detection, plus one AES67 sender and one receiver (building takes 20–40 minutes on a Pi). Then, in the hub's
designer, type the machine's IP in **Nodes** and press **Add**: the hub pairs it and its inputs and outputs appear.

Each different source that leaves a machine needs a sender of its own there: music to the kitchen and the news to the
bedroom, both from the hub, use two hub senders (re-run the hub's `install.sh` with its full list of streams, e.g.
`--tx "Living Room" --tx "Living Room 2" --rx "Living Room RX 1"`: any `--tx`/`--rx`/`--vtx`/`--vrx` replaces all of
them; likewise two `--rx` on a machine that receives two streams). With too few, Deploy warns and the sources share
a sender, which mixes them. A node with a PTP hardware clock also stands by as the network's clock: if the hub is off,
one node takes over so the others stay in step (`--ptp backup`, the default; `--ptp follower` opts out).

<p><img src="docs/images/nodes.png" alt="The Nodes box: two nodes added by IP, answering and paired" width="300"></p>

### 3. Home Assistant add-on <a id="home-assistant-add-on"></a>

1. **Settings → Add-ons → Add-on store → ⋮ → Repositories**, add `https://github.com/MorningstarOwl/owl-designer`.
2. Install **Owl Designer** (a prebuilt image; nothing is compiled on your HA host).
3. **Configuration:** set `engine_url` to your hub, e.g. `192.0.2.10` (port 8362 is assumed). Start it and turn on
   **Show in sidebar**.

The audio processing stays on the hub, so a small HA host is fine (the add-on uses about 15 MB of RAM). The add-on's
port 8361 is off by default; set it in the add-on's Network section to reach the designer without HA's login.

## Connect Home Assistant

**Controls and meters (MQTT).** Give the hub your broker once, at install time
(`owl-node/install.sh --hub --mqtt mqtt://user:password@<broker>:1883`) or in `/var/lib/owl-node/mqtt.json`
(`{"host": "...", "port": 1883, "username": "...", "password": "..."}`). A device per hub appears in HA with an entity
for every named control (volume, mute, ducking depth, presets…) and meter; deploying a new design updates them.

**Voice satellite (Wyoming).** The hub advertises itself on the network; HA offers it under **Settings → Devices &
services → Discovered** (or add **Wyoming Protocol** with host `<hub>` and port `10700`). Pick the Assist pipeline and
its wake word on the new device, then turn on the wake-word switch (on the Panels page or the HA entity).

## Your first design

1. **Add your machines** in **Nodes** (above). Each machine's sound cards, USB devices and app streams appear as
   blocks; NMOS senders and receivers appear per machine.
2. **Add processing** from the **Blocks** palette, **wire** outputs (right side of a block) to inputs (left side).
   Wires between machines need nothing else.
3. **Deploy…** shows exactly what will change — processes started, links made, patches set — and warns when a deploy
   would replace the whole design or cut off the voice assistant. Apply; a failure rolls back.
4. **Run** shows live meters on every port and the state of every wire. Drag **🎧 Probe** onto anything to hear it.
5. **Panels** turn named controls into pages for your phone: drag controls onto a page, set ranges and labels.

<p><img src="docs/images/panels-phone.png" alt="A Panels page on a phone: volume, mute, preset, ducking, meters, push-to-talk" width="300"></p>

## Echo cancellation and the voice assistant

The **AEC** block has two inputs and one output that matters:

| Port | Wire into it | |
|---|---|---|
| **mic (voice to keep)** | the room microphone (a USB mic, a mic array) | what people say |
| **reference (sound to remove)** | what the speakers play (after your limiter) and the TV (an optical/HDMI audio capture) | several wires mix |
| **clean mic** → | the **Voice assistant** block's *listen*, a meter, a recorder | the mic minus the reference |

Wire the **Voice assistant** block's *reply* into the ducker's *sidechain* so answers lower the music. Set the AEC's
*Mic channels* to match the mic (Deploy warns when they differ). The microphone's placement matters more than any
setting: close to where people talk, away from the speakers.

## RTSP cameras

Add an **RTSP source** block and paste the camera's link (`rtsp://user:password@camera:554/stream` or `rtsps://…`;
UniFi Protect: enable RTSP on the camera and copy its `rtsps://…:7441/…` or `rtsp://…:7447/…` link).

- **audio** is a source like any other: meter it, process it, play it in another room, listen with the probe.
- **video** is relayed by the hub (pulled from the camera once) and can be wired to any node's **video receiver**;
  **▶ Preview** in the inspector plays it in the browser (browsers generally need H.264 for that; H.265 cameras still
  play on receivers).
- The link is treated as a secret: masked in the designer, never logged, never an HA entity. Self-signed `rtsps`
  certificates are pinned on Deploy.

## Blocks

| Group | Block | |
|---|---|---|
| Voice | **AEC** | WebRTC AEC3 echo canceller with noise suppression, gain control and high-pass |
| Voice | **Voice assistant** | Home Assistant Wyoming satellite: *listen* and *reply* are its mic and speaker |
| Sources | **RTSP source** | a camera or any RTSP stream: audio output, video output |
| Sources | **App Input** | a named sink an app plays into (`squeezelite -o NAME`); its monitor is the output |
| Dynamics | **Compressor** · **Limiter** · **Ducker** · **Gain** | all parameters live |
| EQ | **Parametric EQ** · **DJ EQ** | shelves + 3 bands per channel · 3-band (a template for any LADSPA plugin) |
| Routing | **Delay** · **Mixer 4x2** · **Router 4:1** | lip-sync and speaker alignment · four inputs to one · select one of four |
| Metering | **Meter** | RMS and peak, shown in Run mode, Panels and HA |
| Test | **Test Tone** | sine generator |
| Devices | **XVF3800 mic array** | ReSpeaker USB array, appears when plugged in |

Each block type is a YAML file in [`owl-node/dsp/blocks/`](owl-node/dsp/blocks/): a PipeWire filter-chain or module
configuration with its ports, parameters and live controls. Adding a LADSPA plugin is about 25 lines.

## Updating

- **Hub and nodes:** `git pull`, then run the same `install.sh` again. A re-run keeps the machine's name, roles,
  senders, receivers, registry and start-at-boot choice; `--dry-run` shows the difference first.
- **Home Assistant add-on:** HA offers updates by itself when a new version is released.
- Versions are plain `X.Y.Z` everywhere (the add-on, the image, the release tag `vX.Y.Z`).

## Network ports

| Port | | Port | |
|---|---|---|---|
| 8361/tcp | designer (hub) | 8362/tcp | DSP engine API (hub and nodes) |
| 8010–8011/tcp | NMOS registry (hub) | 8360/tcp | NMOS node API (every machine) |
| 10700/tcp | voice satellite (hub) | 5004/udp, 239.69.x.x | AES67 audio (multicast) |
| 8889/tcp, 8189/udp | browser previews (WebRTC) | 8890/udp | SRT (video, camera relay) |
| PTP | layer 2 (Ethernet), not IP | 1883/tcp | MQTT (to your broker) |

## Troubleshooting

<details><summary><b>A machine does not appear when I add its IP</b></summary>

The designer says what it found: *nothing listens on :8362* means owl-node is not running there (or was installed
without the dsp role: re-run `owl-node/install.sh`); *no answer* means a wrong address or a firewall; *another hub*
means you typed a hub's address.
</details>

<details><summary><b>Wires between machines are silent</b></summary>

Check **Routing**: each node should show **PTP locked**. A node on Wi-Fi or without a PTP hardware clock shows *system
clock*; its audio drifts. Multicast must be allowed on your switch (most home switches pass it; some mesh Wi-Fi
systems flood or drop it).
</details>

<details><summary><b>The wake word stops working</b></summary>

Open the **Voice assistant** block's inspector: it shows the link to Home Assistant, the wake word and what the
assistant hears. If Home Assistant shows the satellite but the link stays *disconnected*, reload the satellite's
Wyoming entry in HA (**Settings → Devices & services → Wyoming → ⋮ → Reload**).
</details>

<details><summary><b>A camera's preview stays black</b></summary>

The browser cannot decode the camera's codec over WebRTC (usually H.265). Use the camera's H.264 substream for the
preview; the main stream still plays on video receivers.
</details>

<details><summary><b>The add-on page says it cannot reach the engine</b></summary>

Set `engine_url` in the add-on's configuration to the hub's address and restart the add-on; the hub must be installed
with `install.sh --hub`.
</details>

## Status and roadmap

Used every day in one house — a hub, a Raspberry Pi 5 node, a PC node and Home Assistant — and still young. Planned:
access control for the designer and the APIs (today they are open on your LAN; the HA sidebar is behind HA's login),
sample-aligned (timestamp-locked) playout between rooms, prebuilt engine images, a local-engine mode for powerful Home Assistant hosts, and a zero-config MQTT link from the
add-on. Issues and pull requests are welcome.

## License

[MIT](LICENSE)
