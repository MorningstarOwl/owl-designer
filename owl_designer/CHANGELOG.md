# Changelog

## 0.12.0
The designer canvas, reworked. Nothing here changes what a design does in the room: boxes, notes, signal names and
where things sit are saved with the design (draft, deploy, history, export) under `canvas` and `layout`.
- **Wires are routed.** Every wire runs at right angles AROUND the blocks instead of in a curve through them; wires
  that share a channel get their own lanes, ordered so they cross as little as possible; the wires of one output share
  a trunk; a wire hops over one it crosses. Routes follow a block while it is dragged.
- **Organize** (top bar) tidies the canvas: signal flows left to right (sources left, outputs right), blocks are
  ordered and lined up so most wires run straight. The blocks of a box stay together and the box fits around them.
- **Boxes and notes.** A coloured, titled box behind a group of blocks (drag its title: everything in it moves along;
  "Box" with several blocks selected draws it around them), and sticky notes for what a design does not say by itself.
- **Signal names.** A wire can be shown as a name tag on both ports instead of a line (select the wire): for long runs
  and loops. Another input picks a named signal up from its block's panel, without a wire across the canvas. The wire
  itself is unchanged and deploys the same.
- **Device drawer.** Only what is wired sits on the canvas; every other device waits in the left panel's Devices list
  and is added with a click or by dragging it onto the canvas (block types can be dragged in too).
- **Undo / redo** (Ctrl+Z, Ctrl+Shift+Z, top bar) for every edit of the draft, up to 100 steps.
- **Signal trace.** Selecting a block or a wire lights up its whole path, upstream to the sources and downstream to
  every output, and dims the rest (it can be turned off).
- **Several blocks at once.** Shift+drag or Ctrl+click selects; drag one to move all; line up, spread out, box, or
  duplicate them from the bar that appears; Ctrl+C / Ctrl+V / Ctrl+D copy processing blocks with the wires between them.
- **Wire colours** by source or by machine (or plain); in Run mode the wire's state keeps the say.
- A dragged block snaps where a wire to its neighbour runs dead straight, or in line with another block (guide lines
  show it). Ctrl+F finds a block by name, type or machine. A minimap. A new block lands in a free spot in view.
- Fixed: moving blocks without changing the design (so also Organize) was forgotten on reload until the next deploy;
  and a draft started on the running design could come back as "an older draft" (it carried the previous deploy's stamp).
- Fixed: the page handed blocks back to the canvas without their measured size, so every refresh hid and re-measured
  all of them for a moment (clicks could fall through to the background).

## 0.11.3
- `install.sh` finds the PTP hardware clock on Debian 13 / Raspberry Pi OS trixie: newer ethtool reports it as
  "Hardware timestamp provider index", so a Raspberry Pi 5 node fell back to the system clock and its audio drifted.
  Engine only; the add-on itself is unchanged.
- Troubleshooting for DAC HATs on a node: the overlay, more ALSA headroom, and a receiver latency above the card's
  delay.

## 0.11.2
- Home Assistant: a voice reply (or what was heard) longer than 255 characters no longer turns the Reply / Heard
  sensor "unknown": the state is shortened with an ellipsis and the whole text is the sensor's `text` attribute.

## 0.11.1
- Voice satellite fixes (engine only; the add-on itself is unchanged): a Home Assistant that stops reading costs
  dropped audio instead of a deaf satellite (per-link writer, real shutdown of dead links); no periodic wake re-arm
  (it stacked Home Assistant wake runs); the wake word is detected on the speaker itself (openWakeWord in the image),
  only the command goes to Home Assistant.

## 0.11.0
- Designer: **🔊 Inject** next to the probe: drag it onto any input, on any machine, to play pink/white noise or a sine
  at a set level for a set time. **Sounds** in the Design panel: upload doorbells, chimes and alarms; a new **Sound
  player** block plays one when its Play button is pressed (inspector, Panels, Home Assistant). Needs owl-node 0.11.0.

## 0.10.0
- Designer: **Export** / **Import…** design files and a **History** of the last 20 deployed designs (Design panel)
  to open on the canvas (needs owl-node 0.10.0 on the hub).
- Engine (owl-node 0.10.0): faders and fades respond at once (one long-lived PipeWire control connection instead of a
  process per change; 10 ms fade steps); all meters run in one process; a **Needs attention** sensor in Home Assistant
  and Prometheus metrics at `:8362/metrics`.

## 0.9.0
- Designer: **Network latency** in the Design panel (how long receivers buffer streams between machines; one value
  keeps rooms in step) and a latency per wire in its inspector (needs owl-node 0.9.0 on the hub and the nodes).
- Engine (owl-node 0.9.0): every node keeps a backup of the hub; `install.sh --hub --restore-from <node>` brings the
  designs, panels, presets and nodes back on a replacement hub.

## 0.8.0
- Designer: AES67 streams that Dante devices (AES67 mode) and other AES67 gear announce on the network appear as
  sources, with each channel pair of a multichannel flow as its own output (needs the hub on owl-node 0.8.0).
- Engine (owl-node 0.8.0): one sender carries several sources side by side on separate channels (Dante-style flows);
  a hub's sender has 8 channels, so four stereo streams leave the hub over one stream; `install.sh --ptp-udp` serves the
  house clock to Dante gear.

## 0.7.3
- Routing page: video previews show whether they are live again (the add-on now asks the hub's engine, owl-node 0.7.3
  or later, instead of MediaMTX's API, which only listens on the hub itself).
- Engine (owl-node 0.7.3): different sources wired to different rooms get their own AES67 senders instead of being
  mixed into one; nodes stand by as the network clock when the hub is off; IS-05 activations can no longer deadlock;
  saved state survives power cuts.

## 0.7.2
- Documentation tab rewritten, with a picture and first steps.
- Designer: Panels pages fit phone screens (a row of page tabs made them wider than the screen); auto-placed blocks no
  longer land on top of each other; long block names are cut off instead of running over their badges.

## 0.7.1
- First add-on release: the designer and Routing page in the Home Assistant sidebar (Ingress), with the audio engine on
  another machine (`engine_url`). Optional LAN port 8361.
- Ships as a prebuilt image for amd64 and aarch64 (`ghcr.io/morningstarowl/owl-designer-addon:0.7.1`): Home Assistant
  pulls it and builds nothing.
