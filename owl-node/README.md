# owl-node: AES67 audio, H.264/HEVC and JPEG XS video endpoints with NMOS discovery and routing (N2, N3, N4), and the hub's DSP designer (D1, D2)

Steps N2, N3 and N4 of `the design notes` (satellite track). One container image turns a Linux host into a
Dante-style audio endpoint and an NVX-style video encoder or decoder. Its AES67 senders and receivers are ordinary
PipeWire devices on the host; its video senders encode with VA-API and serve SRT, its video receivers decode in
hardware (the Pi 5 too). Everything registers with an AMWA NMOS IS-04 registry, and any receiver can be patched to
any sender through IS-05 from a Routing page in the browser, which also previews every video sender over WebRTC.
N4 adds an experimental **video tier 2**: JPEG XS (SVT, CPU) in RTP (RFC 9134) over plain UDP, unicast by default,
advertised as BCP-006-01 `video/jxsv` flows. Measurements and decisions: the plan's **"N2 as built"**, **"N3 as
built"** and **"N4 as built"**.

```
 hub (media box)                                        satellite (pinode, ...)
 ─────────────────────────────────────                  ───────────────────────────────────────
 owl-ptp4l.service  (host, PTP grandmaster, L2) ◄─PTP─► owl-ptp4l.service (host, follower, L2)
 owl-node-ptpstat   (host, pmc -> ptp.json)              owl-node-ptpstat
 registry    nmos-cpp, bridged, :8010/:8011  ◄─IS-04─── agent  :8360  IS-04 node + IS-05 + /x-owl/v1/status
 controller  designer + Routing page :8361 ───IS-05────► agent  -> state.json -> audio
 dsp         :8362 inventory, compiler, deployer (D1): pw-dump + registry -> blocks; wires -> pw-link + IS-05
             named controls, WebSocket, presets, panels, MQTT -> Home Assistant (D2)
 agent + audio (the hub is a node too)       ◄─AES67──► audio  pipewire clients in the host's graph
```

## Quick start (E2): a hub and nodes on your own network

Every machine needs Linux with PipeWire (a desktop install has it), Docker Engine with the compose plugin, a wired
network port, and this repository checked out. Run as the desktop user (not root; the script uses sudo itself).

```
# on the machine that will be the hub (registry, designer, DSP engine):
owl-node/install.sh --hub
# on every other machine (a node):
owl-node/install.sh
```

The installer finds the wired interface, a PTP hardware clock and the PTP role by itself, gives an audio node one AES67
sender and one receiver, builds the image on the spot when it is not loaded (minutes on a PC, 20-40 min on a Pi) and
asks whether to start at boot. Then open the hub's designer at `http://<hub IP>:8361/`, type each node's IP in
**Nodes**, press **Add**: the hub pairs it (no registry address to configure on the node) and its inputs and outputs
appear on the canvas. `--dry-run` shows everything it would write first; re-running it on an installed machine keeps
the name, roles, senders/receivers, registry and start-at-boot choice of the last install. Without a PTP hardware
clock (many laptops and USB network adapters) a node still lists its devices, but its AES67 audio drifts against the
hub's (plan E4). The full option list: `install.sh --help`; the plan: `docs/owl-designer-portable-plan.md`.

## The designer (D1, hub only)

`http://192.0.2.10:8361/` is a Q-SYS-Designer-style canvas (React Flow, vendored, works at phone width). Every
device's ports are on it: one block per PipeWire node on the hub (the ALC269 output and mic, HDMI, squeezelite, any
USB interface that appears) and one block per NMOS node with its senders as outputs and receivers as inputs (every
satellite, within seconds of its container registering; the hub's own AES67 sender/receivers are hidden because they
are transport the compiler allocates). Processing blocks come from a palette; a block type is one file in
`dsp/blocks/*.yaml` (ports, params, which PipeWire nodes carry which port, live controls, and a PipeWire config
template), so adding a type never touches the canvas or the compiler. v1: **AEC** (module-echo-cancel, WebRTC AEC3),
Gain, Mixer 4x2, Router 4:1, Test Tone, Meter. v2 (D2): **Compressor** (swh SC4), **Limiter** (swh fast lookahead),
**Parametric EQ** (swh triple-band + shelves, per channel), **Delay** (builtin, 5 s), **Ducker** with a sidechain
(the house's own LADSPA plugin, `ladspa/owl_ducker.c`), **App Input** (a named sink apps play into), **DJ EQ** (the
15-line example of wrapping any LADSPA plugin). A v2 block is a `plugin:` shorthand in its YAML (plugin, label, port
names, control expressions): nodes, live controls and the filter-chain config are generated. Ubuntu's PipeWire has no
LV2 loader, so LSP comes as its LADSPA build and x42/calf (LV2-only) are out until the image carries a PipeWire built
with lilv.

