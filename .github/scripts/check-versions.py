#!/usr/bin/env python3
"""One version everywhere: owl-node/owlnode/__init__.py VERSION = the add-on's config.yaml version = the default image
of docker-compose.yml and install.sh, plain X.Y.Z, with an entry in the add-on's CHANGELOG.md. Exit 1 otherwise."""
import os, re, sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")


def read(rel):
    with open(os.path.join(ROOT, rel)) as f:
        return f.read()


def one(rel, pattern):
    m = re.search(pattern, read(rel), re.M)
    return m.group(1) if m else None


v = one("owl-node/owlnode/__init__.py", r'^VERSION = "([^"]+)"')
found = {
    "owl_designer/config.yaml": one("owl_designer/config.yaml", r'^version: "?([^"\s]+)"?'),
    "owl-node/docker-compose.yml": one("owl-node/docker-compose.yml", r"OWL_NODE_IMAGE:-owl-node:([^}\s]+)\}"),
    "owl-node/install.sh": one("owl-node/install.sh", r"\bIMAGE=owl-node:(\S+)"),
}
errors = []
if not v or not re.fullmatch(r"\d+\.\d+\.\d+", v):
    errors.append(f"owl-node/owlnode/__init__.py: VERSION {v!r} is not X.Y.Z")
for rel, x in found.items():
    if x != v:
        errors.append(f"{rel}: {x!r}, but owlnode VERSION is {v!r}")
if not re.search(rf"^## {re.escape(v or '?')}\b", read("owl_designer/CHANGELOG.md"), re.M):
    errors.append(f"owl_designer/CHANGELOG.md has no '## {v}' entry")
for e in errors:
    print(f"::error::{e}")
print(f"version {v}: " + ("consistent" if not errors else f"{len(errors)} problem(s)"))
sys.exit(1 if errors else 0)
