# Owl Designer: review and improvement plan

Review of the repository at owl-designer 0.7.2 (2026-11-13): the NMOS agent, IS-05, the AES67 audio supervisor, the
DSP compiler/deployer, the APIs, the Wyoming voice satellite, video, the installer, CI and the Home Assistant add-on.
Findings come from reading the code; nothing was built or run (the repository has no tests to run).

Status: **section 1 (security) is deferred**; **section 2 (bugs) is fixed in 0.7.3**; **section 3 items 1-2 are done in
0.8.0, items 4 and 7 in 0.9.0; items 3, 5 and 6 are designed below, not built**; **section 4 is done in 0.10.0**; **section 5 is done
(items 1-5) or deliberately deferred (item 6)** (see the notes under each item).

## 1. Security (deferred)

1. **Unauthenticated remote command execution on every dsp satellite.** `PUT :8362/api/dsp/share` takes `procs`, and
   a proc whose config starts with `#!owl-cmd` is executed as argv (`owl-node/owlnode/dsp/deploy.py` `make_proc`, via
   `satellite.py` `apply`). The container holds the host's PipeWire socket (every microphone). Share block ids are also
   unvalidated and become file names (`f"{bid}.conf"`).
2. **PipeWire config injection on the hub.** `${label}` and string params (App Input `name`) are substituted unescaped
   into quoted SPA-JSON (`owl-node/owlnode/dsp/blocks.py` `render`, the `plugin:` template). A label containing `"`
   breaks out and can load arbitrary modules. Escape every substituted value (`json.dumps`, as `audio.py`'s `esc()`
   does).
3. **Cross-site attacks from any web page someone in the house opens.** No Origin / Content-Type checks: a
   `text/plain` cross-origin POST can deploy, clear or roll back; a cross-site WebSocket can set controls
   (`/api/dsp/ws`) or **stream a microphone** (`/api/dsp/probe`). The agent sends `Access-Control-Allow-Origin: *` on
   IS-05.
4. **Wyoming (TCP 10700) accepts any LAN client** and lets it take over the link; with the wake word on, the clean mic
   streams continuously. Restrict it to Home Assistant's address.
5. **`PUT /api/dsp/pair`** lets any host re-point a node at another registry.
6. Fix direction: a hub-generated API token (pushed to nodes at pairing) + Origin checks + an `install.sh` firewall
   template; later NMOS IS-10 / BCP-003. Also cap request bodies and WebSocket frame sizes (`ws.py` `read_frame`
   trusts a 64-bit length).

## 2. Bugs

1. **IS-05 activation can deadlock.** An endpoint activates while holding its own lock, and `Agent.persist()` then
   takes every other endpoint's lock: two concurrent activations (two PATCHes, or scheduled activations of several
   receivers at the same `requested_time`) wedge each other for good.
   *Fixed:* one lock per node, shared by its endpoints (`tests/test_is05_deadlock.py` reproduced the hang first).
2. **Cross-host wires share one AES67 sender per host.** The compiler always takes the first sender (`compiler.py`
   `sat_sender` / `hub_tx`), so two different hub outputs going to two rooms are summed (it only warns).
   *Fixed:* one sender per distinct source from the host's pool, stable across deploys; sharing (mixing) only when the
   pool or the far end's receivers run out, with a warning per wire (`tests/test_compiler_senders.py`). A default
   install still has one sender per machine: add senders (`install.sh --tx`) to route separate streams.
3. **Add-on previews never show as live.** MediaMTX's API listens on `127.0.0.1:9997`, but the add-on queries
   `<hub>:9997` (`owl_designer/run.py`).
   *Fixed:* the engine serves `/api/dsp/previews`; the add-on's controller asks it (`tests/test_addon_previews.py`).
4. **"Sample-accurate with PTP" claim vs. the adaptive playout default**, which by the project's own notes slips a
   packet every ~11.5 s. Fix timestamp-direct playout or soften the claim.
   *Done (docs):* the README now describes adaptive playout and lists aligned playout on the roadmap; making
   timestamp-direct playout reliable needs work on real hardware.
