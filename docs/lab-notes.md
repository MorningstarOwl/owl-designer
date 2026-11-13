# Lab notes

What broke, what was measured and why things are the way they are, written down while owl-node was built and run in
one house. Names in these notes: **the box** is the hub (a PC with an Intel PTP-capable NIC), **pinode** a
Raspberry Pi 5 node on a bridged interface, **PCNode** a desktop used for tests and the JPEG XS receiver. References
to "the plan" are to the project's design notes, which are not in this repository. Architecture:
[architecture.md](architecture.md).

## Testing without the hosts (what PCNode ran)

- `amwa/nmos-testing` + an agent on a Docker bridge (`interfaces: {media: eth0}`, no `registry:` so it finds the
  tool's mock registries over mDNS): suites IS-04-01, IS-05-01, IS-05-02 via `POST :5000/api` (N4: also
  `BCP-006-01-01`, with the node's host/port/version only). One agent per tool instance: two agents on one bridge
  both heartbeat the tool's mock registry and fail IS-04 test_05/test_21.
- `rhastie/nmos-cpp` as registry and, with `RUN_NODE=TRUE` and `"senders": ["a"], "receivers": ["a"]`, as the
  reference node to patch to and from.
- A test node with `clock: {phc_if: none}`, `multicast_ttl: 0`, `multicast_loop: true`, `sap_announce: false`:
  real audio through the host's PipeWire, but nothing leaves the machine (N1's hosts auto-subscribe to any `owl-*`
  SAP session). Timestamp-direct playout needs a PHC driver, so system-clock test nodes use the adaptive path.

Today the repository automates this: `owl-node/tests/nmos-conformance.sh` runs IS-04-01, IS-05-01 and IS-05-02
against the agent with `amwa/nmos-testing` (CI does on every change).

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
