# Owl Designer

A Q-SYS-style audio designer for a whole house, built on PipeWire, AES67 and AMWA NMOS, with Home Assistant in mind.

- **One canvas for every machine.** Every speaker, microphone, app stream and network audio port of every machine
  running owl-node appears as a block with ports. Wire them by drag and drop; Deploy turns the wires into PipeWire
  links, AES67 network streams and NMOS patches.
- **Processing blocks:** EQ, compressor, limiter, ducker, gain, delay, mixer, router, meters, and an acoustic echo
  canceller (WebRTC AEC3), on the hub or on any node. A block type is one YAML file.
- **Voice assistant block:** the hub is a Home Assistant voice satellite (Wyoming). Wire the echo-cancelled mic into it
  so the TV and music do not drown your wake word; replies duck the music.
- **RTSP sources:** paste a camera's `rtsp://` or `rtsps://` link. Its audio becomes a source you can wire anywhere; its
  video can be routed to any node's video receiver (H.264/H.265, hardware decoding on a Raspberry Pi 5).
- **Listening probe:** drag 🎧 onto any port, block or wire and hear it on the computer you are using.
- **Home Assistant:** every named control and meter appears in HA over MQTT; the designer can live in HA's sidebar.
- **Video (experimental):** H.264/HEVC over SRT and JPEG XS over RTP between nodes, with browser previews.

## Pieces

| | What | Where |
|---|---|---|
| **Hub** | NMOS registry, the DSP engine, the designer web page (`http://<hub>:8361/`) | one Linux machine with sound hardware |
| **Nodes** | AES67 audio, the node's own PipeWire devices, optional processing and video | any other Linux machine (a Pi 5 works) |
| **Home Assistant add-on** | the designer in HA's sidebar; the engine stays on the hub | HA OS / Supervised |

## Install

Every machine needs Linux with PipeWire (any desktop install), Docker Engine with the compose plugin, and a wired
network port (AES67 and PTP need a cable; a network card with a PTP hardware clock gives sample-accurate sync).
Run as the desktop user; the script asks for sudo itself.

```
git clone https://github.com/MorningstarOwl/owl-designer.git && cd owl-designer
owl-node/install.sh --hub      # on the hub
owl-node/install.sh            # on every node
```

The installer finds the network interface, the PTP clock and its role, builds the image on the spot (minutes on a PC,
20-40 min on a Pi) and asks whether to start at boot; `--dry-run` shows what it would do first. Then open
`http://<hub>:8361/`, type each node's IP in **Nodes** and press **Add**: its inputs and outputs appear on the canvas.
More: [owl-node/README.md](owl-node/README.md), `owl-node/install.sh --help`.

### Home Assistant add-on

Settings → Add-ons → Add-on store → ⋮ → Repositories → add `https://github.com/MorningstarOwl/owl-designer`, install
**Owl Designer**, set `engine_url` to your hub (e.g. `192.0.2.10`) and start it. The designer opens from the sidebar
behind Home Assistant's login; the audio processing stays on the hub, so a small HA host is fine.

## Status

Early and used daily in one house: a hub, a Raspberry Pi 5 node, a PC node and Home Assistant. Not yet: access
control (the designer and APIs are open on your LAN; the HA sidebar is behind HA's login), prebuilt images (each
machine builds its own), and a local-engine mode for the add-on (the HA host doing the processing itself).

## License

MIT
