#!/usr/bin/env bash
# owl-node installer: one host becomes an NMOS/AES67 node (and, with the hub role, the registry, the designer + Routing
# page and the D1 dsp engine).
# Run ON the target host as the desktop/audio user, from a copy of satellite/ (it reads ../aes67/ptp4l-*.cfg when it
# installs PTP). sudo is used for /etc, /var/lib, /run and systemd. See owl-node/README.md.
#
# Quick start (E2: nothing to know about the network; everything below is detected):
#   node:   ./install.sh            (= --node: roles agent,audio,dsp; then add this machine's IP in the hub's designer,
#                                    Nodes -> Add; the hub pairs it, no --registry needed)
#   hub:    ./install.sh --hub      (roles agent,audio,hub; the designer opens on http://<this IP>:8361)
#   Detected when not given: --media-if (the wired interface of the default route; a bridge like br0 is fine),
#   --phc-if (that interface or the bridge member with a PTP hardware clock; none = system clock, see below),
#   --ptp (keep an existing owl-ptp4l.service, else grandmaster on a hub / backup on a node; none without a PHC),
#   one sender "<label>" and one receiver "<label> RX 1", the image (built here from this checkout when it is not
#   loaded: minutes on a PC, 20-40 min on a Pi). Interactive runs ask whether to start at boot (--enable / --no-enable,
#   --yes answers every question with its default).
#   Without a PTP hardware clock the node still lists its devices in the designer, but its AES67 audio runs on the
#   system clock and drifts against the hub's (plan E4).
#
#   hub:    ./install.sh --name livingroom --label "Living Room" --area "Living Room" --roles agent,audio,hub \
#             --media-if eno1 --phc-if eno1 --tx "Living Room" --rx "Living Room RX 1" --rx "Living Room RX 2"
#   pinode:  ./install.sh --name pinode --label pinode --area Office --roles agent,audio,dsp \
#             --media-if br0 --phc-if eth0 --registry http://192.0.2.10:8010 --tx pinode --rx "pinode RX 1"
#   roles: agent (NMOS), audio (AES67), hub (registry, designer, dsp engine), dsp (D3: a satellite runs processing
#   blocks the hub's designer places on it; the hub finds it on :8362), video-enc, video-dec
#
#   --tx 'LABEL[@GROUP][|channels=N]'   a sender (repeatable); group defaults to 239.69.<n>.<last octet of the host
#                          IP); channels 1-8 (default 2; 8 for a hub's default sender): the designer puts several
#                          sources side by side in one sender (channels 1-2, 3-4, ...), so one 8-channel sender carries
#                          four stereo streams to other machines
#   --rx LABEL[>SINK]      a stereo receiver (repeatable); >SINK links it to a host sink (default: linked to nothing)
#   --vtx 'LABEL[|k=v,...]'  N3 video sender (repeatable; adds role video-enc): source=testsrc|v4l2, device=/dev/videoN,
#                          format=yuy2|mjpeg, width=, height=, fps=, codec=h265|h264, encap=rtp|ts, bitrate=<kbps>,
#                          port=<SRT listen port, default 9001+n>, latency=<ms>, preview=no
#                          N4 JPEG XS (codec=jxs, RTP RFC 9134 over UDP): bpp=<codestream bits/pixel, 3 = 4:1 on
#                          8-bit 4:2:0>, jxs_format=i420|i422_10, source_of=vtxN (the same frames as that sender, in
#                          its process, sharing its preview), multicast=yes (default unicast), port=<default 5010+2n>,
#                          mtu=<1400>, threads=<SVT threads, 0 = auto>
#   --vrx 'LABEL[|k=v,...]'  N3 video receiver (repeatable; adds role video-dec): display=none|kms, latency=<ms>,
#                          decoder=<element>, kms_driver=, connector_id=, codecs=h265+h264
#                          N4: codecs=jxs makes it a JPEG XS RTP receiver (port=<default 5010+2n>, echo=ip:port)
#   --video-dev PATH       extra device for the video containers (repeatable): a capture stick /dev/videoN; the Pi 5's
#                          HEVC decoder (rpi-hevc-dec + /dev/media*) is found automatically
#   --ptp keep|grandmaster|backup|follower|none   keep (default) = use the host's existing owl-ptp4l.service (N1's);
#                          grandmaster/backup/follower = install it like N1 does (L2 transport, hardware timestamps).
#                          backup (a fresh node's default) follows the hub but takes over as grandmaster when the hub
#                          is gone, so the nodes stay aligned; follower never leads (aes67/README.md)
#   --ptp-udp / --no-ptp-udp   hub: also serve PTP over UDPv4 (a second ptp4l, server-only, same NIC and clock:
#                          ptp/ptp4l-gm-udpv4.cfg), so Dante devices in AES67 mode and other AES67 gear, which expect
#                          UDPv4, follow the house clock. Needs the PHC on the media interface itself (not a bridge
#                          port). Kept on a re-run once installed; --no-ptp-udp removes it
#   --restore-from NODE|FILE   hub: after starting, restore the backup of the previous hub, which every paired node
#                          keeps (NODE = its address, e.g. 192.0.2.20 or 192.0.2.20:8362), or from a file (e.g. on a
#                          node being promoted to hub: /var/lib/owl-node/hub-backup.json). Designs, panels, presets and
#                          nodes come back; the nodes are paired with this hub (see the README: Replacing the hub)
#   --registry none        a node: drop the registry of an earlier install; the hub pairs it from the designer instead
#   --image TAG            owl-node image already loaded on this host (default owl-node:0.11.0)
#                          When it is not on this host, the released image is pulled from ghcr.io/morningstarowl/owl-node
#                          (amd64 and arm64; OWL_NODE_REGISTRY to use another), else it is built here from this checkout
#   --build / --no-build   always build the image here (local changes) / never build (fail if it is not loaded)
#   --mqtt URL             hub only (D2): Home Assistant over MQTT, mqtt://user:pass@host:1883 -> OWL_MQTT_URL in .env
#                          (the dsp role publishes HA discovery for every control). Or write
#                          /var/lib/owl-node/mqtt.json {host, port, username, password} instead; neither = bridge off
#   --replace-n1           stop N1's hand-run owl-aes67 user unit (it would duplicate the PTP driver and senders)
#   --enable               start at boot: containers restart: unless-stopped; owl-ptp4l, owl-node-ptpstat and
#                          owl-node.service (compose up -d once the user's PipeWire socket exists) enabled.
#                          Without it nothing comes back after a reboot (ask the household first).
#   --no-start             write files only        --dry-run   show what would change     --remove   undo everything
# Never touches the host's PipeWire/WirePlumber daemons or their config.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
NAME="" LABEL="" AREA="" ROLES="" MEDIA_IF="" PHC_IF="" REGISTRY="" PTP="" IMAGE=owl-node:0.11.0 MQTT_URL=""
TX=() RX=() VTX=() VRX=() VDEV=() DRY=0 REMOVE=0 ENABLE="" START=1 REPLACE_N1=0 YES=0 MODE="" BUILD=auto PTP_UDP="" RESTORE_FROM=""
while [ $# -gt 0 ]; do
  case "$1" in
    --name) NAME="$2"; shift 2;; --label) LABEL="$2"; shift 2;; --area) AREA="$2"; shift 2;;
    --roles) ROLES="$2"; shift 2;; --media-if) MEDIA_IF="$2"; shift 2;; --phc-if) PHC_IF="$2"; shift 2;;
    --ptp-udp) PTP_UDP=1; shift;; --no-ptp-udp) PTP_UDP=0; shift;; --restore-from) RESTORE_FROM="$2"; shift 2;;
    --registry) REGISTRY="$2"; shift 2;; --ptp) PTP="$2"; shift 2;; --image) IMAGE="$2"; shift 2;; --mqtt) MQTT_URL="$2"; shift 2;;
    --tx) TX+=("$2"); shift 2;; --rx) RX+=("$2"); shift 2;;
    --vtx) VTX+=("$2"); shift 2;; --vrx) VRX+=("$2"); shift 2;; --video-dev) VDEV+=("$2"); shift 2;;
    --replace-n1) REPLACE_N1=1; shift;; --enable) ENABLE=1; shift;; --no-enable) ENABLE=0; shift;; --no-start) START=0; shift;;
    --dry-run) DRY=1; shift;; --remove) REMOVE=1; shift;;
    --node) MODE=node; shift;; --hub) MODE=hub; shift;; --yes|-y) YES=1; shift;; --no-build) BUILD=no; shift;; --build) BUILD=always; shift;;
    -h|--help) awk 'NR>1 && !/^#/ {exit} NR>1' "$0"; exit 0;;
    *) echo "unknown argument: $1" >&2; exit 2;;
  esac
