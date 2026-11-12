#!/usr/bin/env bash
# Build owl-node for both arches, natively (no QEMU/binfmt needed), and load it where it runs.
#   ./build.sh                          amd64 here + arm64 on pinode's Docker over ssh, both tagged owl-node:<version>
#   ./build.sh --amd64-to ssh://user@192.0.2.10   also copy the amd64 image to the media box (docker save | load)
#   ./build.sh --only amd64|arm64
# The arm64 build runs on the Pi's own daemon (DOCKER_HOST=ssh://...), so the image is already there afterwards.
set -euo pipefail
cd "$(dirname "$0")"
V=$(sed -n 's/^VERSION = "\(.*\)"/\1/p' owlnode/__init__.py)
ARM=${OWL_ARM64_HOST:-ssh://user@192.0.2.20}
ONLY="" TO=()
while [ $# -gt 0 ]; do case "$1" in
  --only) ONLY="$2"; shift 2;; --amd64-to) TO+=("$2"); shift 2;; --arm64-host) ARM="$2"; shift 2;;
  *) echo "unknown argument $1" >&2; exit 2;; esac; done
if [ "$ONLY" != arm64 ]; then
  docker build --platform linux/amd64 -t "owl-node:$V" .
  for h in ${TO[@]+"${TO[@]}"}; do echo "loading owl-node:$V into $h"; docker save "owl-node:$V" | ssh "${h#ssh://}" docker load; done
fi
if [ "$ONLY" != amd64 ]; then
  DOCKER_HOST="$ARM" docker build --platform linux/arm64 --build-arg JOBS="${OWL_ARM64_JOBS:-2}" -t "owl-node:$V" .
fi
for h in local ${ONLY:+} "$ARM"; do
  if [ "$h" = local ]; then [ "$ONLY" = arm64 ] && continue; docker image inspect "owl-node:$V" --format "local: {{.Architecture}} {{.Size}}"
  else [ "$ONLY" = amd64 ] && continue; DOCKER_HOST="$h" docker image inspect "owl-node:$V" --format "$h: {{.Architecture}} {{.Size}}"; fi
done
