# owl-node changelog

The engine (the `owl-node` image, `install.sh`, the designer pages). The Home Assistant add-on has its own:
[owl_designer/CHANGELOG.md](../owl_designer/CHANGELOG.md). Versions are shared: one `X.Y.Z` for both.

## 0.11.3
- `install.sh` finds the PTP hardware clock on Debian 13 / Raspberry Pi OS trixie: newer ethtool reports it as
  "Hardware timestamp provider index", so a Raspberry Pi 5 node fell back to the system clock and its audio drifted.
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
- CI for the engine: lint, unit + live PipeWire tests, AMWA NMOS conformance (IS-04-01, IS-05-01, IS-05-02), and
  multi-arch images published to `ghcr.io/morningstarowl/owl-node`; `install.sh` pulls the released image instead of
  building it (`--build` to build anyway).
- The Home Assistant add-on is built from the controller's source (no duplicated folder, no committed archive); the
  designer page is split into `designer.html`, `designer.css` and `designer.js`.
- Thread subclasses no longer shadow `threading.Thread._stop` (they crashed on Python <= 3.12 when joined).
- Test signal injector: pink/white noise or a sine into any input, on any machine, at a set level and for a set time.
- Sound library on the hub (WAV/FLAC/Ogg/MP3) and a Sound player block with Play/Stop controls (Panels, HA buttons).
- `pw-dump` output with a trailing change document is read correctly.

## 0.10.0
- Live controls through one long-lived `pw-cli`, without the deployer lock: ~16 ms -> ~0.03 ms per change; 10 ms fades.
- Meter banks: all meter taps in one `pw-record` of up to 64 channels.
- Health issues (`/api/dsp/health`, an HA "Needs attention" sensor) and Prometheus metrics (`/metrics`).
- Deploy history (the last 20) and design export/import in the designer.

## 0.9.0
- Receiver playout latency from the designer (design-wide and per wire), applied on the nodes.
- The hub's backup kept on every node; `install.sh --hub --restore-from <node|file>` restores it on a new hub.

## 0.8.0
- Multichannel AES67 senders: several sources side by side on separate channels (a hub sender has 8 channels).
- SAP discovery of Dante (AES67 mode) and other AES67 streams as designer sources; `install.sh --ptp-udp`.

## 0.7.3
- Fixed: IS-05 activation deadlock; sources mixed into one sender; add-on previews; state files without fsync.
- `--ptp backup`: nodes take over as PTP grandmaster while the hub is down.

## 0.7.2 and earlier
See the git history and the add-on changelog.