done

run() { if [ "$DRY" = 1 ]; then echo "+ $*"; else "$@"; fi; }
say() { echo "owl-node: $*"; }
ask() { # ask "question" default(y|n) [answer-without-a-terminal] -> 0 for yes
  local d="$2" a
  if [ "$YES" = 1 ]; then [ "$d" = y ]; return; fi
  if [ ! -t 0 ]; then [ "${3:-$d}" = y ]; return; fi
  read -r -p "$1 [$([ "$d" = y ] && echo Y/n || echo y/N)] " a || a=""
  a="${a:-$d}"; [[ "$a" =~ ^[Yy] ]]
}
# ---------------------------------------------------------------- E2: roles from --node / --hub
# a re-run over an existing install with no role/stream flags keeps its roles and its audio/video sections verbatim
KEEP_STREAMS=0
if [ -r /etc/owl-node/node.yaml ] && [ "$REMOVE" = 0 ] && [ ${#TX[@]} = 0 ] && [ ${#RX[@]} = 0 ] && [ ${#VTX[@]} = 0 ] && [ ${#VRX[@]} = 0 ]; then
  KEEP_STREAMS=1
  if [ -z "$ROLES" ] && [ -z "$MODE" ]; then
    ROLES=$(sed -n 's/^roles: *\[\(.*\)\].*/\1/p' /etc/owl-node/node.yaml | tr -d ' ' | head -1)
    [ -n "$ROLES" ] && echo "owl-node: keeping the installed roles $ROLES and its senders/receivers (pass --tx/--rx/--vtx/--vrx/--roles to change them)"
  fi
fi
if [ -z "$ROLES" ]; then
  case "${MODE:-node}" in hub) ROLES="agent,audio,hub";; *) ROLES="agent,audio,dsp";; esac
