# owl-node

The engine behind Owl Designer: one container image (Ubuntu 26.04, amd64 + arm64) whose roles turn a Linux machine into
an AES67 / NMOS audio node, a video encoder or decoder, and on the hub the designer, the DSP engine and the NMOS
registry. Installing and using it: the [README](../README.md). How it is built: [docs/architecture.md](../docs/architecture.md).
Why it is built that way (measurements, pitfalls): [docs/lab-notes.md](../docs/lab-notes.md).

## Layout

| Path | What |
|---|---|
| `owlnode/` | the Python package: `agent.py` (NMOS IS-04/IS-05), `audio.py` (AES67 processes), `video.py` / `vpipe.py` (video), `controller.py` + `web/` (designer and Routing pages) |
| `owlnode/dsp/` | the engine: inventory, compiler, deployer, controls, MQTT, voice, satellites, SAP discovery, backup, health, injector, sounds |
| `dsp/blocks/*.yaml` | the block palette (one file per block type) |
| `gst/`, `ladspa/` | the small C plugins built into the image |
| `install.sh`, `docker-compose.yml`, `Dockerfile`, `build.sh` | install, run and build |
| `tests/` | unit tests, live PipeWire tests, NMOS conformance |
| `../aes67/`, `ptp/` | PTP configurations |

## Working on it

```bash
cd owl-node
python3 -m unittest discover -s tests                         # unit tests (the live ones skip themselves)
tests/with-pipewire.sh python3 -m unittest discover -s tests  # everything, against a private PipeWire + WirePlumber
ruff check .                                                  # bugs and dead code only (pyproject.toml)
tests/nmos-conformance.sh                                     # AMWA IS-04-01, IS-05-01, IS-05-02 (needs Docker)
```

The live tests need `pipewire`, `wireplumber` and `dbus-launch`; Python needs `numpy`, `PyYAML` and `zeroconf` (Debian
and Ubuntu: `python3-numpy python3-yaml python3-zeroconf`). CI (`.github/workflows/owl-node.yml`) runs all of it on
every pull request, builds the amd64 image, and on `main` and release tags publishes both images to
`ghcr.io/morningstarowl/owl-node`. More in [CONTRIBUTING.md](../CONTRIBUTING.md).

## Building the image

```bash
docker build -t owl-node:$(python3 -m owlnode version) .      # this machine's architecture
./build.sh --amd64-to ssh://user@hub                          # amd64 here, arm64 natively on OWL_ARM64_HOST (ssh)
```

`install.sh` pulls the released image for its version from GHCR when the machine does not have it, and builds it from
the checkout otherwise (or with `--build`). Changes: [CHANGELOG.md](CHANGELOG.md).
