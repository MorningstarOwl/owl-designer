# Contributing to Owl Designer

Issues and pull requests are welcome. Owl Designer runs one household's audio every day, so changes are judged by
whether they keep a house's sound working: tests, measured behaviour and clear failure messages matter more than
features.

## The repository

| Path | What |
|---|---|
| `owl-node/` | the engine: the container image, `install.sh`, the designer pages ([owl-node/README.md](owl-node/README.md)) |
| `owl_designer/` | the Home Assistant add-on (built from `owl-node/`'s controller and pages by CI) |
| `aes67/` | PTP configurations `install.sh` installs |
| `docs/` | [architecture](docs/architecture.md), [lab notes](docs/lab-notes.md), [the improvement plan](docs/improvement-plan.md) |

## Before you open a pull request

```bash
cd owl-node
ruff check .                                                  # bugs and dead code only (no style rules)
tests/with-pipewire.sh python3 -m unittest discover -s tests  # unit tests + live tests on a private PipeWire
tests/nmos-conformance.sh                                     # if you touched the agent or IS-05 (needs Docker)
```

CI runs the same on every pull request (`.github/workflows/owl-node.yml`) and builds the amd64 image.

- **Add a test with a fix.** Reproduce the bug first (a deadlock, a race, a wrong level), then fix it; the tests in
  `owl-node/tests/` show the patterns: pure functions with fake inventories, HTTP handlers on a local port, and live
  tests that skip themselves without a PipeWire daemon.
- **Audio claims need a measurement.** Levels, latencies and timing go through the live tests (meter banks read real
  levels) or a recording; note what you measured, and where, in the pull request.
- **Things that bit go in [docs/lab-notes.md](docs/lab-notes.md)** (what failed, the evidence, what was done).
- **Block types are YAML** (`owl-node/dsp/blocks/`); a new LADSPA plugin is usually one file with a `plugin:` section
  and a `test:` section.
- **Comments say why**, not what; keep them about the code, not about a particular house or date.

## Versions and releases

One version, `X.Y.Z`, for the engine and the add-on. `.github/scripts/check-versions.py` checks that these agree:
`owl-node/owlnode/__init__.py` (`VERSION`), `owl_designer/config.yaml`, the default image in
`owl-node/docker-compose.yml` and `owl-node/install.sh`, and an entry in `owl_designer/CHANGELOG.md`.

To release:

1. Bump the version in those four places; add entries to `owl-node/CHANGELOG.md` and `owl_designer/CHANGELOG.md`.
2. Merge to `main`: CI publishes the add-on image `:X.Y.Z` (Home Assistant offers the update) and the engine images
   `:main`.
3. Tag `vX.Y.Z` on that commit: CI publishes the engine as `ghcr.io/morningstarowl/owl-node:X.Y.Z` and `:latest`,
   which `install.sh` pulls.

Do not reuse a version for different code: Home Assistant and `install.sh` cache by version.

## Security

The designer and the APIs are open on the LAN today (see the README's roadmap). Report a vulnerability privately
through GitHub's security advisories ("Report a vulnerability" on the repository's Security tab), not in a public issue.