elif [ "$MODE" = hub ] && [[ ",$ROLES," != *",hub,"* ]]; then ROLES="$ROLES,hub"; fi
put() { # put MODE DEST (content on stdin; sudo outside $HOME)
  local mode="$1" dest="$2" tmp; tmp=$(mktemp); cat > "$tmp"
  if [ "$DRY" = 1 ]; then
    if [ -f "$dest" ] && cmp -s "$tmp" "$dest"; then echo "= $dest (unchanged)"; else echo "+ write $dest"; [ -f "$dest" ] && diff -u "$dest" "$tmp" | sed 's/^/    /' || sed 's/^/    /' "$tmp"; fi
  else case "$dest" in "$HOME"/*) mkdir -p "$(dirname "$dest")"; install -m "$mode" "$tmp" "$dest";;
         *) sudo install -d "$(dirname "$dest")"; sudo install -m "$mode" "$tmp" "$dest";; esac; fi
  rm -f "$tmp"
}
ETC=/etc/owl-node OPT=/opt/owl-node STATE=/var/lib/owl-node RUNDIR=/run/owl-node
TMPF=/etc/tmpfiles.d/owl-node.conf PTPSTAT_UNIT=/etc/systemd/system/owl-node-ptpstat.service
BOOT_UNIT=/etc/systemd/system/owl-node.service
PTP_UNIT=/etc/systemd/system/owl-ptp4l.service UDEV=/etc/udev/rules.d/60-owl-node-ptp.rules
PTP_UDP_UNIT=/etc/systemd/system/owl-ptp4l-udpv4.service PTP_UDP_CFG=/etc/linuxptp/owl-aes67-gm-udpv4.cfg
U=$(id -un) UIDN=$(id -u) GIDN=$(id -g)
DOCKER="docker"; docker info >/dev/null 2>&1 || DOCKER="sudo docker"     # docker group members need no sudo
if [ ${#VTX[@]} -gt 0 ] && [[ ",$ROLES," != *",video-enc,"* ]]; then ROLES="$ROLES,video-enc"; fi
if [ ${#VRX[@]} -gt 0 ] && [[ ",$ROLES," != *",video-dec,"* ]]; then ROLES="$ROLES,video-dec"; fi
PROFILES=""; for r in audio hub dsp video-enc video-dec; do if [[ ",$ROLES," == *",$r,"* ]]; then PROFILES="$PROFILES --profile $r"; fi; done
CFILES="-f $OPT/docker-compose.yml -f $OPT/docker-compose.devices.yml"
compose() { run $DOCKER compose --project-directory "$OPT" $CFILES --env-file "$OPT/.env" $PROFILES "$@"; }

if [ "$REMOVE" = 1 ]; then
  if [ -f "$OPT/docker-compose.yml" ]; then
    f=(-f "$OPT/docker-compose.yml"); [ -f "$OPT/docker-compose.devices.yml" ] && f+=(-f "$OPT/docker-compose.devices.yml")
    run $DOCKER compose --project-directory "$OPT" "${f[@]}" --env-file "$OPT/.env" --profile audio --profile hub \
      --profile video-enc --profile video-dec --profile dsp down --remove-orphans || true
  fi
  run sudo systemctl disable owl-node.service 2>/dev/null || true
  run sudo systemctl disable --now owl-node-ptpstat.service 2>/dev/null || true
  run sudo systemctl disable --now owl-ptp4l-udpv4.service 2>/dev/null || true
  for f in "$BOOT_UNIT" "$PTPSTAT_UNIT" "$TMPF" "$UDEV" "$PTP_UDP_UNIT" "$PTP_UDP_CFG" /usr/local/lib/owl-node/owl-node-ptpstat.py; do [ -e "$f" ] && run sudo rm -f "$f"; done
  [ -d "$OPT" ] && run sudo rm -rf "$OPT"
  [ -d "$ETC" ] && run sudo rm -rf "$ETC"
  [ -d "$RUNDIR" ] && run sudo rm -rf "$RUNDIR"
  if [ -d "$STATE" ]; then run sudo mv "$STATE" "$STATE.removed-$(date +%Y%m%d%H%M%S)"; fi   # keep the patch state
  run sudo systemctl daemon-reload
  echo "removed owl-node N2 (owl-ptp4l.service left as it was: N1's aes67/install.sh --remove owns it;"
  echo "the image stays loaded: docker image rm $IMAGE)"
  exit 0
fi

# ---------------------------------------------------------------- validate
yget() { [ -r "$ETC/node.yaml" ] || return 0; sed -n "s/^$1: *\"\{0,1\}\([^\"#]*\)\"\{0,1\}.*/\1/p" "$ETC/node.yaml" | head -1 | sed 's/ *$//'; }
# a re-run keeps the installed name/label/area: designs refer to devices by name (pw:<name>:...), so a hostname that
# differs from the installed name must never rename the node behind the household's back
if [ -z "$NAME" ] && [ -n "$(yget name)" ]; then NAME=$(yget name); say "keeping the installed name $NAME (--name to change it)"; fi
[ -n "$LABEL" ] || LABEL=$(yget label)
[ -n "$AREA" ] || AREA=$(yget area)
[ -n "$NAME" ] || NAME=$(hostname -s)
[ -n "$LABEL" ] || LABEL="$NAME"
wired_ok() { # a real wired interface (not loopback, Wi-Fi, Docker/VM bridges, VPNs)
  local i="$1"; [ -e "/sys/class/net/$i" ] || return 1
  [ -d "/sys/class/net/$i/wireless" ] && return 1
  case "$i" in lo|docker*|br-*|veth*|virbr*|vnet*|tailscale*|wg*|zt*|tun*|tap*|cni*|flannel*|podman*) return 1;; esac
  ip -4 -o addr show dev "$i" 2>/dev/null | grep -q inet
}
phc_of() { # /dev/ptpN of an interface, or nothing
  local n; n=$(ls "/sys/class/net/$1/device/ptp" 2>/dev/null | head -1 || true)
  [ -n "$n" ] || n=$( (/usr/sbin/ethtool -T "$1" 2>/dev/null || ethtool -T "$1" 2>/dev/null || true) | awk -F': *' '/PTP Hardware Clock/ && $2 ~ /^[0-9]+$/ {print "ptp" $2}')
  [ -n "$n" ] && [ -e "/dev/$n" ] && echo "/dev/$n"
}
if [ -z "$MEDIA_IF" ]; then
  MEDIA_IF=$(ip route get 1.1.1.1 2>/dev/null | sed -n 's/.* dev \([^ ]*\).*/\1/p' | head -1)
  if [ -z "$MEDIA_IF" ] || ! wired_ok "$MEDIA_IF"; then
    was="$MEDIA_IF"; MEDIA_IF=""
    for i in $(ls /sys/class/net); do if wired_ok "$i"; then MEDIA_IF="$i"; break; fi; done
    [ -n "$MEDIA_IF" ] || { echo "no wired network interface with an IPv4 address (the default route uses ${was:-nothing}): AES67 and PTP need a cable. Plug one in, or pass --media-if" >&2; exit 1; }
    say "the default route uses ${was:-nothing} (not wired): using $MEDIA_IF"
  fi
  say "media interface $MEDIA_IF (detected; --media-if to choose another)"
