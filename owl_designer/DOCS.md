# Owl Designer

The designer for owl-node: one canvas with every audio device of every owl-node machine in the house (speakers,
microphones, apps, AES67 network senders and receivers), wired by drag and drop, with processing blocks (EQ, ducker,
limiter, echo canceller), a voice-assistant block, a listening probe, Panels and Home Assistant controls over MQTT.

## How it is split

Audio processing needs a machine with real sound hardware and some CPU; many Home Assistant hosts are neither. So the
work is split:

- **This add-on** runs the designer web page and the Routing page. Open it from the sidebar (Home Assistant signs you
  in) or, if you set a port in the Network section, directly on your network.
- **The engine** runs on a machine you choose: install owl-node there with `owl-node/install.sh --hub`. It
  holds the audio graph, the processing blocks, the NMOS registry and the voice satellite.
- **Nodes** are any other machines with `install.sh` (no options); add them in the designer's Nodes box by IP.

## Configuration

| Option | |
|---|---|
| `engine_url` | The engine machine, e.g. `192.0.2.50` (port 8362 is assumed) or `http://192.0.2.50:8362`. |
| `registry_url` | Optional. The NMOS registry for the Routing page; empty = the engine machine on port 8010. |

The engine publishes its controls to Home Assistant through MQTT (the Mosquitto broker add-on works); its voice
satellite is found by the Wyoming integration.

Video previews on the Routing page stream from the engine machine directly (WebRTC), so they work on your home
network, not through Home Assistant's remote access.
