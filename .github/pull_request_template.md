## What and why

## How it was tested
<!-- Tests added/changed; what you measured on real hardware (levels, latency, timing), and where. -->

## Checklist
- [ ] `ruff check .` and `tests/with-pipewire.sh python3 -m unittest discover -s tests` pass (in `owl-node/`)
- [ ] NMOS conformance (`tests/nmos-conformance.sh`) if the agent or IS-05 changed
- [ ] Docs updated (README, docs/architecture.md, docs/lab-notes.md for things that bit)
- [ ] Version bump and CHANGELOG entries if this is a release (see CONTRIBUTING.md)