fi
if [ -z "$PHC_IF" ]; then
  PHC_IF=none
  if [ -n "$(phc_of "$MEDIA_IF")" ]; then PHC_IF="$MEDIA_IF"
  elif [ -d "/sys/class/net/$MEDIA_IF/brif" ]; then
    for m in $(ls "/sys/class/net/$MEDIA_IF/brif"); do if [ -n "$(phc_of "$m")" ]; then PHC_IF="$m"; break; fi; done
  fi
  if [ "$PHC_IF" = none ]; then say "no PTP hardware clock on $MEDIA_IF: audio runs on the system clock (it lists devices; AES67 audio will drift, plan E4)"
  else say "PTP hardware clock on $PHC_IF (detected)"; fi
fi
IP=$(ip -4 -o addr show dev "$MEDIA_IF" 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -1)
[ -n "$IP" ] || { echo "$MEDIA_IF has no IPv4 address" >&2; exit 1; }
case "$MEDIA_IF${PHC_IF}" in *wl*) echo "refusing a Wi-Fi interface: AES67 and PTP are wired-only" >&2; exit 1;; esac
PHC_DEV=/dev/null
if [ -n "$PHC_IF" ] && [ "$PHC_IF" != none ]; then
  # the PHC index: sysfs where the driver exposes it (igc on the box), else ethtool (macb on the Pi 5 does not)
  n=$(ls "/sys/class/net/$PHC_IF/device/ptp" 2>/dev/null | head -1 || true)
  [ -n "$n" ] || n=$( (/usr/sbin/ethtool -T "$PHC_IF" 2>/dev/null || true) | awk -F': *' '/PTP Hardware Clock/ && $2 ~ /^[0-9]+$/ {print "ptp" $2}')
  [ -n "$n" ] && [ -e "/dev/$n" ] || { echo "$PHC_IF has no PTP hardware clock" >&2; exit 1; }
  PHC_DEV=/dev/$n
else PHC_IF=none; fi
HUB=0; [[ ",$ROLES," == *",hub,"* ]] && HUB=1
# E2: a node needs no registry up front: the hub's designer pairs it (Nodes -> Add writes the hub's registry to
# /var/lib/owl-node/hub.json, which the agent prefers); --registry still works for a node without the dsp role
# a re-run keeps what the last install chose: the registry of the existing node.yaml, start-at-boot from .env
if [ "$REGISTRY" = none ]; then REGISTRY=""; REG_NONE=1; else REG_NONE=0; fi
if [ -z "$REGISTRY" ] && [ "$REG_NONE" = 0 ] && [ $HUB = 0 ] && [ -r "$ETC/node.yaml" ]; then
  REGISTRY=$(sed -n 's/^registry: *\(https\?:\/\/[^ #]*\).*/\1/p' "$ETC/node.yaml" | head -1)
  [ -n "$REGISTRY" ] && say "keeping the registry of the existing node.yaml ($REGISTRY)"
fi
if [ -z "$ENABLE" ] && [ -r "$OPT/.env" ] && grep -q '^OWL_RESTART=unless-stopped' "$OPT/.env"; then
  ENABLE=1; say "keeping start-at-boot (enabled by the last install; --no-enable to turn it off)"
fi
[ -n "$REGISTRY" ] || { [ $HUB = 1 ] && REGISTRY="http://$IP:8010"; } || true
if [ -z "$PTP" ]; then
  if [ "$PHC_IF" = none ]; then PTP=none
  elif systemctl cat owl-ptp4l.service >/dev/null 2>&1; then PTP=keep
  elif [ $HUB = 1 ]; then PTP=grandmaster
  else PTP=backup; fi