Three modes: **Design** (edit; the draft is saved on the hub, so a phone and a desktop see the same canvas), **Run**
(live: block state, PTP badge per node, packets/s and loss per NMOS port and per cross-host wire, RMS meters on every
wired hub port and block output, the same data `owl-node.sh` reads), **Panels** (D2: phone pages of named controls,
below), and **Routing** (the N2 grid, unchanged, as a view). **Deploy…** shows the diff (processes to
start/stop/restart, links and patches to make/remove, meters) before anything changes; **Rollback** re-applies the
previous deployed design (twice = redo); **Reset** discards the draft.

**A wire is a wire.** The compiler (`owlnode/dsp/compiler.py`) decides the transport: both ends on the hub = a
PipeWire link; a satellite output into a hub input = an IS-05 patch of a hub AES67 receiver (reused if it already
carries that sender, else a free one) plus a link from the receiver's node; a hub output into a satellite input = a link
into the hub's AES67 sender plus a patch of the satellite's receiver; satellite to satellite = a patch (video included,
with N4's unicast aiming done by the controller's patch code). Deploy is idempotent and owned: re-deploying the same
design changes nothing, removing a wire removes only what the compiler created for it, and links WirePlumber or apps
made (squeezelite -> speaker) are shown grey and never touched. A deploy that fails verification (a block's nodes or a
link missing) rolls back to the previous design by itself.

The `dsp` role (`owlnode/dsp/`, the plan's `owl-dspd`) runs in the hub's compose stack with the audio role's mounts:
each processing block is its own `pipewire -c` client process of the host's daemon (the pattern owl-aec and the AES67
jobs use), meters are passive `pw-record` taps, and the deployed design lives in `/var/lib/owl-node/dsp-design.json`
(+ `.prev.json`, `dsp-state.json` for what it owns, `dsp-draft.json` for the canvas). At start it restarts its block
processes (retrying until the registry and the hub's agent are up; a device that is absent leaves only its wires pending
and they are wired in when it appears) and a 2 s reconcile loop re-makes links whose nodes came back and re-patches a receiver its far end lost
(after a 15 s grace, the agents restore their own patches first), so a design survives a reboot of the hub or of any
satellite. API: `/api/dsp/{inventory,palette,design,draft,plan,deploy,rollback,clear,control,status}` (the controller
proxies it; `curl -s -X POST :8361/api/dsp/plan -d '{"design": ...}'` is what the Deploy button does).

## Named controls, Panels, presets and Home Assistant (D2, hub only)

Every parameter of every deployed block and every meter is a **named control**: key `<block id>.<param>` (stable;
panels, presets and HA use it) and name `<slug of the block label>.<param>` (what people type:
`living_room_volume.gain_db`); meters are `<block>.<port>.level` / `<meter block>.level`; presets `presets.recall`
(select) and `preset.<slug>.recall` (trigger); read-only system values `design.name`, `design.state`, `ptp.state`,
`ptp.offset_ns`, `registry.nodes`. Renaming a block changes the names; the old ones keep resolving through an alias
until the next deploy (the plan says so), and the keys never change. API: `GET /api/dsp/controls` (everything with
values), `GET /api/dsp/controls/<key|name>`, `POST /api/dsp/controls/set {key|name, value, fade_s?}`, and
`ws://hub:8361/api/dsp/ws` (tunnelled by the controller): a `hello` with the listing, then every change
(`{"type":"control", key, name, value, source}`), meters at 4 Hz, design/preset events; send `{"type":"set", ...}` or
`{"type":"recall","preset":...}`. One change path for everything (page, API, MQTT, presets), so all of them stay in step.

**Panels** are pages of controls stored with the design (`design.panels`), one per room: in Edit, drag a control from the
list onto the page (or tap ＋), drag items to reorder, ⚙ for label, widget (fader / toggle / select / button / meter /
text), size and fader range; Done, and the page is a phone remote with live values both ways. `PUT /api/dsp/panels`
changes no audio. **Presets** (`design.presets`) store the live values of chosen controls; recall sets them back, fading
numeric live controls over `fade_s` (40 ms steps); they are controls themselves and HA entities (a select plus a button
each). `POST /api/dsp/presets {name, keys|values, fade_s}`, `POST /api/dsp/presets/recall {preset, fade_s?}`,
`DELETE /api/dsp/presets/<id>`.

