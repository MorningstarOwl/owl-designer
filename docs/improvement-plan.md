# Owl Designer: review and improvement plan

Review of the repository at owl-designer 0.7.2 (2026-11-13): the NMOS agent, IS-05, the AES67 audio supervisor, the
DSP compiler/deployer, the APIs, the Wyoming voice satellite, video, the installer, CI and the Home Assistant add-on.
Findings come from reading the code; nothing was built or run (the repository has no tests to run).

Status: **section 1 (security) is deferred**; **section 2 (bugs) is fixed in 0.7.3** (see the notes under each item).

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
2. SAP discovery of third-party AES67 / Dante (AES67 mode) / Ravenna streams as designer sources; UDPv4 PTP as the
   default wherever the host is not on a bridge.
3. ST 2022-7 dual-network redundancy (only the first SDP leg is parsed today).
4. Per-flow latency and aligned multi-room playout (depends on bug 4).
5. NMOS IS-07 (events for panels/buttons), IS-08, IS-11, IS-12; an IPMX roadmap for video.
6. Video, NVX-style: HDMI audio de-embed/embed, multiview, a scalable video routing model.
7. The hub is a single point of failure (registry, engine, PTP grandmaster): a standby hub, or documented degraded
   behaviour.

## 4. Performance and reliability

1. Every live control change holds the deployer lock, deep-copies the design, runs `pw-dump` + `pw-cli` and writes
   the design file; fades repeat this every 40 ms, and the 2 s reconcile holds the same lock during HTTP calls. Use a
   long-lived PipeWire connection, cached node ids and a separate lock for live controls.
2. One `pw-record` process + numpy thread per meter tap; one multi-channel meter process would scale.
3. 40 ms fade steps on plugins without smoothing can zipper.
4. Metrics (Prometheus) and HA alerts for PTP loss, packet loss and restarts; more than one rollback generation;
   design export/import.

## 5. Engineering hygiene

1. No tests. The compiler, SDP parser, IS-05 state machine and block templates are pure and easy to unit-test; run
   AMWA nmos-testing in CI.
2. CI builds only the add-on; nothing builds, lints or publishes the `owl-node` image (prebuilt multi-arch GHCR images
   would remove the 20-40 min Pi build).
3. The add-on is duplicated (`owl_designer/` and `owl-node/ha-addon/owl_designer/`) plus a committed
   `owlnode.tar.gz`; CI only rebuilds on `owl_designer/**`, so a controller change alone ships nothing. With a
   prebuilt image, CI can build from source directly.
4. `owl-node/README.md` links files that are not in the repo (`scripts/checks/*`, `docs/owl-designer-portable-plan.md`,
   `dsp/designs/`, `upgrade-hub.sh`, `upgrade-satellite.sh`) and mixes user docs with a lab notebook; same for the long
   dated comments in `voice.py`.
5. Commit messages are all "owl-designer 0.7.x"; add an engine CHANGELOG, CONTRIBUTING and issue templates.
6. ~10 hand-rolled `BaseHTTPRequestHandler` routers and a custom WebSocket; a 118 KB single-file `designer.html`. A
   small framework (aiohttp/Starlette) would centralise validation, auth and WebSockets; a UI build step would let the
   designer be split up.
