#!/usr/bin/env bash
# Assemble the Home Assistant add-on repository (F1) and optionally publish it on the LAN.
#   ha-addon/build-repo.sh OUT_DIR              repository.yaml + owl_designer/ with a copy of the controller and the
#                                               web pages (HA builds an add-on from its own folder only)
#   ha-addon/build-repo.sh --lan [OUT_DIR]      also commit OUT_DIR as a git repo and push it to pinode, where a
#                                               `git daemon` container serves it as git://<pinode>:9418/owl-addons.git
#                                               (add that URL in Home Assistant: Settings > Add-ons > Add-on store >
#                                               Repositories). Until the public release (plan F4/E6).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$HERE/.."
LAN=0
[ "${1:-}" = --lan ] && { LAN=1; shift; }
OUT="${1:-${TMPDIR:-/tmp}/owl-addons}"
LAN_HOST=${OWL_ADDON_HOST:-user@192.0.2.20}
LAN_DIR=${OWL_ADDON_DIR:-owl-addons}

VERSION=$(sed -n 's/^VERSION = "\(.*\)"/\1/p' "$SRC/owlnode/__init__.py")
grep -q "^version: \"$VERSION\"" "$HERE/owl_designer/config.yaml" || {
  echo "owl_designer/config.yaml version differs from owlnode $VERSION: bump it (and CHANGELOG.md)" >&2; exit 1; }

mkdir -p "$OUT"
# keep a git checkout in OUT if there is one; replace everything else
find "$OUT" -mindepth 1 -maxdepth 1 ! -name .git -exec rm -rf {} +
cp "$HERE/repository.yaml" "$OUT/"
cp -r "$HERE/owl_designer" "$OUT/"
# The controller and its pages ship as ONE archive file (owlnode.tar.gz, unpacked by ADD in the Dockerfile): Home
# Assistant's Supervisor (2026.11) built the add-on from a checkout that had the folder's top-level files but not its
# subfolders, so a COPY of an owlnode/ folder failed there ("/owlnode": not found). Deterministic (sorted, fixed mtime
# and owner) so an unchanged controller gives an unchanged file and no spurious commit.
STAGE=$(mktemp -d)
mkdir -p "$STAGE/owlnode"
cp "$SRC/owlnode/__init__.py" "$SRC/owlnode/__main__.py" "$SRC/owlnode/controller.py" "$STAGE/owlnode/"
cp -r "$SRC/owlnode/web" "$STAGE/owlnode/"
find "$STAGE" -name __pycache__ -prune -exec rm -rf {} +
tar -C "$STAGE" --sort=name --mtime=2026-01-01 --owner=0 --group=0 --numeric-owner -cf - owlnode | gzip -n -9 > "$OUT/owl_designer/owlnode.tar.gz"
rm -rf "$STAGE"
find "$OUT" -name __pycache__ -prune -exec rm -rf {} +
echo "assembled $OUT (owl_designer $VERSION)"

if [ "$LAN" = 1 ]; then
  cd "$OUT"
  [ -d .git ] || { git init -q -b main; }
  git add -A
  git -c user.name="owl-node build" -c user.email="build@owl-node.invalid" commit -q -m "owl_designer $VERSION" || echo "(no changes to commit)"
  ssh -o BatchMode=yes "$LAN_HOST" "mkdir -p ~/$LAN_DIR && [ -d ~/$LAN_DIR/owl-addons.git ] || git init -q --bare -b main ~/$LAN_DIR/owl-addons.git"
  git push -q --force "ssh://$LAN_HOST/~/$LAN_DIR/owl-addons.git" main
  # serve it read-only over git:// (shallow clones work, which the Supervisor uses); idempotent
  ssh -o BatchMode=yes "$LAN_HOST" "docker inspect owl-addons-git >/dev/null 2>&1 || docker run -d --name owl-addons-git --restart unless-stopped -p 9418:9418 -v \$HOME/$LAN_DIR:/repos:ro --entrypoint git alpine/git:2.47.2 daemon --reuseaddr --export-all --base-path=/repos /repos >/dev/null"
  echo "published: git://${LAN_HOST#*@}:9418/owl-addons.git"
fi