fi
case "$PTP" in keep|grandmaster|backup|follower|none) ;; *) echo "--ptp must be keep, grandmaster, backup, follower or none" >&2; exit 2;; esac
if [ "$PTP" = none ] && [ "$PHC_IF" != none ]; then PHC_IF=none; PHC_DEV=/dev/null; fi
if [ -z "$PTP_UDP" ]; then [ -e "$PTP_UDP_UNIT" ] && PTP_UDP=1 || PTP_UDP=0; fi        # keep what is installed
if [ -n "$RESTORE_FROM" ]; then
  [ $HUB = 1 ] || { echo "--restore-from is for a hub (it restores the previous hub's designs and nodes)" >&2; exit 2; }
  [ "$START" = 1 ] || { echo "--restore-from needs the hub started (drop --no-start)" >&2; exit 2; }
  # read a file now (as this user, before anything is reinstalled); a node address is fetched once the hub runs
  if [ -f "$RESTORE_FROM" ]; then
    RESTORE_FILE=$(mktemp); cp "$RESTORE_FROM" "$RESTORE_FILE" 2>/dev/null || sudo cat "$RESTORE_FROM" > "$RESTORE_FILE"
  fi
fi
if [ "$PTP_UDP" = 1 ]; then
  [ $HUB = 1 ] || { echo "--ptp-udp is for the hub (the house's grandmaster)" >&2; exit 2; }
  [ "$PHC_IF" != none ] || { echo "--ptp-udp needs a PTP hardware clock" >&2; exit 2; }
  [ "$PHC_IF" = "$MEDIA_IF" ] || { echo "--ptp-udp: the PHC ($PHC_IF) is not the media interface ($MEDIA_IF): a UDP socket on a bridge port never receives (see ptp/ptp4l-gm-udpv4.cfg)" >&2; exit 2; }
fi
if [ "$PTP" = keep ] && [ "$PHC_IF" != none ] && ! systemctl cat owl-ptp4l.service >/dev/null 2>&1; then
  echo "no owl-ptp4l.service on this host: pass --ptp grandmaster|backup|follower" >&2; exit 1; fi
command -v docker >/dev/null || { echo "Docker is not installed. Install Docker Engine with the compose plugin (https://docs.docker.com/engine/install/), add yourself to the docker group, log in again, re-run." >&2; exit 1; }
docker compose version >/dev/null 2>&1 || $DOCKER compose version >/dev/null 2>&1 || { echo "the Docker compose plugin is missing (apt install docker-compose-plugin)" >&2; exit 1; }
NEED_BUILD=0
# a released version is published for amd64 and arm64 (the repository's CI): pull it instead of building it here
PREBUILT="${OWL_NODE_REGISTRY:-ghcr.io/morningstarowl/owl-node}:${IMAGE##*:}"
if [ "$BUILD" = always ]; then NEED_BUILD=1
elif ! $DOCKER image inspect "$IMAGE" >/dev/null 2>&1 && [ "$BUILD" != no ] && [ "${IMAGE%%:*}" = owl-node ]; then
  if [ "$DRY" = 1 ]; then say "would pull the prebuilt image $PREBUILT (else build it here)"
  else
    say "pulling the prebuilt image $PREBUILT"
    if $DOCKER pull -q "$PREBUILT" >/dev/null 2>&1 && $DOCKER tag "$PREBUILT" "$IMAGE"; then say "using $PREBUILT as $IMAGE"
    else say "no prebuilt $PREBUILT (an unreleased checkout, or no network): building it here instead"; fi
  fi
fi
if [ "$NEED_BUILD" = 0 ] && [ "$DRY" = 0 -o "${IMAGE%%:*}" != owl-node ] && ! $DOCKER image inspect "$IMAGE" >/dev/null 2>&1; then
  if [ "$BUILD" = no ]; then echo "image $IMAGE is not loaded (build.sh on a build machine, or drop --no-build)" >&2; exit 1; fi
  [ -f "$HERE/Dockerfile" ] || { echo "image $IMAGE is not loaded and there is no Dockerfile next to install.sh to build it" >&2; exit 1; }
  if ask "The owl-node image $IMAGE is not on this machine. Build it here now (minutes on a PC, 20-40 min on a Pi)?" y; then NEED_BUILD=1
  else echo "not building; load $IMAGE (build.sh) and re-run" >&2; exit 1; fi
fi
[ -S "/run/user/$UIDN/pipewire-0" ] || { echo "no PipeWire socket at /run/user/$UIDN/pipewire-0 (run as the audio user)" >&2; exit 1; }
if systemctl --user is-active -q owl-aes67.service 2>/dev/null; then
  if [ "$REPLACE_N1" = 1 ]; then run systemctl --user stop owl-aes67.service
  else echo "N1's owl-aes67 user unit is running; it would duplicate the PTP driver and senders. Re-run with --replace-n1 (stops it, leaves its files)" >&2; exit 1; fi
