#!/usr/bin/env bash
# AMWA NMOS conformance of the agent: IS-04-01 (node API + registration through the testing tool's mock registry,
# found over mDNS), IS-05-01 (connection API), IS-05-02 (IS-04/IS-05 interaction), with amwa/nmos-testing in Docker on
# the host network. An audio node (one sender, one receiver), no PipeWire needed: the agent alone answers the APIs.
#   tests/nmos-conformance.sh                 interface of the default route
#   IFACE=eth0 tests/nmos-conformance.sh
# Exit status 1 when any test fails. Extra `docker run` arguments (a proxy, a CA) in NMOS_TESTING_DOCKER_ARGS.
# Needs python3 with PyYAML and zeroconf, curl, docker.
set -euo pipefail
cd "$(dirname "$0")/.."
IFACE=${IFACE:-$(ip route show default | awk '{for (i = 1; i < NF; i++) if ($i == "dev") {print $(i + 1); exit}}')}
IP=$(ip -4 -o addr show dev "$IFACE" | awk '{print $4}' | cut -d/ -f1 | head -1)
[ -n "$IP" ] || { echo "no IPv4 address on $IFACE" >&2; exit 2; }
IMAGE=${NMOS_TESTING_IMAGE:-amwa/nmos-testing:latest}
WORK=$(mktemp -d)
AGENT=""
cleanup() {
  [ -n "$AGENT" ] && kill "$AGENT" 2>/dev/null || true
  docker rm -f owl-nmos-testing >/dev/null 2>&1 || true
  rm -rf -- "$WORK"
}
trap cleanup EXIT
cat > "$WORK/node.yaml" <<EOF
name: ci-node
label: CI node
interfaces: {media: $IFACE}
clock: {phc_if: none}
sap_announce: false
multicast_ttl: 0
audio:
  senders: [{id: tx1, channels: 8}]
  receivers: [{id: rx1}]
EOF
mkdir -p "$WORK/state" "$WORK/run"
echo "agent on $IFACE ($IP):8360; testing tool $IMAGE"
OWL_NODE_CONF="$WORK/node.yaml" OWL_NODE_STATE="$WORK/state" OWL_NODE_RUN="$WORK/run" python3 -m owlnode agent > "$WORK/agent.log" 2>&1 &
AGENT=$!
# shellcheck disable=SC2086
docker run -d --name owl-nmos-testing --network host ${NMOS_TESTING_DOCKER_ARGS:-} "$IMAGE" > /dev/null
for _ in $(seq 150); do curl -s --noproxy '*' -o /dev/null "http://localhost:5000" && break; sleep 2; done
curl -s --noproxy '*' -o /dev/null "http://localhost:5000" || { docker logs owl-nmos-testing | tail -20; exit 1; }
curl -s --noproxy '*' -o /dev/null "http://$IP:8360/x-nmos/node/v1.3/self" || { cat "$WORK/agent.log"; exit 1; }

run() {   # suite, request body -> results JSON in $WORK/<suite>.json
  curl -s --noproxy '*' -X POST "http://localhost:5000/api" -H 'Content-Type: application/json' -d "$2" > "$WORK/$1.json"
}
run IS-04-01 "{\"suite\": \"IS-04-01\", \"host\": [\"$IP\"], \"port\": [8360], \"version\": [\"v1.3\"]}"
run IS-05-01 "{\"suite\": \"IS-05-01\", \"host\": [\"$IP\"], \"port\": [8360], \"version\": [\"v1.1\"]}"
run IS-05-02 "{\"suite\": \"IS-05-02\", \"host\": [\"$IP\", \"$IP\"], \"port\": [8360, 8360], \"version\": [\"v1.3\", \"v1.1\"]}"
python3 - "$WORK" <<'PY'
import collections, json, sys
failed = 0
for suite in ("IS-04-01", "IS-05-01", "IS-05-02"):
    try:
        r = json.load(open(f"{sys.argv[1]}/{suite}.json"))
        res = r["results"]
    except Exception as e:
        print(f"{suite}: no results ({e})")
        failed += 1
        continue
    c = collections.Counter(x.get("state") for x in res)
    print(f"{suite}: " + ", ".join(f"{n} {k}" for k, n in sorted(c.items())))
    for x in res:
        if x.get("state") in ("Fail", "Warning"):
            print(f"  {x['state']}: {x.get('name')} - {(x.get('detail') or '')[:240]}")
        if x.get("state") == "Fail":
            failed += 1
sys.exit(1 if failed else 0)
PY
