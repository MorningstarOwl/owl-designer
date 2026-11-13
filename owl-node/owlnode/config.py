"""node.yaml loading, stable resource IDs, interface facts, shared state files.

/etc/owl-node/node.yaml (written by install.sh) is the only hand-edited file. Everything the agent changes at run
time (IS-05 active parameters) lives in /var/lib/owl-node/state.json, which the audio role reads, so a patch
survives container restarts and reboots. /run/owl-node/ holds run-time status (audio.json from the audio role,
ptp.json from the host's owl-node-ptpstat service).
"""
import fcntl, json, os, re, socket, struct, tempfile, time, uuid

import yaml

CONF = os.environ.get("OWL_NODE_CONF", "/etc/owl-node/node.yaml")
STATE_DIR = os.environ.get("OWL_NODE_STATE", "/var/lib/owl-node")
RUN_DIR = os.environ.get("OWL_NODE_RUN", "/run/owl-node")
NS = uuid.UUID("6f776c2d-6e6f-6465-2d6e-6d6f73000001")  # fixed namespace: IDs depend only on names

AES67_PORT = 5004
TX_MTU = 1280            # audio.py tx_conf net.mtu
MAX_TX_CHANNELS = 8      # 8 ch x 48 samples x 3 bytes = 1152 bytes + RTP header per 1 ms packet: the most under TX_MTU
JXS_PORT = 5010          # N4 JPEG XS RTP: sender n (and receiver n) default to 5010 + 2n
JXS_ECHO_PORT = 9190     # N4 latency test: a sender's UDP echo socket (receivers send back each frame's stamp)
# 8-bit 4:2:0 is what the test source and a UVC stick give; 4:2:2 10-bit is the ST 2110-22 / IPMX norm
JXS_FORMATS = {
    "i420": {"gst": "I420", "sampling": "YCbCr-4:2:0", "depth": 8, "bits": 12, "profile": "Main420.12",
             "components": (("Y", 1, 1), ("Cb", 2, 2), ("Cr", 2, 2))},
    "i422_10": {"gst": "I422_10LE", "sampling": "YCbCr-4:2:2", "depth": 10, "bits": 20, "profile": "Main422.10",
                "components": (("Y", 1, 1), ("Cb", 2, 1), ("Cr", 2, 1))},
}
# ISO/IEC 21122-2 sublevels: the smallest one whose nominal bits per pixel covers the codestream
JXS_SUBLEVELS = ((2, "Sublev2bpp"), (3, "Sublev3bpp"), (4, "Sublev4bpp"), (6, "Sublev6bpp"), (9, "Sublev9bpp"),
                 (12, "Sublev12bpp"))


def jxs_level(w, h, fps):
    """ISO/IEC 21122-2 level by frame width, picture size and luma sample rate: 2k-1 for 1080p up to 60 fps, 4k-1
    for 2160p60. The bounds are approximate (the household has no copy of the standard); `level:` in node.yaml wins."""
    rate = w * h * fps
    for name, max_w, max_px, max_rate in (("1k-1", 1280, 2_621_440, 83_558_400), ("2k-1", 2048, 4_194_304, 133_693_440),
                                          ("4k-1", 4096, 8_912_896, 534_773_760), ("4k-2", 4096, 16_777_216, 1_069_547_520),
                                          ("4k-3", 4096, 16_777_216, 2_139_095_040)):
        if w <= max_w and w * h <= max_px and rate <= max_rate:
            return name
    return "8k-1"


def stable_id(*parts):
    return str(uuid.uuid5(NS, ":".join(str(p) for p in parts)))


def tai_now():
    """IS-04/IS-05 timestamps are TAI 'sec:nsec'. TAI = UTC + 37 s (since 2017)."""
    t = time.time_ns() + 37 * 10**9
    return t


def tai_str(ns=None):
    ns = tai_now() if ns is None else ns
    return f"{ns // 10**9}:{ns % 10**9}"


def parse_tai(s):
    sec, nsec = s.split(":")
    return int(sec) * 10**9 + int(nsec)


_last_version = [0]


def new_version():
    """Monotonic resource version (two changes within one ns must still differ)."""
    ns = max(tai_now(), _last_version[0] + 1)
    _last_version[0] = ns
    return tai_str(ns)


def if_ipv4(ifname):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        return socket.inet_ntoa(fcntl.ioctl(s.fileno(), 0x8915, struct.pack("256s", ifname[:15].encode()))[20:24])
    except OSError:
        return None
    finally:
        s.close()


def if_mac(ifname):
    try:
        with open(f"/sys/class/net/{ifname}/address") as f:
            return f.read().strip().lower().replace(":", "-")
    except OSError:
        return None