5. **State files are not fsync'd.** `config.atomic_write` renames without `fsync`; a power cut (Pi, SD card) can leave
   `state.json` / `dsp-design.json` empty, and `read_json` silently falls back to the default (the design is lost).
   *Fixed:* file and directory fsync, temp file removed on failure (`tests/test_atomic_write.py`).
6. **No PTP grandmaster failover.** Followers are `slaveOnly`, so when the hub is down every node loses its clock.
   *Fixed:* `aes67/ptp4l-backup.cfg` (`install.sh --ptp backup`, a fresh node's default): follows the hub, takes over
   ~6 s after it goes away, hands back when it returns (checked with two ptp4l instances on a veth pair). Existing
   nodes keep their config until re-installed with `--ptp backup`.
7. Minor: `last_mtime` in `audio.py` is computed but unused; `state.json` is parsed every 250 ms.
   *Fixed:* re-read only when the file changes (plus every 5 s as a backstop).

## 3. Dante / NVX parity

1. Per-wire flow allocation and channel-level routing (sender pool and/or multichannel flows + IS-08 channel mapping)
   instead of fixed `--tx`/`--rx` sets.
   *Done (0.8.0):* multichannel flows. A sender of up to 8 channels carries several sources side by side; the compiler
   gives each distinct source a channel slot (stable across deploys, packing into senders the destination already
   receives) and the receiving side links only those channels. A hub's default sender has 8 channels. IS-08 to expose
   the mapping to other NMOS controllers is still open (item 5).
2. SAP discovery of third-party AES67 / Dante (AES67 mode) / Ravenna streams as designer sources; UDPv4 PTP as the
   default wherever the host is not on a bridge.
   *Done (0.8.0):* the hub's engine listens for SAP (`owlnode/dsp/sap.py`); playable streams that are not owl-nodes are
   source blocks (channel pairs of multichannel flows wireable on their own); wiring one patches a receiver with the
   announced SDP; a re-announcement with a new SDP is followed. `install.sh --hub --ptp-udp` installs the server-only
   UDPv4 PTP instance for Dante gear (opt-in rather than default: it needs the PHC on the media interface, and the L2
   nodes are unaffected). Not yet tried against a real Dante device.
3. ST 2022-7 dual-network redundancy (only the first SDP leg is parsed today).
   *Designed, not built:* it needs two network ports per machine and two switches, which a house rarely has, and
   nothing here could test it. PipeWire cannot merge two copies of a stream, so the plan is a small C relay per
   machine (like `owlrtpstat`): on send, copy each RTP packet of the sender's leg A out of the second NIC to group B;
   on receive, join both legs, keep the first copy of each sequence number in a reorder window of the path difference,
   and feed the result to `rtp-source` on loopback. The agent then advertises two legs (`transport_params` x 2,
   `a=group:DUP` SDPs; the controller already sizes legs from the receiver's constraints) and `rtpstat` reports loss
   per leg. Worth building when a household or installer asks for it with the hardware to test on.
4. Per-flow latency and aligned multi-room playout (depends on bug 4).
   *Done (0.9.0):* a design-wide **Network latency** and a per-wire override in the designer; the compiler gives each
   owl receiver one latency (the largest of its wires'), the deployer sets it on the node through an owl extension
   (`PATCH /x-owl/v1/receivers/<id> {latency_ms}`, kept in `state.json`, used by the audio role's `rtp-source`) and
   clears it when a design drops it. Equal latencies line rooms up to within the adaptive playout's slack;
   sample-exact alignment still needs timestamp-locked playout (bug 4), on real hardware.
5. NMOS IS-07 (events for panels/buttons), IS-08, IS-11, IS-12; an IPMX roadmap for video.
   *Designed, not built:* each needs a consumer to be worth its weight, and none is in this system yet; Home Assistant
   already gets every control and meter over MQTT. In order of value: **IS-08** on each agent, mapping its sources'
   channels to its senders' channels, would let third-party NMOS controllers see and change the channel slots item 1
   allocates (the hub compiler stays the main controller; the agent would apply IS-08 activations as PipeWire links,
   which today only the hub's share mechanism makes). **IS-07** (REST + WebSocket on the hub's dsp role) would publish
   named controls and meters as NMOS event sources for hardware panels and tally. **IS-11** matters for video sinks
   with EDID (NVX-style displays), **IS-12** for third-party control surfaces. Each should be developed against the AMWA
   NMOS testing tool, which the README's "Testing it without the hosts" already uses for IS-04/05.
6. Video, NVX-style: HDMI audio de-embed/embed, multiview, a scalable video routing model.
   *Designed, not built:* everything here needs capture hardware and decoders to develop against. **HDMI audio
   de-embed** likely works already: most USB HDMI capture sticks are also a USB sound card, which appears as a device
   block on the canvas like any other (to confirm with the household's stick); a convenience "audio follows video"
   pairing in the designer would come next. **Embedding** audio into a video sender means muxing an audio branch into
   `vpipe`'s MPEG-TS/RTP output. **Multiview** is a `compositor` in a decoder node's `vpipe` with N SRT callers, so a
   video receiver becomes N receivers plus a layout. A scalable routing model (several streams per decoder, scaling,
   switching without black frames) follows from multiview.
7. The hub is a single point of failure (registry, engine, PTP grandmaster): a standby hub, or documented degraded
   behaviour.
   *Done (0.9.0):* the hub pushes its backup (design, previous design, draft, nodes, wake-word switch, MQTT settings
   without the password) to every satellite within ~30 s of a change; `install.sh --hub --restore-from <node | file>`
   restores it on a replacement hub or a promoted node, moves references to the old hub's devices to the new name,
   re-pairs every node and re-applies the design. The README's "If the hub fails" says what keeps running meanwhile
   (node-to-node streams, and with `--ptp backup` the clock). A hot standby (two hubs, automatic takeover) is left out
   on purpose: two engines must never deploy at once, and the manual swap covers a house.

## 4. Performance and reliability

1. Every live control change holds the deployer lock, deep-copies the design, runs `pw-dump` + `pw-cli` and writes
   the design file; fades repeat this every 40 ms, and the 2 s reconcile holds the same lock during HTTP calls. Use a
   long-lived PipeWire connection, cached node ids and a separate lock for live controls.
   *Done (0.10.0):* one long-lived `pw-cli` fed on stdin (`pw.PwCtl`), node ids from the graph dumps reconcile makes
   anyway, no deployer lock (per-block lock + copy-on-write design under a short lock, a deploy counter so a racing
   change never overwrites a new design), the design/share file written at most once a second. Measured on a real
   PipeWire: 16 ms per change (idle graph) -> 0.03 ms. The controls listing no longer rebuilds per fader step.
2. One `pw-record` process + numpy thread per meter tap; one multi-channel meter process would scale.
   *Done (0.10.0):* meter banks: a tap is a channel range of one `pw-record` of up to 64 channels; taps coming and
   going only add or remove links. Tested on PipeWire + WirePlumber (a tone in a tap's channels, silence next to it).
3. 40 ms fade steps on plugins without smoothing can zipper.
   *Done (0.10.0):* 10 ms steps (affordable now that a step is a line to a pipe): a 12 dB fade over 0.5 s moves in
   0.24 dB steps instead of 1 dB. Ramps inside the plugins would remove the steps entirely; not needed so far.
4. Metrics (Prometheus) and HA alerts for PTP loss, packet loss and restarts; more than one rollback generation;
   design export/import.
   *Done (0.10.0):* `health.py` (PTP, loss in the last minute, silent receivers, restarts in the last 10 min, nodes
   not answering, design/registry errors) -> `GET /api/dsp/health`, a Home Assistant "Needs attention" problem sensor
   with the issues as attributes, and `GET /metrics` (Prometheus). The last 20 deployed designs are kept
   (`dsp-history/`) and the designer's History opens one on the canvas; Export/Import save and open design files.

## 5. Engineering hygiene

1. No tests. The compiler, SDP parser, IS-05 state machine and block templates are pure and easy to unit-test; run
   AMWA nmos-testing in CI.
   *Done:* 85 tests in `owl-node/tests/` (unit, HTTP handlers, and live ones against a private PipeWire + WirePlumber
   started by `tests/with-pipewire.sh`), a hygiene check that caught a real bug (six Thread subclasses shadowed
   `Thread._stop`, crashing on Python <= 3.12), and `tests/nmos-conformance.sh`: IS-04-01 (50 pass), IS-05-01 (62)
   and IS-05-02 (56), no failures, with `amwa/nmos-testing`. `ruff` for bugs and dead code only.
2. CI builds only the add-on; nothing builds, lints or publishes the `owl-node` image (prebuilt multi-arch GHCR images
   would remove the 20-40 min Pi build).
   *Done:* `.github/workflows/owl-node.yml`: versions agree, lint, tests with live PipeWire, NMOS conformance, the
   image on native amd64 + arm64 runners (pull requests: amd64 build only), published as `:main` / `:sha-*` from
   `main` and `:X.Y.Z` / `:latest` from tags `vX.Y.Z`. `install.sh` pulls the released image (`--build` to build).
3. The add-on is duplicated (`owl_designer/` and `owl-node/ha-addon/owl_designer/`) plus a committed
   `owlnode.tar.gz`; CI only rebuilds on `owl_designer/**`, so a controller change alone ships nothing. With a
   prebuilt image, CI can build from source directly.
   *Done:* `owl_designer/Dockerfile` builds from the repository root, copying the controller and pages from
   `owl-node/`; the duplicate folder, `build-repo.sh` and the archive are gone; the add-on workflow also rebuilds on
   controller/page changes. `.github/scripts/check-versions.py` keeps the one version in step.
4. `owl-node/README.md` links files that are not in the repo (`scripts/checks/*`, `docs/owl-designer-portable-plan.md`,
   `dsp/designs/`, `upgrade-hub.sh`, `upgrade-satellite.sh`) and mixes user docs with a lab notebook; same for the long
   dated comments in `voice.py`.
   *Done:* split into `docs/architecture.md` (reference, broken links removed, new modules listed),
   `docs/lab-notes.md` (the "things that bit", with a key to the house's machine names) and a short developer
   `owl-node/README.md`; `voice.py`'s comments keep the reasons, not the dates.
5. Commit messages are all "owl-designer 0.7.x"; add an engine CHANGELOG, CONTRIBUTING and issue templates.
   *Done:* `owl-node/CHANGELOG.md`, `CONTRIBUTING.md` (tests, measurements, the release process), bug/feature issue
   forms, a PR template, private security reports. Commits on this branch describe what and why.
6. ~10 hand-rolled `BaseHTTPRequestHandler` routers and a custom WebSocket; a 118 KB single-file `designer.html`. A
   small framework (aiohttp/Starlette) would centralise validation, auth and WebSockets; a UI build step would let the
   designer be split up.
   *Partly done:* the designer page is now `designer.html` (structure), `designer.css` and `designer.js`, still with
   no build step. *Deferred on purpose:* moving the servers to aiohttp/Starlette rewrites every endpoint and the
   threading model at once, adds dependencies to an image that has none beyond Debian packages, and buys little until
   the security work (section 1: a token, Origin checks) needs a middleware layer; do them together then. Likewise a UI
   build step (bundler, components in modules) is worth it when the designer is next reworked, not as a move on its
   own: the page works, is tested headless, and has no toolchain to maintain.