**Moving an app's stream.** A wire from an application stream (squeezelite, pw-play) into a block can carry
`"move": true` (the wire inspector's checkbox): the deployer sets `target.object` metadata on the stream and WirePlumber
takes it off its current output (the speaker) and links it into the block, remembering the choice for that stream; when
the block is gone the stream falls back to its own output, so music never stops because a design is down. That is how
the "Living room" design puts Music Assistant's player through the ducker and volume without touching squeezelite's
`-o`. A wire without `move` is a tap (the block gets a copy; the AEC's reference works that way). `allow_missing` on
plan/deploy (the modal's "Deploy anyway") leaves wires to absent devices pending (a player whose stream exists only while
it plays) and wires them within 2 s of appearing.

**Home Assistant over MQTT** (`owlnode/dsp/mqtt.py`, paho): one device per DSP (the hub), retained discovery under
`homeassistant/<component>/owl_dsp_<hub>/...`: a `number` (slider when live, box in the config category when it
restarts the block), `switch` or `select` per control, a `sensor` (dBFS) per meter, a `select` + `button` per preset,
diagnostic sensors for the deployed design, DSP state, PTP state/offset and NMOS node count; availability topic with a
last-will, re-publish on reconnect and on HA's birth message, stale entities removed when a design drops them. Broker:
`install.sh --mqtt mqtt://user:pass@host:1883` (-> `OWL_MQTT_URL` in `.env`) or `/var/lib/owl-node/mqtt.json`
`{host, port, username, password}`; nothing configured = bridge off, `designer.sh` WARNs. Topics:
`owl-dsp/<hub>/control/<key>/state|set`, `owl-dsp/<hub>/meter/<key>/state`, `owl-dsp/<hub>/preset/state|set`.

Checks: `scripts/checks/designer.sh` (deployed design = running state, with levels; D2 adds Controls, Move and MQTT
lines), `scripts/checks/dsp-blocks.py` (every block with a `test:` section: tone in, expected level out, timing by
recording for the ducker and the delay), `scripts/checks/ducking.py` (music + an HA announcement, the dip measured),
`scripts/checks/aec-erle.py`
(ERLE of the deployed AEC: `--play bursts --play-sink NODE` generates and plays a speech-like stimulus on the hub, `--play FILE` plays a WAV, no `--play` measures passively while music plays). Measurements:
the plan's **"D1 as built"**.

## Processing on the satellites, push-to-talk, device blocks (D3)

**Satellite `dsp` role** (`--roles agent,audio,dsp`; compose profile `dsp`; the same service as the hub's, in
satellite mode when the node has no `hub` role). It is a PipeWire *executor*, not a designer: the hub's `dsp` compiles
the design, splits off each satellite's **share** (the block processes placed on that host, the links between its own
PipeWire nodes, its meter taps, the live control values) and pushes it (`PUT /api/dsp/share`); the satellite applies it
against its own daemon (PipeWire 1.2 on pinode; the blocks are `pipewire -c` clients, like the audio role's), persists
it in `/var/lib/owl-node/dsp-share.json`, restores it by itself after a restart, and reports its graph
(`GET /api/dsp/graph`, for the hub's inventory), the state of every share link and its meters (`GET /api/dsp/status`).
The hub finds satellites through the registry (every non-hub owl-node is probed on :8362), polls them at 2 Hz (live
meters on the canvas), re-pushes a share whose digest differs (within 5 s of a satellite coming back), and sends live
control changes as `POST /api/dsp/control {set, procs}` (no restart). In the designer a processing block gets a
**Runs on** select (hub or any satellite with a dsp role); a satellite's own PipeWire ports (its AES67 receiver as a
source, its sender as a sink, its sound cards) appear as device blocks; wires across hosts get AES67 transport
automatically (satellite output -> hub: a link into the satellite's sender + a hub receiver patched to it; hub ->
satellite: the hub's sender + the satellite's receiver; satellite A -> B: A's sender -> B's receiver). Stream moves stay
hub-only. `upgrade-satellite.sh` is the household's re-install of pinode with the role (`install.sh` needs sudo there).

**Stream watcher** (D2's open item 10): the hub's dsp runs `pw-mon` and re-targets a moved app stream the moment its
node appears, so the first announcement after the announce player restarts is ducked from its first sample
(`scripts/checks/ducking.py --first` proves it). WirePlumber 0.5 does not persist `target.object` for these streams
(the box's `restore-stream` state holds volumes only), so the 2 s reconcile alone was too late.

**TV optical feed**: a Cubilux USB S/PDIF receiver on the TV's optical out is a plain device block
(`alsa_input.usb-0c76_Cubilux_SPDIF_Receiver-00.analog-stereo`), wired into the AEC's reference next to the limiter's
output. Keep the TV on PCM output. The analog rear-jack retask of the plan is moot.

## What is in the image (`owl-node:<version>`, Ubuntu 26.04, amd64 + arm64)

| Role (`command`) | What it does |
|---|---|
| `agent` (always) | IS-04 v1.3 Node API and registration client (static `registry:` URL, else mDNS `_nmos-register._tcp`; heartbeat 5 s; failover by heartbeat, re-register on 404; `_nmos-node` advert in peer-to-peer mode only). IS-05 v1.1 Connection API (single + bulk, immediate/scheduled activation, `auto` resolution, SDP transport files). Activations are written to `/var/lib/owl-node/state.json`, so a patch survives restarts and reboots. `/x-owl/v1/status`: PTP lock, and per receiver the packets/s, loss and level it sees on its own multicast socket. |
| `audio` | A supervisor that runs N1's AES67 setup as separate `pipewire -c` client processes of the **host's** daemon (socket from `/run/user/<uid>`, mounted read-only): `base` (PTP driver on the NIC's PHC + SAP announcer), one `tx-*` per sender, and one `rx-*` per *active* receiver, re-rendered whenever `state.json` changes. Receivers: `sess.latency.msec = 6` (N1), adaptive playout by default (`ts_direct: true` in `node.yaml` for N1's timestamp-direct mode, see the plan's open problems), and every AES67 stream pins the PTP graph to 128 frames (`quantum:`). |
| `video-enc` (N3) | A supervisor that runs one `vpipe` process (GStreamer 1.28 in Python) per enabled video sender: source → VA-API `vah265enc`/`vah264enc` (x264 without VA) → **RTP over SRT** (default; `urn:x-matrox:transport:srt.rtp`) or MPEG-TS over SRT (`encap: ts`, for VLC/OBS/ffmpeg) from an SRT **listener**; plus a 640×360 H.264 preview published to MediaMTX on the hub. Sources: `testsrc` (SMPTE bars, a scrolling strip, burnt-in clocks and a machine-readable frame stamp in PTP time) or `v4l2` (a UVC capture stick: `source=v4l2,device=/dev/videoN,format=yuy2|mjpeg`). |
| `video-dec` (N3) | One `vpipe` per *patched* video receiver: SRT **caller** → `v4l2slh265dec` (Pi 5), `vah265dec`/`vah264dec` (x86) or libav → `kmssink` on an attached display (`display=kms`), and/or the headless verifier (`display=none`): frame rate, stamped-frame gaps, capture-to-decode latency, PSNR and luma checksums. |
| `video-enc` + `jxs` (N4) | A JPEG XS sender is `svtjpegxsenc` → `rtpjxsvpay` (RFC 9134, codestream mode) → `udpsink`. With `source_of: <sender>` it is one more branch of that sender's process (one source, two flows, shared preview; a capture stick can only be opened once); without, it gets a process of its own that runs only while patched. Unicast (`urn:x-nmos:transport:rtp.ucast`) by default: the controller points the sender at the receiver over IS-05, and a valve keeps the encoder idle while it is parked. Patches reach the running process through a `.dyn.json` file, so they never restart the H.265 stream next to it. A UDP echo socket per sender records capture → decoded-on-the-receiver latency on the sender's own PTP clock. |
| `video-dec` + `jxs` (N4) | `codecs: [jxs]` makes a receiver an RTP one (`urn:x-nmos:transport:rtp`, unicast or multicast, BCP-006-01 constraint sets): `udpsrc` → `owlrtpstat` (packets, sequence gaps, arrival gaps) → `rtpjxsvdepay` → `svtjpegxsdec` → verifier/`kmssink`; it echoes each stamped frame back to the sender. |
| `controller` (hub) | The designer (`http://hub:8361/`, D1) and the Routing page (`/routing`): senders × receivers grid from the registry's Query API, click to patch (IS-05 PATCH with the sender's SDP, `activate_immediate`), live PTP and packet status from every owl-node. Works with any NMOS node. Proxies `/api/dsp/*` to the dsp role. |
| `dsp` (hub, D1/D2) | The designer's engine: inventory (pw-dump + the registry), block types from `dsp/blocks/*.yaml`, the compiler (wires -> PipeWire links + IS-05 patches + stream moves), the deployer (diff, verify, rollback, reconcile every 2 s), passive meter taps, named controls with a WebSocket, presets with fades, panels, the MQTT/HA bridge. Block processes are `pipewire -c` clients of the host's daemon, like the audio role's. |
| `mediamtx` (hub, separate image) | `bluenviron/mediamtx` v1.21.3, pinned: SRT publish in (:8890), WebRTC/WHEP out (:8889 HTTP, :8189/udp ICE); everything else off. The Routing page's ▶ button plays a sender's preview inline (WHEP signalling relayed by the controller, so only :8361 is needed for HTTP). |
| registry (hub, separate image) | `rhastie/nmos-cpp` (Sony's nmos-cpp registry; nmos-js is served at `:8010/admin` as a fallback controller). Bridged, so its mDNS responder stays off the LAN; nodes get its URL from `node.yaml`. |

PipeWire is pinned at 1.6.2 inside the image on every node (pinode's Debian 12 host runs 1.2.7; the 1.6 clients were
tested against a 1.2.7 daemon + WirePlumber 0.5.7). `ptp4l` stays a host package (N1's pattern): a container that
disciplines the clock is the one part that should not be "just a container". The audio container needs no root, no
capabilities and no rtkit: real-time comes from `ulimits` (rtprio 95, nice 40, rttime unlimited).

## Files

| File | What |
|---|---|
| `Dockerfile`, `entrypoint.sh` | the image; `owl-node agent|audio|video-enc|video-dec|controller|version|shell` |
| `owlnode/` | `agent.py` (IS-04/IS-05 HTTP, registration, mDNS), `connection.py` (IS-05 state machine, RTP and SRT parameters), `sdp.py` (AES67 and video SDP), `rtpstat.py`, `audio.py` (PipeWire supervisor + config generator), `video.py` (video supervisor), `vpipe.py` (one GStreamer stream: encoder, decoder, stamp, verifier), `controller.py` + `web/routing.html` + `web/designer.html` (+ `web/vendor/`: React 18, React Flow 11) |
| `owlnode/dsp/` | (D1) `blocks.py` (block types, the template language, the D2 `plugin:` shorthand), `pw.py` (pw-dump/pw-link/pw-cli/pw-metadata), `inventory.py`, `compiler.py`, `deploy.py`, `meters.py`, `server.py` (the `dsp` role's API on :8362); (D2) `controls.py` (named controls, presets, fades, the event bus), `ws.py` (WebSocket), `mqtt.py` (HA discovery bridge) |
| `dsp/blocks/*.yaml` | the block palette: `aec`, `gain`, `mixer`, `router`, `tone`, `meter` (D1); `compressor`, `limiter`, `eq`, `delay`, `ducker`, `appsink`, `dj-eq` (D2, each with a `test:` section); `dsp/designs/`: the AEC test designs, `living-room.json` (D2: ducking + volume + limiter + AEC, panel and presets) |
| `ladspa/owl_ducker.c` | (D2) the house's LADSPA ducker (stereo program, stereo sidechain; depth/threshold/attack/hold/release, linear dB ramps), built into the image at `/opt/owl-node/ladspa/` |
| `gst/gstowlpoolfix.c` | a ~100-line GStreamer element built into the image: lets upstream `v4l2slh265dec`'s SAND128 DMA-BUF output (Pi 5) negotiate with any sink (see Things that bit) |
| `gst/gstowlrtpstat.c` | (N4) a passthrough element counting RTP packets, sequence-gap loss and arrival gaps at 16-60 k packets/s, where a Python probe would cost a core |
| image build stage `jxsbuild` | (N4) SVT-JPEG-XS v0.10.0, GStreamer's `svtjpegxsenc/dec` compiled from gst-plugins-bad 1.28.2, and `rtpjxsvpay/depay` from gst-plugins-rs main (pinned commit); none is packaged for Ubuntu 26.04. +5-6 MB per arch |
| `mediamtx/mediamtx.yml.in` | the hub's MediaMTX config (installer fills in the hub IP) |
| `docker-compose.yml` | services `agent` (always), `audio` (profile `audio`), `video-enc` / `video-dec` (profiles of the same name), `controller` + `dsp` + `registry` + `mediamtx` (profile `hub`); host networking |
| `install.sh` | per-host installer: `/etc/owl-node/node.yaml`, `/opt/owl-node/{docker-compose.yml,docker-compose.devices.yml,.env}`, tmpfiles for `/run/owl-node` + `/var/lib/owl-node`, the PHC udev rule, `owl-node-ptpstat.service`, optionally `owl-ptp4l.service`; `--vtx`/`--vrx`/`--video-dev` for video; `--dry-run`, `--remove`, `--enable` |
| `host/owl-node-ptpstat.py` | root helper on the host: `pmc` → `/run/owl-node/ptp.json` every 2 s |
| `build.sh` | native builds: amd64 here, arm64 on pinode's Docker over ssh; `--amd64-to` copies to the box |
| `ptp/ptp4l-gm-udpv4.cfg` | the hub's optional second ptp4l instance (UDPv4, server-only) for Dante/AES67 gear; tested, not deployed |
| `registry-container-config` | entrypoint settings for the nmos-cpp image (no MQTT, no Mellanox probe, no relabel) |
| `../../scripts/checks/owl-node.sh` | health check: registry, every owl-node's registration/PTP/processes, and wire stats of every patched receiver |
| `../../scripts/checks/designer.sh`, `aec-erle.py` | (D1) the deployed design is what runs, with levels; ERLE of the deployed AEC block |
| `../../scripts/checks/dsp-blocks.py`, `ducking.py` | (D2) every block's scripted test (level + timing); announcement ducking end to end with HA and Music Assistant |
| `../../media-machine/owl-squeezelite-announce.service` (+ `.env.example`, `install-squeezelite-announce.sh`) | (D2) the second squeezelite player ("Living Room Announce") whose stream the Living room design moves into the ducker's sidechain |
| `upgrade-hub.sh` | (D2) re-install the hub from the checkout: dev stack down, pinode's video receiver parked, `install.sh` with the hub's arguments + `--enable`, wait for the restored design (`--dry-run`, `--repatch`) |

## Build and install

```bash
./build.sh --amd64-to ssh://user@192.0.2.10     # amd64 here + box, arm64 natively on pinode
```

```bash
# on the hub (media box), from a copy of satellite/: registry + Routing page + a node with one sender and two receivers
./install.sh --name livingroom --label "Living Room" --area "Living Room" --roles agent,audio,hub \
  --media-if eno1 --phc-if eno1 --tx "Living Room" --rx "Living Room RX 1" --rx "Living Room RX 2" --replace-n1
```

```bash
# on pinode: IP on bridge br0, PHC on the bridge port eth0, PTP stays L2 (see the plan's PTP transport decision)
./install.sh --name pinode --label pinode --area Office --roles agent,audio --media-if br0 --phc-if eth0 \
  --registry http://192.0.2.10:8010 --tx pinode --rx "pinode RX 1" --replace-n1
```

Video (N3), added to the same commands (keep `--enable` if the host already has it):

```bash
# hub: one HEVC test-pattern sender (SRT listener on :9001, preview in MediaMTX)
./install.sh ...hub args as above... --vtx 'Living Room test pattern|codec=h265' --enable
# pinode: one headless receiver (HEVC hardware decode + verifier); display=kms once a screen is attached
./install.sh ...pinode args as above... --vrx 'pinode video RX 1|display=none' --enable
# later, a capture stick on the hub: config only
./install.sh ... --vtx 'TV HDMI|source=v4l2,device=/dev/video0,format=yuy2,fps=60,codec=h265,bitrate=20000' --video-dev /dev/video0
```

JPEG XS (N4), again added to the same commands:

```bash
# hub: a JPEG XS sender of the same test pattern (4:1 at 3 bpp; unicast; parked until a receiver is patched)
./install.sh ...hub args... --vtx 'Living Room test pattern|codec=h265' --vtx 'Living Room JPEG XS|codec=jxs,source_of=vtx1' --enable
```

PCNode's JPEG XS receiver is a **user-space node** (no sudo, nothing at boot): `~/owl-node-pcnode/` holds a copy
of `docker-compose.yml`, an `.env` that points `OWL_ETC`/`OWL_STATE`/`OWL_RUN` into that folder (`OWL_PTP_DEV=/dev/null`,
`OWL_RESTART=no`) and an `etc/node.yaml` with `roles: [agent, video-dec]`, `clock: {phc_if: none}` and one receiver
`{id: vrx1, label: "PCNode JPEG XS RX", codecs: [jxs]}`. Start/stop: `docker compose --env-file .env --profile
video-dec up -d` / `down` in that folder. A second user-space node on the same host needs `-p <project>` and its own
`http_port` (the compose file names its project `owl-node`).

`--ptp keep` (the default) uses the host's existing `owl-ptp4l.service` (installed by N1); `--ptp grandmaster|backup|follower`
installs it the same way N1 does (`backup`, a fresh node's default, takes over as grandmaster while the hub is down;
see `aes67/README.md`). `--replace-n1` stops N1's hand-run `owl-aes67` user unit (both would create a PTP
driver and announce the same sessions); it does not remove N1's files. Without `--enable` nothing comes back after a
reboot (containers have `restart: no`, units are not enabled). With it, `owl-node.service` runs `docker compose up -d`
at boot once the user's PipeWire socket exists; the box and pinode run this way since 2026-11-08 (reboot-tested).

Senders are named `aes67-tx-<name>` (first) and multicast to `239.69.<n>.<last octet of the host IP>`, the N1 scheme,
so the hub's first sender is still `239.69.1.126`. Receivers are `aes67-rx-<name>-<n>` Audio/Source nodes that exist
only while patched; `--rx "Label>sink.node.name"` makes the audio role link a receiver to a host sink whenever it is up.
Nothing is ever linked to a speaker by default.

Patch from the browser: `http://192.0.2.10:8361/`. From a shell (same API the page uses):

```bash
curl -s -X POST http://192.0.2.10:8361/api/patch -d '{"receiver_id":"<rx uuid>","sender_id":"<tx uuid or null>"}'
```

Check: `scripts/checks/owl-node.sh`. Undo: `./install.sh --remove` on each host (keeps the image and owl-ptp4l; the
patch state is moved aside, not deleted).

## Testing it without the hosts (what PCNode ran)

- `amwa/nmos-testing` + an agent on a Docker bridge (`interfaces: {media: eth0}`, no `registry:` so it finds the
  tool's mock registries over mDNS): suites IS-04-01, IS-05-01, IS-05-02 via `POST :5000/api` (N4: also
  `BCP-006-01-01`, with the node's host/port/version only). One agent per tool instance: two agents on one bridge
  both heartbeat the tool's mock registry and fail IS-04 test_05/test_21.
- `rhastie/nmos-cpp` as registry and, with `RUN_NODE=TRUE` and `"senders": ["a"], "receivers": ["a"]`, as the
  reference node to patch to and from.
- A test node with `clock: {phc_if: none}`, `multicast_ttl: 0`, `multicast_loop: true`, `sap_announce: false`:
  real audio through the host's PipeWire, but nothing leaves the machine (N1's hosts auto-subscribe to any `owl-*`
  SAP session). Timestamp-direct playout needs a PHC driver, so system-clock test nodes use the adaptive path.

## Things that bit (D2, controls + ducking)

- **A `pipewire -c` client cannot export `context.objects`**: a null-audio-sink declared there is created locally and
  never appears in the daemon's graph (no error either). Sinks a block needs (the ducker's sidechain, App Input) are
  filter-chains of `copy` nodes instead (an Audio/Sink in, an Audio/Source out).
- **WirePlumber maps a moved stereo stream onto a sink's FL/FR**, never onto other channels: a 4-channel plugin sink
  (program + sidechain) got the announcement on the program channels. Hence the ducker's own 2-channel sidechain sink,
  linked internally into channels 3-4 (`internal_links:` in the YAML).
- **`target.object` metadata must be a bare node name or a serial**; a JSON-quoted name is ignored. WirePlumber then
  links the stream itself, so the deployer leaves those links to it (`wp` links: counted, never created or removed).
- **squeezelite's pulse output refuses to start when its `-o` sink does not exist**, so pointing players at design-owned
  sinks would stop music whenever the design is down: moving the stream from the design side is the safe direction.
  Both squeezelite players keep `-o <speaker>`; their stream only exists while the player is powered on (Music Assistant
  `power_control: none` keeps the announce player on, so its stream stays open after the first announcement).
- **The Test Tone's capture dummy must have as many channels as the graph has outputs** on PipeWire 1.6 ("invalid
  ports" and a silent block otherwise).
- **YAML**: `label: On` is a boolean; descriptions with `: ` need quotes.
- **Two `pw-record` taps start at different moments** (whenever their link lands), so timing measurements record all
  signals as channels of one stream (`dsp-blocks.py`, `ducking.py`).
- **The ducker's hold reads ~80 ms longer than set** at a 20 dB sidechain margin: it starts when the 20 ms detector falls
  below the threshold, not when the sound stops.
- **Build the image last.** A dev stack that bind-mounts the checkout hides a stale image: the box's 0.6.0 had been built
  before the D2 code existed and the official dsp came up with "unknown type ducker". `build.sh` after the last edit.
- **systemd drops `Environment=` names with a dot** (`PULSE_PROP_node.name=`), silently: use one `PULSE_PROP=` proplist.
- **HA `expire_after` needs keep-alives**: a silent meter that only publishes on change went "unavailable" after 120 s;
  the bridge re-publishes every meter at least every 45 s.

## Things that bit (D1, designer + AEC)

- **AEC3 does not lock on continuous stationary noise.** With pink-ish noise as the stimulus the linear canceller read
  3.6 dB and the whole 20 dB "ERLE" was the noise suppressor; with speech-like bursts the same loop reads 60 dB from the
  canceller alone. `aec-erle.py` ships a burst stimulus; never judge the AEC with steady noise or a tone (NS eats a tone
  by ~20 dB with no reference at all).
- **The first ~3 s after a (re)start are muted** (AEC3's initial suppression): a clean output at −100 dBFS right after a
  deploy is not cancellation. The check reports the first 3 s apart.
- **Ports arrive after nodes.** A block process's nodes show in `pw-dump` a few ms before their ports; linking in that
  window finds nothing. The deployer waits for ports, and treats "node without ports" as pending, not missing.
- **A `pw-record` meter with `node.autoconnect = false` never links itself**: `--target 0` and the deployer links it like
  any wire (so tapping an app stream such as squeezelite works, which WirePlumber cannot do for a capture stream).
- **Block processes live in the dsp container**: `docker compose restart dsp` restarts every AEC/filter block too (and the
  deployer re-links them within a second). Same for the audio role's AES67 jobs, so it is the known pattern.
- **A live param change is not a deploy**: it edits the deployed design in place, and Rollback goes to the last
  *deployed* design, never to a parameter variant.
- **Setting a filter-chain control** is `pw-cli set-param <capture node id> Props '{ params = [ "g0:Gain 1" 0.5 ] }'`;
  the AEC's parameters are module arguments, so changing them restarts that one block (shown in the diff).

## Things that bit (N4, JPEG XS)

- **Nothing is packaged.** Ubuntu 26.04 builds gst-plugins-bad without `svtjpegxs` and ships no gst-plugins-rs RTP
  plugin; the RFC 9134 payloader exists only on gst-plugins-rs main (merged 2026-08-12, in no release or
  `gstreamer-1.28.x` tag). The image builds all three, pinned. gst-plugins-rs main builds against the system 1.28.2
  with Ubuntu's rustc 1.93.
- **`rtpjxsvdepay` derives its output caps from the fmtp fields** (width, height, sampling, depth, ...). A receiver
  that gives `udpsrc` only `encoding-name=JXSV` fails with not-negotiated; the receiver builds full caps from the SDP.
- **`rtpjxsvpay` requires `interlace-mode=progressive`** and `svtjpegxsenc` proxies that upstream, so a source whose
  caps do not state it (our `appsrc`) is refused by the first element after it. The test source's caps say it now.
- **SDPoker treats every `jxsv` sender as ST 2110-22**, which mandates `TP` (ST 2110-21 traffic shaping). owl-node
  sends each frame as a burst (what keeps latency under a frame), so it does not claim `TP`/`SSN` or
  `st2110_21_sender_type`; that is the one IS-05-01 failure (`test_41`) and one BCP-006-01 warning on JPEG XS senders.
- **Unicast needs the controller to aim the sender**: IS-05 has no pull model for RTP, so a click first PATCHes the
  sender (destination = the receiver's `interface_ip` and `destination_port`), then the receiver with the new SDP; one
  unicast stream feeds one receiver, and the previous one is disconnected.
- **No loss counters in gst-plugins-rs depayloaders**, hence `owlrtpstat`.
- **A box-side restart of the H.265 sender hung pinode's HEVC kernel driver** (N3's receiver: `rpi_hevc_dec` stuck in
  `hevc_d_h265_stop` after "Decoding frame took too long"; the process sat in D state until pinode was rebooted). See
  the plan's N4 open problems.

## Things that bit (N3, video)

- **SRT is not an AMWA transport.** The AMWA parameter registers have no SRT entry. owl-node follows Matrox's published
  "NMOS With SRT" (`urn:x-matrox:transport:srt[.rtp|.mp2t]`; senders listen, receivers call; parameters `source_ip`,
  `source_port`, `destination_ip`, `destination_port`, `protocol`, `latency`, `stream_id`). Its endpoints validate
  against Matrox's schemas, but the AMWA testing tool checks IS-05 against AMWA's RTP/MQTT/WebSocket schemas only, so
  it reports every SRT endpoint as failing (see the plan's N3 conformance table). Audio endpoints still pass everything.
- **tsdemux holds every frame** until the next PES starts (video PES have no length): +1 frame (33 ms at 30 fps). That
  is why the default is RTP over SRT, where the depayloader emits each frame on its marker bit. Even with MPEG-TS, the
  parser must be told `alignment=au` (a `capssetter`), or it holds another frame.
- **The Pi 5's HEVC decoder only writes SAND128** (V4L2 `NC12`, 128-byte columns). Upstream GStreamer (1.28.2 and main)
  exposes it only as `DMA_DRM` caps (`NV12:0x0700000000000004`), and `GstVideoDecoder`'s default allocation then
  configures a `GstVideoBufferPool` with caps it cannot parse, so `v4l2slh265dec` fails with every sink, `kmssink`
  included ("Failed to configure the buffer pool"). `gst/gstowlpoolfix.c` sits after the decoder and adds GstVideoMeta
  plus a plain pool to the allocation answer; the decoder stays zero-copy. PyGObject cannot do this from a probe: it
  holds an extra reference, so queries (and buffers, hence the stamp is drawn in numpy before `appsrc`) are read-only.
- **Bit-exact check across hardware**: pinode's `v4l2slh265dec` and PCNode's AMD `vah265dec` produce identical luma
  checksums for the same frames, so the verifier's numbers are about the stream, not the decoder.
- **MediaMTX 1.21 turns on MoQ** (ports 8892/8893) by default; the config turns it off. Browsers in some embedded
  panes block cross-origin fetches, hence the WHEP relay in the controller.
- **Image size** grew from 264 MB to ~920 MB (GStreamer, libav, Mesa/LLVM for VA-API).

## Things that bit (N2, audio)

- **RT in a container without root**: `rtprio` alone is not enough for PipeWire's module-rt; it also needs `nice`
  and `rttime` limits, then its data threads run SCHED_FIFO 83.
- **`sess.ts-direct` + a system-clock driver** gives digital silence; it is only for PHC (PTP) drivers.
- **`rtp-sink` ignores `rtp.payload`** and sends PT 127. The transport file is built from the sender node's real
  `rtp.payload` / `rtp.ts-offset`, so the SDP always matches the wire.
- **IS-05 details the testing tool enforces**: `transporttype` is `urn:x-nmos:transport:rtp` (`.mcast` is only the
  IS-04 sender transport); the `c=` line goes in the media section; a parked receiver (`master_enable` false) shows
  `sender_id: null` in IS-04; `ts-refclk:ptp=IEEE1588-2008:traceable`, not `ptp=traceable`; a system-clock node's SDP
  says `localmac=`. IS-04 v1.3 nodes advertise `_nmos-node._tcp` only when no registry is reachable, and fail over by
  heartbeating the next registry (re-registering only on 404).
- **ST 2022-7 receivers** (nmos-cpp's have two legs) reject a PATCH whose `transport_params` has one entry: the
  controller sizes it from the receiver's `constraints`.
- **Timestamp-direct receive is off by default.** With each receiver in its own process, PipeWire 1.6.2 came up
  silent, bit-exact or garbled at random across restarts, and always silent under pinode's 1.2.7 daemon. Adaptive
  playout is clean, but on the box ← pinode direction it slips one 1 ms packet every 11.55 s (open problem in the plan).
- **Quantum 128.** 1.6's rtp-source asks for 103 frames for a 6 ms link, so the graph fell to 64 and pinode
  glitched; `node.force-quantum = 128` on every AES67 stream fixes it.
- **No `/var/run/ptp4lro` in the container**: Ubuntu's ptp4l AppArmor profile denies clients in another mount
  namespace ("disconnected path"), and linuxptp 3.1 (pinode) has none. SAP says `traceable`; the IS-05 SDP carries
  the grandmaster ID from `owl-node-ptpstat`.
- **The Pi 5's PHC is not under `/sys/class/net/eth0/device/ptp`** (macb); the installer asks `ethtool -T`.
- **A second ptp4l instance** (UDPv4 for Dante gear, `ptp/ptp4l-gm-udpv4.cfg`) must put its sockets under
  `/run/timemaster/ptp4l.*.socket`: the AppArmor profile denies any other extra path.
- **pinode's default sink is its AES67 sender**: it is the only sink there, so WirePlumber picks it. Nothing is
  audible unless a receiver elsewhere is patched to it and linked to a speaker.