def phc_index(ifname):
    """/dev/ptpN of a NIC (the PipeWire PTP driver opens the same one)."""
    try:
        for n in os.listdir(f"/sys/class/net/{ifname}/device/ptp"):
            if n.startswith("ptp"):
                return int(n[3:])
    except OSError:
        pass
    return None


def atomic_write(path, data):
    """Write-then-rename that survives a power cut: the data is fsync'd before the rename and the directory after it.
    Without the fsyncs a cut (a Pi on an SD card) could leave an empty state.json / dsp-design.json, which read_json
    then silently treats as "nothing saved"."""
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(data if isinstance(data, str) else json.dumps(data, indent=1, sort_keys=True))
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:
        dfd = os.open(d, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass                # some filesystems refuse a directory fsync; the file itself is already on disk


def read_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


class Node:
    """node.yaml, with defaults filled in and derived facts (IDs, addresses)."""

    def __init__(self, path=CONF):
        with open(path) as f:
            y = yaml.safe_load(f) or {}
        self.raw = y
        self.name = slug(y.get("name") or socket.gethostname())
        self.label = y.get("label") or y.get("name") or self.name
        self.area = y.get("area", "")
        self.roles = y.get("roles") or ["agent"]
        ifs = y.get("interfaces") or {}
        self.media_if = ifs.get("media") or "eth0"
        clk = y.get("clock") or {}
        self.phc_if = clk.get("phc_if", ifs.get("phc"))      # None/"none" -> system clock (test nodes only)
        if self.phc_if in ("none", ""):
            self.phc_if = None
        self.system_clock = clk.get("system", "realtime")
        self.ptp_domain = int(clk.get("domain", 0))
        # Receivers' timestamp-direct playout (N1: bit-exact, fixed 6 ms). In N2's layout (each receiver its own
        # process, PipeWire 1.6.2 clients) it came up silent, garbled or bit-exact at random across restarts, so the
        # default is the adaptive path (clean, ~70 dB SNR). "true" re-enables it for experiments.
        self.ts_direct = str(y.get("ts_direct", "false")).lower()
        # PTP graph quantum. 1.6's rtp-source asks for 103 frames for a 6 ms link, so the graph fell to 64 frames,
        # the Pi 5 deadline-miss case N1 fixed with 128. Every AES67 stream pins it.
        self.quantum = int(y.get("quantum", 128))
        self.http_port = int(y.get("http_port", 8360))
        self.registries = y.get("registry") or []
        if isinstance(self.registries, str):
            self.registries = [self.registries]
        self.loopback = bool(y.get("multicast_loop", False))   # test nodes: hear our own senders
        self.sap = bool(y.get("sap_announce", True))
        self.ttl = int(y.get("multicast_ttl", 32))             # 0 = never leaves this host (tests)
        a = y.get("audio") or {}
        ip = if_ipv4(self.media_if) or "0.0.0.0"
        last = ip.split(".")[-1]
        self.senders, self.receivers = [], []
        for i, s in enumerate(a.get("senders") or []):
            key = slug(s.get("id") or f"tx{i + 1}")
            self.senders.append({
                "key": key,
                "id": stable_id(self.name, "sender", key),
                "source_id": stable_id(self.name, "source", key),
                "flow_id": stable_id(self.name, "flow", key),
                "label": s.get("label") or f"{self.label} TX {i + 1}",
                "node": s.get("node") or f"aes67-tx-{self.name}" + ("" if i == 0 else f"-{i + 1}"),
                "description": s.get("description") or f"AES67 TX: {s.get('label') or self.label}",
                "session": s.get("session") or f"owl-{self.name}" + ("" if i == 0 else f"-{i + 1}"),
                "group": s.get("group") or f"239.69.{i + 1}.{last}",
                "port": int(s.get("port", AES67_PORT)),
                "channels": int(s.get("channels", 2)),
                "link_from": s.get("link_from"),
            })
            if not 1 <= self.senders[-1]["channels"] <= MAX_TX_CHANNELS:
                raise ValueError(f"audio sender {key}: channels must be 1..{MAX_TX_CHANNELS} (1 ms AES67 packets of "
                                 f"L24 at 48 kHz fit the {TX_MTU}-byte MTU up to {MAX_TX_CHANNELS} channels)")
        for i, r in enumerate(a.get("receivers") or []):
            key = slug(r.get("id") or f"rx{i + 1}")
            self.receivers.append({
                "key": key,
                "id": stable_id(self.name, "receiver", key),
                "label": r.get("label") or f"{self.label} RX {i + 1}",
                "node": r.get("node") or f"aes67-rx-{self.name}-{i + 1}",
                "channels": int(r.get("channels", 2)),
                "latency_ms": float(r.get("latency_ms", 6)),
                "link_to": r.get("link_to"),
            })
        self.node_id = stable_id(self.name, "node")
        self.device_id = stable_id(self.name, "device", "audio")
        # ---- N3 video (roles video-enc / video-dec): SRT senders (listeners) and receivers (callers)
        v = y.get("video") or {}
        self.video_device_id = stable_id(self.name, "device", "video")
        self.render_node = v.get("render_node", "/dev/dri/renderD128")
        pv = v.get("preview")                       # MediaMTX on the hub: {host, port (SRT 8890), webrtc (8889)}
        if pv is None and self.registries:
            hub = re.sub(r"^\w+://([^:/]+).*$", r"\1", self.registries[0])
            pv = {"host": hub}
        self.preview = None if pv in (False, None) else {"host": pv.get("host"), "port": int(pv.get("port", 8890)),
                                                         "webrtc": int(pv.get("webrtc", 8889))}
        self.vsenders, self.vreceivers = [], []
        for i, s in enumerate(v.get("senders") or []):
            key = slug(s.get("id") or f"vtx{i + 1}")
            src = dict({"kind": "testsrc", "width": 1920, "height": 1080, "fps": 30}, **(s.get("source") or {}))
            codec = str(s.get("codec", "h265")).lower()
            if codec == "jxs":
                ref = next((x for x in self.vsenders if s.get("source_of") and x["key"] == slug(s["source_of"])), None)
                if ref:                                             # same frames as an earlier sender (source_of)
                    src = ref["source"]
                self.vsenders.append(self._jxs_sender(i, key, s, src, last))
                continue
            encap = str(s.get("encap", "rtp")).lower()           # rtp (srt.rtp, default) | ts (srt.mp2t)
            self.vsenders.append({
                "transport": "srt",
                "key": key, "kind": "video",
                "id": stable_id(self.name, "sender", key),
                "source_id": stable_id(self.name, "source", key),
                "flow_id": stable_id(self.name, "flow", key),
                "mux_source_id": stable_id(self.name, "source", key, "mux"),
                "mux_flow_id": stable_id(self.name, "flow", key, "mux"),
                "label": s.get("label") or f"{self.label} video {i + 1}",
                "description": s.get("description") or f"{src['kind']} {src['width']}x{src['height']}@{src['fps']} "
                                                       f"{codec.upper()} over SRT ({'RTP' if encap == 'rtp' else 'MPEG-TS'})",
                "source": src, "codec": codec, "encap": encap,
                "bitrate_kbps": int(s.get("bitrate_kbps", 12000 if codec == "h265" else 16000)),
                "gop": int(s.get("gop", 2 * int(src["fps"]))),
                "port": int(s.get("port", 9001 + i)),
                "latency_ms": int(s.get("latency_ms", 40)),
                "preview": bool(s.get("preview", True)) and self.preview is not None,
                "preview_path": f"{self.name}-{key}",
            })
        for i, r in enumerate(v.get("receivers") or []):
            key = slug(r.get("id") or f"vrx{i + 1}")
            disp = str(r.get("display", "none")).lower()          # kms | none (headless: verify only)
            codecs = [c.lower() for c in (r.get("codecs") or ["h265", "h264"])]
            jxs = codecs == ["jxs"]                               # N4: an RTP (RFC 9134) receiver, not an SRT caller
            self.vreceivers.append({
                "transport": "rtp" if jxs else "srt",
                "port": int(r.get("port", JXS_PORT + 2 * i)) if jxs else None,
                "echo": r.get("echo"),                            # N4 latency test: "ip:port" of the sender's echo socket
                "key": key, "kind": "video",
                "id": stable_id(self.name, "receiver", key),
                "label": r.get("label") or f"{self.label} video RX {i + 1}",
                "display": disp,
                "verify": bool(r.get("verify", disp == "none")),
                "latency_ms": int(r.get("latency_ms", 40)),
                "decoder": r.get("decoder"),
                "kms_driver": r.get("kms_driver"), "connector_id": r.get("connector_id"),
                "codecs": codecs,
            })
        # senders that share another sender's source (and its process and preview): {source sender key: [keys]}
        by_key = {x["key"]: x for x in self.vsenders}
        for x in self.vsenders:
            if not x.get("source_of"):
                continue
            src = by_key.get(x["source_of"])
            if not src or src.get("source_of") or src["source"] is not x["source"]:
                raise ValueError(f"video sender {x['key']}: source_of {x['source_of']!r} must be an earlier video "
                                 f"sender on this node with its own source")
            x["preview_tag"] = src["preview"]                       # the ▶ of the shared source's preview
            x["preview_path"] = src["preview_path"]

    def _jxs_sender(self, i, key, s, src, last):
        """N4 video tier 2: JPEG XS (SVT) in RTP (RFC 9134) over plain UDP. Unicast by default (the switch floods
        unjoined multicast to some ports); `multicast: true` makes it an rtp.mcast sender on `group`."""
        fmt = str(s.get("jxs_format", "i420")).lower()           # i420 = 4:2:0 8-bit | i422_10 = 4:2:2 10-bit
        if fmt not in JXS_FORMATS:
            raise ValueError(f"video sender {key}: jxs_format {fmt!r} (one of {', '.join(JXS_FORMATS)})")
        f = JXS_FORMATS[fmt]
        w, h, fps = int(src["width"]), int(src["height"]), int(src["fps"])
        bpp = float(s.get("bpp", 3.0))                          # codestream bits per pixel: 3.0 = 4:1 on 8-bit 4:2:0
        mtu = int(s.get("mtu", 1400))                           # RTP packet size (header included)
        flow_kbps = -(-int(bpp * w * h * fps) // 1000)
        # IS-04 sender bit_rate / SDP b=AS: the IP stream: + 4-byte RFC 9134 header, 12 RTP, 8 UDP, 20 IP per packet
        sender_kbps = -(-flow_kbps * (mtu + 28) // (mtu - 16)) + 1
        sub = next((n for b, n in JXS_SUBLEVELS if bpp <= b), "Full")
        multicast = bool(s.get("multicast", False))
        label = s.get("label") or f"{self.label} JPEG XS {i + 1}"
        return {
            "transport": "rtp", "codec": "jxs", "encap": "rtp", "key": key, "kind": "video",
            "id": stable_id(self.name, "sender", key),
            "source_id": stable_id(self.name, "source", s.get("source_of") or key),
            "flow_id": stable_id(self.name, "flow", key),
            "label": label,
            "description": s.get("description") or f"{src['kind']} {w}x{h}@{fps} JPEG XS {bpp:g} bpp "
                                                    f"({f['bits'] / bpp:.1f}:1) in RTP (RFC 9134) over UDP "
                                                    f"{'multicast' if multicast else 'unicast'}",
            "source": src, "source_of": slug(s["source_of"]) if s.get("source_of") else None,
            "format": fmt, "gst_format": f["gst"], "sampling": f["sampling"], "depth": f["depth"],
            "components": f["components"],
            "profile": s.get("profile") or f["profile"], "level": s.get("level") or jxs_level(w, h, fps),
            "sublevel": s.get("sublevel") or sub,
            "bpp": bpp, "mtu": mtu, "bitrate_kbps": flow_kbps, "sender_kbps": sender_kbps,
            "threads": int(s.get("threads", 0)),
            "multicast": multicast, "group": s.get("group") or f"239.69.{20 + i}.{last}",
            "port": int(s.get("port", JXS_PORT + 2 * i)), "ttl": int(s.get("ttl", 4)),
            "echo_port": int(s.get("echo_port", JXS_ECHO_PORT + i)) if s.get("echo", True) else None,
            "preview": bool(s.get("preview", not s.get("source_of"))) and self.preview is not None and not s.get("source_of"),
            "preview_path": f"{self.name}-{key}",
        }

    @property
    def ip(self):
        return if_ipv4(self.media_if)

    @property
    def mac(self):
        return if_mac(self.media_if)

    def sender_by_key(self, key):
        return next((s for s in self.vsenders if s["key"] == key), None)

    def sender(self, rid):
        return next((s for s in self.senders + self.vsenders if s["id"] == rid), None)

    def receiver(self, rid):
        return next((r for r in self.receivers + self.vreceivers if r["id"] == rid), None)


STATE = os.path.join(STATE_DIR, "state.json")
# Pairing (E1): a hub that added this node by address in its designer wrote {hub, registry, hub_name, paired_at} here
# (PUT /api/dsp/pair on the node's dsp role). The agent registers with that registry first, so a node needs no
# `registry:` in node.yaml and no mDNS to join a hub.
HUB_FILE = os.path.join(STATE_DIR, "hub.json")
AUDIO_STATUS = os.path.join(RUN_DIR, "audio.json")
VIDEO_STATUS = os.path.join(RUN_DIR, "video-{}.json")      # video-enc / video-dec supervisors
PTP_STATUS = os.path.join(RUN_DIR, "ptp.json")