fi
# E2: a sender and a receiver unless told otherwise (what a node needs to be wired across hosts)
# (a hub's sender has 8 channels: it is where most streams to other rooms start; four stereo sources fit side by side)
if [ "$KEEP_STREAMS" = 0 ] && [[ ",$ROLES," == *",audio,"* ]] && [ ${#TX[@]} = 0 ] && [ ${#RX[@]} = 0 ]; then
  if [ $HUB = 1 ]; then TX=("$LABEL|channels=8"); else TX=("$LABEL"); fi
  RX=("$LABEL RX 1")
fi
for t in ${TX[@]+"${TX[@]}"}; do
  case "$t" in *"|"*) o="${t#*|}"
    [[ "$o" =~ ^channels=[1-8]$ ]] || { echo "--tx '$t': the only option is channels=1..8 (e.g. --tx 'Living Room|channels=8')" >&2; exit 2; };; esac
done
if [ -z "$ENABLE" ]; then
  if [ "$DRY" = 1 ]; then ENABLE=0
  elif ask "Start owl-node automatically when this machine boots?" y n; then ENABLE=1; else ENABLE=0; fi
fi
for p in 8360 $([ $HUB = 1 ] && echo 8361 8010 8011 8889 9997) $([[ ",$ROLES," == *",hub,"* || ",$ROLES," == *",dsp,"* ]] && echo 8362); do
  if ss -ltnH "sport = :$p" | grep -q . && ! $DOCKER ps --format '{{.Names}}' | grep -q '^owl-node-'; then echo "port $p is already in use" >&2; exit 1; fi
done

# ---------------------------------------------------------------- node.yaml
LAST=${IP##*.}
yq() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }
vopts() { # vopts ID 'LABEL|k=v,k=v' sender|receiver  -> one YAML flow mapping (N3 video)
  local id="$1" spec="$2" kind="$3" label="${2%%|*}" opts="" src="" out kv k v
  [[ "$spec" == *"|"* ]] && opts="${spec#*|}"
  out="{id: $id, label: \"$(yq "$label")\""
  IFS=, read -ra kv <<< "$opts"
  for x in ${kv[@]+"${kv[@]}"}; do k="${x%%=*}"; v="${x#*=}"
    case "$kind:$k" in
      sender:source) src="$src, kind: $v";; sender:device|sender:format|sender:width|sender:height|sender:fps|sender:pattern) src="$src, $k: $v";;
      sender:bitrate) out="$out, bitrate_kbps: $v";; sender:latency|receiver:latency) out="$out, latency_ms: $v";;
      sender:preview) out="$out, preview: $([ "$v" = no ] && echo false || echo true)";;
      receiver:codecs) out="$out, codecs: [${v//+/, }]";;
      *) out="$out, $k: $v";;
    esac; done
  if [ -n "$src" ]; then out="$out, source: {${src#, }}"; fi
  echo "    - $out}"
}
{
  echo "# owl-node $NAME (written by owl-node/install.sh; re-run it to change)"
  echo "name: \"$(yq "$NAME")\""
  echo "label: \"$(yq "$LABEL")\""
  echo "area: \"$(yq "$AREA")\""
  echo "roles: [$(echo "$ROLES" | sed 's/,/, /g')]"
  echo "interfaces: {media: $MEDIA_IF}"
  echo "clock: {phc_if: $PHC_IF}"
  echo "http_port: 8360"
  if [ -n "$REGISTRY" ]; then echo "registry: $REGISTRY"
  else echo "registry: []   # none: the hub pairs this node (designer -> Nodes -> Add $IP), see /var/lib/owl-node/hub.json"; fi
  if [ "$KEEP_STREAMS" = 1 ]; then awk '/^audio:/{f=1} f' "$ETC/node.yaml"; else
  echo "audio:"
  echo "  senders:"
  i=0; for t in ${TX[@]+"${TX[@]}"}; do i=$((i+1)); c=""; [[ "$t" == *"|"* ]] && { c="${t##*|channels=}"; t="${t%%|*}"; }
    l="${t%@*}"; g=""; [[ "$t" == *@* ]] && g="${t##*@}"
    echo "    - {id: tx$i, label: \"$(yq "$l")\"$([ -n "$g" ] && echo ", group: $g")$([ -n "$c" ] && echo ", channels: $c")}"; done
  if [ $i = 0 ]; then echo "    []"; fi
  echo "  receivers:"
  i=0; for r in ${RX[@]+"${RX[@]}"}; do i=$((i+1)); l="${r%%>*}"; k=""; [[ "$r" == *">"* ]] && k="${r#*>}"
    echo "    - {id: rx$i, label: \"$(yq "$l")\"$([ -n "$k" ] && echo ", link_to: \"$(yq "$k")\"")}"; done
  if [ $i = 0 ]; then echo "    []"; fi
  if [ ${#VTX[@]} -gt 0 ] || [ ${#VRX[@]} -gt 0 ]; then
    echo "video:"
    echo "  senders:"; i=0
    for t in ${VTX[@]+"${VTX[@]}"}; do i=$((i+1)); vopts "vtx$i" "$t" sender; done
    if [ $i = 0 ]; then echo "    []"; fi
    echo "  receivers:"; i=0
    for r in ${VRX[@]+"${VRX[@]}"}; do i=$((i+1)); vopts "vrx$i" "$r" receiver; done
    if [ $i = 0 ]; then echo "    []"; fi
  fi
  fi
} | put 0644 "$ETC/node.yaml"
if [ $HUB = 1 ]; then
  put 0644 "$ETC/registry.json" <<EOF
{"pri": 10, "logging_level": 0, "http_port": 8010, "query_ws_port": 8011, "registration_expiry_interval": 12,
 "label": "owl-node registry ($NAME)", "host_address": "$IP"}
EOF
fi

if [ $HUB = 1 ]; then
  sed "s/@HUB_IP@/$IP/g" "$HERE/mediamtx/mediamtx.yml.in" | put 0644 "$ETC/mediamtx.yml"
fi

# ---------------------------------------------------------------- video devices (N3)
gid() { getent group "$1" | cut -d: -f3; }
if [ ${#VRX[@]} -gt 0 ]; then   # the Pi 5's stateless HEVC decoder: its /dev/videoN plus the media controller nodes
  for d in /sys/class/video4linux/video*; do
    [ "$(cat "$d/name" 2>/dev/null)" = rpi-hevc-dec ] || continue
    VDEV+=("/dev/$(basename "$d")"); for m in /dev/media*; do [ -e "$m" ] && VDEV+=("$m"); done
  done
fi
if [ "$KEEP_STREAMS" = 1 ] && [ ${#VDEV[@]} = 0 ] && [ -r "$OPT/docker-compose.devices.yml" ]; then
  put 0644 "$OPT/docker-compose.devices.yml" < "$OPT/docker-compose.devices.yml"      # kept with the streams
else {
  echo "# owl-node $NAME: per-host video devices (install.sh; N3). Added to every compose call with a second -f."
  echo "services:"
  for svc in video-enc video-dec; do
    echo "  $svc:"
    if [ ${#VDEV[@]} -gt 0 ]; then echo "    devices:"; for d in "${VDEV[@]}"; do echo "      - $d:$d"; done
    else echo "    environment: {}"; fi
  done
} | put 0644 "$OPT/docker-compose.devices.yml"
fi

# ---------------------------------------------------------------- compose + env
put 0644 "$OPT/docker-compose.yml" < "$HERE/docker-compose.yml"
put 0644 "$OPT/registry-container-config" < "$HERE/registry-container-config"
put 0644 "$OPT/.env" <<EOF
# owl-node $NAME (install.sh)
OWL_NODE_IMAGE=$IMAGE
OWL_MQTT_URL=$MQTT_URL
OWL_UID=$UIDN
OWL_GID=$GIDN
OWL_PTP_DEV=$PHC_DEV
OWL_RESTART=$( [ $ENABLE = 1 ] && echo unless-stopped || echo "no" )
OWL_RENDER_GID=$(gid render || echo 65534)
OWL_VIDEO_GID=$(gid video || echo 65534)
EOF
put 0644 "$TMPF" <<EOF
# owl-node run-time status (audio.json from the audio container, ptp.json from owl-node-ptpstat)
d $RUNDIR 0755 $U $U -
d $STATE 0755 $U $U -
EOF
run sudo systemd-tmpfiles --create "$TMPF"

# ---------------------------------------------------------------- PTP
if [ "$PHC_IF" != none ]; then
  if [ "$PTP" = grandmaster ] || [ "$PTP" = backup ] || [ "$PTP" = follower ]; then
    command -v /usr/sbin/ptp4l >/dev/null || run sudo apt-get install -y linuxptp
    CFG=/etc/linuxptp/owl-aes67-$PTP.cfg
    put 0644 "$CFG" < "$HERE/../aes67/ptp4l-$PTP.cfg"
    put 0644 "$PTP_UNIT" <<EOF
[Unit]
Description=owl-node PTP $PTP on $PHC_IF (AES67 media clock)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/sbin/ptp4l -f $CFG -i $PHC_IF
Restart=always
RestartSec=5
CapabilityBoundingSet=CAP_NET_ADMIN CAP_NET_BIND_SERVICE CAP_NET_RAW CAP_SYS_ADMIN CAP_SYS_TIME
DeviceAllow=char-ptp rw
DevicePolicy=closed

[Install]
WantedBy=multi-user.target
EOF
  fi
  if [ "$PTP_UDP" = 1 ]; then
    command -v /usr/sbin/ptp4l >/dev/null || run sudo apt-get install -y linuxptp
    put 0644 "$PTP_UDP_CFG" < "$HERE/ptp/ptp4l-gm-udpv4.cfg"
    put 0644 "$PTP_UDP_UNIT" <<EOF
[Unit]
Description=owl-node PTP over UDPv4 on $PHC_IF (server-only, for Dante / AES67 gear)
After=network-online.target owl-ptp4l.service
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/sbin/ptp4l -f $PTP_UDP_CFG -i $PHC_IF
Restart=always
RestartSec=5
# its management sockets live in /run/timemaster (the only extra-instance path Ubuntu's ptp4l AppArmor profile allows)
RuntimeDirectory=timemaster
RuntimeDirectoryPreserve=yes
CapabilityBoundingSet=CAP_NET_ADMIN CAP_NET_BIND_SERVICE CAP_NET_RAW CAP_SYS_ADMIN CAP_SYS_TIME
DeviceAllow=char-ptp rw
DevicePolicy=closed

[Install]
WantedBy=multi-user.target
EOF
  elif [ -e "$PTP_UDP_UNIT" ]; then
    run sudo systemctl disable --now owl-ptp4l-udpv4.service 2>/dev/null || true
    run sudo rm -f "$PTP_UDP_UNIT" "$PTP_UDP_CFG"
  fi
  put 0644 "$UDEV" <<'EOF'
# owl-node: the audio container (running as the desktop user) reads the NIC's PTP hardware clock. Read-only for others.
SUBSYSTEM=="ptp", MODE="0664"
EOF
  run sudo udevadm control --reload; run sudo udevadm trigger --subsystem-match=ptp --action=change
  put 0755 /usr/local/lib/owl-node/owl-node-ptpstat.py < "$HERE/host/owl-node-ptpstat.py"
  put 0644 "$PTPSTAT_UNIT" <<EOF
[Unit]
Description=owl-node PTP status for the NMOS agent (pmc -> $RUNDIR/ptp.json)
After=owl-ptp4l.service

[Service]
ExecStart=/usr/bin/python3 /usr/local/lib/owl-node/owl-node-ptpstat.py --out $RUNDIR/ptp.json
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
fi
put 0644 "$BOOT_UNIT" <<EOF
[Unit]
Description=owl-node containers ($NAME: $ROLES), started once the audio user's session and PTP are up
# The audio container bind-mounts /run/user/$UIDN (the host PipeWire socket); at boot it may not exist yet when
# dockerd restores containers, so this unit (re)starts the stack after the user manager. Idempotent with the
# containers' own restart: unless-stopped.
Requires=docker.service
After=docker.service network-online.target user@$UIDN.service owl-ptp4l.service owl-node-ptpstat.service
Wants=network-online.target user@$UIDN.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStartPre=/bin/sh -c 'for i in \$(seq 1 60); do [ -S /run/user/$UIDN/pipewire-0 ] && exit 0; sleep 1; done; echo "no PipeWire socket after 60 s"; exit 0'
ExecStart=/usr/bin/docker compose --project-directory $OPT $CFILES --env-file $OPT/.env $PROFILES up -d
ExecStop=/usr/bin/docker compose --project-directory $OPT $CFILES --env-file $OPT/.env $PROFILES stop
TimeoutStartSec=180

[Install]
WantedBy=multi-user.target
EOF
run sudo systemctl daemon-reload
if [ "$ENABLE" = 1 ]; then
  [ "$PHC_IF" != none ] && run sudo systemctl enable owl-ptp4l.service owl-node-ptpstat.service
  [ "$PTP_UDP" = 1 ] && run sudo systemctl enable owl-ptp4l-udpv4.service
  run sudo systemctl enable owl-node.service
else
  run sudo systemctl disable owl-node.service 2>/dev/null || true
fi

# ---------------------------------------------------------------- image (E2: built here when missing)
if [ "$NEED_BUILD" = 1 ]; then
  say "building $IMAGE from $HERE (docker build; this takes a while)"
  run $DOCKER build -t "$IMAGE" "$HERE"
fi

# ---------------------------------------------------------------- start
if [ "$START" = 1 ]; then
  if [ "$PHC_IF" != none ]; then
    [ "$PTP" = keep ] || run sudo systemctl restart owl-ptp4l.service
    [ "$PTP_UDP" = 1 ] && run sudo systemctl restart owl-ptp4l-udpv4.service
    run sudo systemctl restart owl-node-ptpstat.service
  fi
  compose up -d --remove-orphans
  if [ -n "$RESTORE_FROM" ] && [ "$DRY" = 0 ]; then
    say "restoring the previous hub's backup from $RESTORE_FROM"
    python3 - "$RESTORE_FROM" "${RESTORE_FILE:-}" <<'PY' || { echo "restore failed: run it again later with: owl-node/install.sh --hub --restore-from $RESTORE_FROM" >&2; exit 1; }
import json, sys, time, urllib.request
src, path = sys.argv[1], sys.argv[2]
def get(url, timeout=5):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read())
for _ in range(90):                     # the dsp role answers once its container is up
    try:
        get("http://127.0.0.1:8362/healthz", 2)
        break
    except Exception:
        time.sleep(2)
else:
    sys.exit("the hub's dsp role did not come up on :8362")
if path:
    bundle = json.load(open(path))
else:
    url = src if "://" in src else "http://" + src
    if url.count(":") < 2:
        url += ":8362"
    bundle = get(url.rstrip("/") + "/api/dsp/hub-backup", 10)
req = urllib.request.Request("http://127.0.0.1:8362/api/dsp/restore", data=json.dumps({"backup": bundle}).encode(),
                             method="POST", headers={"Content-Type": "application/json"})
with urllib.request.urlopen(req, timeout=120) as r:
    res = json.loads(r.read())
print(f"restored hub {res.get('from_hub')!r}'s backup ({', '.join(res.get('restored') or [])}); "
      f"nodes: {json.dumps(res.get('nodes') or {})}; deploy: {json.dumps(res.get('deploy'))}")
PY
    [ -n "${RESTORE_FILE:-}" ] && rm -f "$RESTORE_FILE"
  fi
fi
echo "$([ $DRY = 1 ] && echo "[dry-run] would install" || echo installed) owl-node $NAME: roles=$ROLES media=$MEDIA_IF ($IP) phc=$PHC_IF ptp=$PTP$([ "$PTP_UDP" = 1 ] && echo "+udpv4")" \
     "registry=${REGISTRY:-paired-by-hub} $([ "$KEEP_STREAMS" = 1 ] && echo "streams=kept from the existing node.yaml" || echo "senders=${#TX[@]} receivers=${#RX[@]} video senders=${#VTX[@]} video receivers=${#VRX[@]}")" \
     "at-boot=$([ $ENABLE = 1 ] && echo yes || echo no)"
if [ $HUB = 1 ]; then
  echo; echo "Next: open the designer at http://$IP:8361/ and add your nodes there (Nodes -> Add <node IP>)."
elif [[ ",$ROLES," == *",dsp,"* ]]; then
  echo; echo "Next: in your hub's designer (http://<hub>:8361/), Nodes -> Add $IP . This machine's inputs and outputs appear there."
fi
