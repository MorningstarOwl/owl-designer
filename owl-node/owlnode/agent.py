"""The `agent` role: an AMWA NMOS node.

  IS-04 v1.3 Node API        /x-nmos/node/v1.3/       self, devices, sources, flows, senders, receivers
  IS-04 registration client  registers with the registry (node.yaml `registry:`, else mDNS _nmos-register._tcp),
                             heartbeats every 5 s, re-registers on 404, falls back to peer-to-peer
  IS-05 v1.1 Connection API  /x-nmos/connection/v1.1/ single + bulk; activating a receiver starts/re-targets its
                             AES67 receive process (through state.json -> the audio role)
  owl status                 /x-owl/v1/status          PTP lock, per-receiver packets/loss/level, audio processes
"""
import json, os, socket, sys, threading, time, traceback, urllib.error, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from . import VERSION, config, sdp
from .config import new_version, read_json
from .connection import Endpoint, PatchError
from .rtpstat import Monitor

IS04 = "v1.3"
IS05 = "v1.1"
HEARTBEAT = 5
# JPEG XS profiles a receiver accepts: what SVT JPEG XS decodes in 4:2:0 / 4:2:2 / 4:4:4 (no Bayer, no alpha)
JXS_PROFILES = ["Main420.12", "High420.12", "Main422.10", "Main444.12", "High444.12"]


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def gmid_nmos(gm):
    """pmc's '5c60ba.fffe.6a29c8' -> NMOS '5c-60-ba-ff-fe-6a-29-c8'."""
    if not gm:
        return None
    h = gm.replace(".", "").replace("-", "").replace(":", "").lower()
    return "-".join(h[i:i + 2] for i in range(0, 16, 2)) if len(h) == 16 else None


def asset_tags(node, instance, function=None):
    """BCP-002-02 distinguishing information (the testing tool requires it to be unique once a node has 2 devices)."""
    t = {"urn:x-nmos:tag:asset:manufacturer/v1.0": ["owl-node"], "urn:x-nmos:tag:asset:product/v1.0": ["owl-node"],
         "urn:x-nmos:tag:asset:instance-id/v1.0": [instance]}
    if function:
        t["urn:x-nmos:tag:asset:function/v1.0"] = [function]
    return t


def channel_list(n):
    if n == 1:
        return [{"label": "Mono", "symbol": "M"}]
    if n == 2:
        return [{"label": "Left", "symbol": "L"}, {"label": "Right", "symbol": "R"}]
    return [{"label": f"Channel {i + 1}", "symbol": f"U{i + 1:02d}"} for i in range(n)]


class Agent:
    def __init__(self, node):
        self.n = node
        saved = read_json(config.STATE, {}) or {}
        self.eps = {}
        # one lock for every endpoint of this node: activation -> on_activate -> persist() reads all endpoints, so
        # per-endpoint locks deadlocked two concurrent activations (see connection.Endpoint)
        self.ep_lock = threading.RLock()
        for s in node.senders:
            self.eps[s["id"]] = Endpoint("sender", s, node, (saved.get("senders") or {}).get(s["key"]), self.ep_lock)
        for r in node.receivers:
            self.eps[r["id"]] = Endpoint("receiver", r, node, (saved.get("receivers") or {}).get(r["key"]), self.ep_lock)
        for s in node.vsenders:
            self.eps[s["id"]] = Endpoint("sender", s, node, (saved.get("video_senders") or {}).get(s["key"]), self.ep_lock)
        for r in node.vreceivers:
            self.eps[r["id"]] = Endpoint("receiver", r, node, (saved.get("video_receivers") or {}).get(r["key"]), self.ep_lock)
        self.static_version = new_version()
        self.node_version = new_version()
        self.lock = threading.RLock()
        self.mon = Monitor()
        self.mon.start()
        self.registry = None            # base URL we are registered with
        self.registered = False
        self.dirty = set()              # (type, id) to re-POST to the registry
        self.wake = threading.Event()
        self.clock = self._clock()
        self.persist()
        self._update_watches()

    # ---------------------------------------------------------------- facts
    @property
    def base(self):
        return f"http://{self.n.ip}:{self.n.http_port}"

    def ptp(self):
        p = read_json(config.PTP_STATUS) or {}
        if p and time.time() - p.get("time", 0) > 15:
            p = dict(p, stale=True)
        return p

    def _clock(self):
        if not self.n.phc_if:
            return {"name": "clk0", "ref_type": "internal"}
        p = self.ptp()
        gm = gmid_nmos(p.get("gm"))
        locked = bool(p) and not p.get("stale") and (
            p.get("port_state") == "MASTER" or (p.get("port_state") == "SLAVE" and abs(p.get("offset_ns") or 1e9) < 1000))
        c = {"name": "clk0", "ref_type": "ptp", "traceable": False, "version": "IEEE1588-2008",
             "gmid": gm or "00-00-00-00-00-00-00-00", "locked": locked and gm is not None}
        return c

    # ---------------------------------------------------------------- IS-04 resources
    def r_node(self):
        n, ip = self.n, self.n.ip
        return {
            "id": n.node_id, "version": self.node_version, "label": n.label,
            "description": f"owl-node {VERSION} ({', '.join(n.roles)})",
            "tags": dict(asset_tags(n, n.name), **({"urn:x-owl:area": [n.area]} if n.area else {})),
            "href": self.base + "/", "hostname": socket.gethostname(),
            "api": {"versions": [IS04], "endpoints": [{"host": ip, "port": n.http_port, "protocol": "http",
                                                       "authorization": False}]},
            "caps": {}, "services": [{"href": self.base + "/x-owl/v1/status", "type": "urn:x-owl:service:status/v1",
                                      "authorization": False}],
            "clocks": [self.clock],
            "interfaces": [{"chassis_id": n.mac, "port_id": n.mac or "00-00-00-00-00-00", "name": n.media_if}],
        }

    def r_device(self):
        n = self.n
        return {
            "id": n.device_id, "version": self.static_version, "label": f"{n.label} audio",
            "description": "AES67 senders and receivers in the host's PipeWire graph",
            "tags": asset_tags(n, f"{n.name}/audio", "Audio endpoint"), "type": "urn:x-nmos:device:generic", "node_id": n.node_id,
            "senders": [s["id"] for s in n.senders], "receivers": [r["id"] for r in n.receivers],
            "controls": [{"href": f"{self.base}/x-nmos/connection/{IS05}/", "type": f"urn:x-nmos:control:sr-ctrl/{IS05}",
                          "authorization": False}],
        }

    def r_source(self, s):
        return {"id": s["source_id"], "version": self.static_version, "label": s["label"],
                "description": s["description"], "tags": {}, "caps": {}, "device_id": self.n.device_id,
                "parents": [], "clock_name": "clk0", "format": "urn:x-nmos:format:audio",
                "channels": channel_list(s["channels"])}

    def r_flow(self, s):
        return {"id": s["flow_id"], "version": self.static_version, "label": s["label"],
                "description": s["description"], "tags": {}, "source_id": s["source_id"],
                "device_id": self.n.device_id, "parents": [], "format": "urn:x-nmos:format:audio",
                "sample_rate": {"numerator": 48000}, "media_type": "audio/L24", "bit_depth": 24}

    def r_sender(self, s):
        ep = self.eps[s["id"]]
        return {"id": s["id"], "version": ep.version, "label": s["label"], "description": s["description"],
                # urn:x-owl:channels: the designer's compiler puts several sources side by side in one sender
                "tags": {"urn:x-owl:pipewire-node": [s["node"]], "urn:x-owl:sap-session": [s["session"]],
                         "urn:x-owl:channels": [str(s["channels"])]},
                "flow_id": s["flow_id"], "transport": "urn:x-nmos:transport:rtp.mcast", "device_id": self.n.device_id,
                "manifest_href": f"{self.base}/x-nmos/connection/{IS05}/single/senders/{s['id']}/transportfile",
                "interface_bindings": [self.n.media_if], "subscription": ep.subscribed}

    def r_receiver(self, r):
        ep = self.eps[r["id"]]
        return {"id": r["id"], "version": ep.version, "label": r["label"],
                "description": f"AES67 receiver -> PipeWire source {r['node']}",
                "tags": {"urn:x-owl:pipewire-node": [r["node"]]}, "device_id": self.n.device_id,
                "transport": "urn:x-nmos:transport:rtp.mcast", "interface_bindings": [self.n.media_if],
                "format": "urn:x-nmos:format:audio",
                "caps": {"media_types": ["audio/L24", "audio/L16"]}, "subscription": ep.subscribed}

    # ---- N3 video: a second device; SRT senders (listeners) and receivers (callers), Matrox "NMOS With SRT"
    def r_vdevice(self):
        n = self.n
        return {
            "id": n.video_device_id, "version": self.static_version, "label": f"{n.label} video",
            "description": "H.264/HEVC video over SRT and JPEG XS over RTP (owl-node video-enc / video-dec)",
            "tags": asset_tags(n, f"{n.name}/video", "Video endpoint"), "type": "urn:x-nmos:device:generic", "node_id": n.node_id,
            "senders": [s["id"] for s in n.vsenders], "receivers": [r["id"] for r in n.vreceivers],
            "controls": [{"href": f"{self.base}/x-nmos/connection/{IS05}/", "type": f"urn:x-nmos:control:sr-ctrl/{IS05}",
                          "authorization": False}],
        }

    def r_vsources(self, s):
        src = {"id": s["source_id"], "version": self.static_version, "label": s["label"],
               "description": s["description"], "tags": {"urn:x-owl:source-kind": [s["source"]["kind"]]}, "caps": {},
               "device_id": self.n.video_device_id, "parents": [], "clock_name": "clk0",
               "format": "urn:x-nmos:format:video",
               "grain_rate": {"numerator": int(s["source"]["fps"]), "denominator": 1}}
        out = [src]
        if s["encap"] == "ts":
            out.append(dict(src, id=s["mux_source_id"], label=s["label"] + " (MPEG-TS)", parents=[s["source_id"]],
                            format="urn:x-nmos:format:mux", tags={}))
        return out

    def r_vflows(self, s):
        src = s["source"]
        if s["codec"] == "jxs":         # BCP-006-01: components, profile/level/sublevel, bit_rate (the codestream)
            w, h = int(src["width"]), int(src["height"])
            return [{"id": s["flow_id"], "version": self.static_version, "label": s["label"],
                     "description": s["description"], "tags": {}, "source_id": s["source_id"],
                     "device_id": self.n.video_device_id, "parents": [], "format": "urn:x-nmos:format:video",
                     "media_type": "video/jxsv", "grain_rate": {"numerator": int(src["fps"]), "denominator": 1},
                     "frame_width": w, "frame_height": h, "colorspace": "BT709", "interlace_mode": "progressive",
                     "transfer_characteristic": "SDR",
                     "components": [{"name": n, "width": w // dx, "height": h // dy, "bit_depth": s["depth"]}
                                    for n, dx, dy in s["components"]],
                     "profile": s["profile"], "level": s["level"], "sublevel": s["sublevel"],
                     "bit_rate": s["bitrate_kbps"]}]
        f = {"id": s["flow_id"], "version": self.static_version, "label": s["label"], "description": s["description"],
             "tags": {}, "source_id": s["source_id"], "device_id": self.n.video_device_id, "parents": [],
             "format": "urn:x-nmos:format:video", "media_type": f"video/{s['codec'].upper()}",
             "grain_rate": {"numerator": int(src["fps"]), "denominator": 1},
             "frame_width": int(src["width"]), "frame_height": int(src["height"]), "colorspace": "BT709",
             "interlace_mode": "progressive", "transfer_characteristic": "SDR",
             "bit_rate": s["bitrate_kbps"]}
        if s["codec"] == "h265":
            f["profile"] = "Main"
        out = [f]
        if s["encap"] == "ts":
            out.append({"id": s["mux_flow_id"], "version": self.static_version, "label": s["label"] + " (MPEG-TS)",
                        "description": "MPEG-TS (H.222.0) carrying " + f["media_type"], "tags": {},
                        "source_id": s["mux_source_id"], "device_id": self.n.video_device_id, "parents": [s["flow_id"]],
                        "format": "urn:x-nmos:format:mux", "media_type": "application/mp2t",
                        "grain_rate": f["grain_rate"]})
        return out

    def r_vsender(self, s):
        ep = self.eps[s["id"]]
        tags = {"urn:x-owl:codec": [s["codec"]], "urn:x-owl:encap": [s["encap"]]}
        if s["codec"] == "jxs":
            if s.get("source_of"):
                tags["urn:x-owl:shares-source-with"] = [self.n.sender_by_key(s["source_of"])["id"]]
            if s.get("echo_port"):
                tags["urn:x-owl:latency-echo"] = [f"{self.n.ip}:{s['echo_port']}"]
        if s["preview"] or s.get("preview_tag"):
            pv = self.n.preview
            tags["urn:x-owl:preview-whep"] = [f"http://{pv['host']}:{pv['webrtc']}/{s['preview_path']}/whep"]
            tags["urn:x-owl:preview-page"] = [f"http://{pv['host']}:{pv['webrtc']}/{s['preview_path']}/"]
        if s["codec"] == "jxs":
            return {"id": s["id"], "version": ep.version, "label": s["label"], "description": s["description"],
                    "tags": tags, "flow_id": s["flow_id"],
                    "transport": "urn:x-nmos:transport:rtp." + ("mcast" if s["multicast"] else "ucast"),
                    "device_id": self.n.video_device_id,
                    "manifest_href": f"{self.base}/x-nmos/connection/{IS05}/single/senders/{s['id']}/transportfile",
                    "interface_bindings": [self.n.media_if], "subscription": ep.subscribed,
                    "bit_rate": s["sender_kbps"], "packet_transmission_mode": "codestream"}
        return {"id": s["id"], "version": ep.version, "label": s["label"], "description": s["description"],
                "tags": tags, "flow_id": s["mux_flow_id"] if s["encap"] == "ts" else s["flow_id"],
                "transport": "urn:x-matrox:transport:srt." + ("mp2t" if s["encap"] == "ts" else "rtp"),
                "device_id": self.n.video_device_id,
                "manifest_href": f"{self.base}/x-nmos/connection/{IS05}/single/senders/{s['id']}/transportfile",
                "interface_bindings": [self.n.media_if], "subscription": ep.subscribed, "bit_rate": s["bitrate_kbps"]}

    def r_vreceiver(self, r):
        ep = self.eps[r["id"]]
        if r["transport"] == "rtp":     # N4 JPEG XS: BCP-006-01 constraint sets (what svtjpegxsdec + the depayloader take)
            cs = {"urn:x-nmos:cap:meta:label": "JPEG XS (SVT), codestream packetization",
                  "urn:x-nmos:cap:format:media_type": {"enum": ["video/jxsv"]},
                  "urn:x-nmos:cap:format:profile": {"enum": JXS_PROFILES},
                  "urn:x-nmos:cap:format:level": {"enum": ["1k-1", "2k-1", "4k-1", "4k-2", "4k-3"]},
                  "urn:x-nmos:cap:format:sublevel": {"enum": ["Sublev2bpp", "Sublev3bpp", "Sublev4bpp", "Sublev6bpp",
                                                              "Sublev9bpp", "Sublev12bpp", "Full"]},
                  "urn:x-nmos:cap:format:color_sampling": {"enum": ["YCbCr-4:2:0", "YCbCr-4:2:2", "YCbCr-4:4:4"]},
                  "urn:x-nmos:cap:format:component_depth": {"enum": [8, 10, 12]},
                  "urn:x-nmos:cap:format:interlace_mode": {"enum": ["progressive"]},
                  "urn:x-nmos:cap:transport:packet_transmission_mode": {"enum": ["codestream"]}}
            return {"id": r["id"], "version": ep.version, "label": r["label"],
                    "description": "JPEG XS RTP (RFC 9134, unicast or multicast) -> svtjpegxsdec -> "
                                   + ("display (kmssink)" if r["display"] == "kms" else "headless verifier"),
                    "tags": {"urn:x-owl:display": [r["display"]]}, "device_id": self.n.video_device_id,
                    "transport": "urn:x-nmos:transport:rtp", "interface_bindings": [self.n.media_if],
                    "format": "urn:x-nmos:format:video",
                    "caps": {"media_types": ["video/jxsv"], "constraint_sets": [cs], "version": self.static_version},
                    "subscription": ep.subscribed}
        return {"id": r["id"], "version": ep.version, "label": r["label"],
                "description": f"SRT caller -> {', '.join(c.upper() for c in r['codecs'])} decode -> "
                               + ("display (kmssink)" if r["display"] == "kms" else "headless verifier"),
                "tags": {"urn:x-owl:display": [r["display"]]}, "device_id": self.n.video_device_id,
                "transport": "urn:x-matrox:transport:srt", "interface_bindings": [self.n.media_if],
                "format": "urn:x-nmos:format:video",
                "caps": {"media_types": [f"video/{c.upper()}" for c in r["codecs"]]}, "subscription": ep.subscribed}

    def resources(self):
        """[(type, data)] in registration order."""
        n = self.n
        out = [("node", self.r_node()), ("device", self.r_device())]
        out += [("source", self.r_source(s)) for s in n.senders]
        out += [("flow", self.r_flow(s)) for s in n.senders]
        out += [("sender", self.r_sender(s)) for s in n.senders]
        out += [("receiver", self.r_receiver(r)) for r in n.receivers]
        if n.vsenders or n.vreceivers:
            out.append(("device", self.r_vdevice()))
            out += [("source", x) for s in n.vsenders if not s.get("source_of") for x in self.r_vsources(s)]
            out += [("flow", x) for s in n.vsenders for x in self.r_vflows(s)]
            out += [("sender", self.r_vsender(s)) for s in n.vsenders]
            out += [("receiver", self.r_vreceiver(r)) for r in n.vreceivers]
        return out

    # ---------------------------------------------------------------- IS-05 side effects
    def on_activate(self, kind, ep):
        log(f"activated {kind} {ep.key}: {json.dumps({k: v for k, v in ep.active.items() if k != 'transport_file'})}")
        self.persist()
        self._update_watches()
        self.mark("sender" if kind == "sender" else "receiver", ep.id)

    def persist(self):
        with self.ep_lock:     # a consistent snapshot, written in activation order
            self._persist()

    def _persist(self):
        st = {"senders": {}, "receivers": {}, "video_senders": {}, "video_receivers": {}, "written": time.time()}
        for ep in self.eps.values():
            a = ep.view_active()
            if ep.kind == "receiver" and a.get("transport_file", {}).get("data"):
                try:
                    a["format"] = ep.parse_sdp(a["transport_file"]["data"])
                except sdp.SDPError:
                    a["format"] = None
            st[("video_" if ep.video else "") + ep.kind + "s"][ep.key] = a
        config.atomic_write(config.STATE, st)

    def _update_watches(self):
        for ep in self.eps.values():
            if ep.kind != "receiver" or ep.transport != "rtp" or ep.video:   # the AES67 wire monitor: audio only
                continue
            a, tp = ep.active, ep.active["transport_params"][0]
            if a["master_enable"] and tp.get("rtp_enabled") and tp.get("multicast_ip"):
                fmt = {}
                if (a.get("transport_file") or {}).get("data"):
                    try:
                        fmt = ep.parse_sdp(a["transport_file"]["data"])
                    except sdp.SDPError:
                        pass
                self.mon.watch(ep.key, tp["multicast_ip"], tp.get("destination_port") or 5004,
                               tp.get("interface_ip") or self.n.ip, tp.get("source_ip"),
                               channels=fmt.get("channels") or 2, width=2 if fmt.get("encoding") == "L16" else 3)
            else:
                self.mon.unwatch(ep.key)

    def mark(self, typ, rid):
        with self.lock:
            self.dirty.add((typ, rid))
        self.wake.set()

    # ---------------------------------------------------------------- registration
    def _http(self, method, url, body=None, timeout=3):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json"} if data else {})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def _post(self, base, typ, data):
        code, body = self._http("POST", f"{base}/x-nmos/registration/{IS04}/resource", {"type": typ, "data": data})
        if code not in (200, 201):
            raise RuntimeError(f"register {typ} {data['id']}: HTTP {code} {body[:300]!r}")
        return code

    def register_all(self, base):
        res = self.resources()
        code = self._post(base, "node", res[0][1])
        if code == 200:     # a stale registration of ours survived (e.g. crash): delete it and start clean
            self._http("DELETE", f"{base}/x-nmos/registration/{IS04}/resource/nodes/{self.n.node_id}")
            self._post(base, "node", res[0][1])
        for typ, data in res[1:]:
            self._post(base, typ, data)
        with self.lock:
            self.dirty.clear()

    def paired_registry(self):
        """The registry of the hub this node was paired with in a designer (config.HUB_FILE), or None."""
        from .config import HUB_FILE, read_json
        try:
            r = (read_json(HUB_FILE, None) or {}).get("registry")
            return r.rstrip("/") if r else None
        except Exception:
            return None

    def registries(self):
        """Candidates in priority order (the paired hub's registry first, then node.yaml, then mDNS); ones that failed
        in the last 30 s go last."""
        out = [u.rstrip("/") for u in self.n.registries]
        pr = self.paired_registry()
        if pr:
            out = [pr] + [u for u in out if u != pr]
        if not out and self.mdns:
            out = self.mdns.registries()
        now = time.time()
        return sorted(out, key=lambda b: now - self.bad.get(b, 0) < 30)

    def _heartbeat(self, base):
        try:
            code, _ = self._http("POST", f"{base}/x-nmos/registration/{IS04}/health/nodes/{self.n.node_id}")
            return code
        except Exception:
            return 0

    def _set_registered(self, base, how):
        self.registry, self.registered = base, True
        self.bad.pop(base, None)
        log(f"{how} {base}")
        if self.mdns:
            self.mdns.advertise(registered=True)

    def _connect(self, failed=None):
        """Find a registry. After a failure (IS-04 behaviour), a heartbeat goes first: the registries of one
        system share their data, so only a 404 means our resources are unknown there and must be re-POSTed."""
        if failed:
            self.bad[failed] = time.time()
        for base in self.registries():
            if base == failed:
                continue
            try:
                code = self._heartbeat(base) if failed else 404
                if code == 200:
                    self._set_registered(base, "failed over (heartbeat) to")
                    return True
                if code == 404:
                    self.register_all(base)
                    self._set_registered(base, f"registered {len(self.resources())} resources with")
                    return True
                self.bad[base] = time.time()
            except Exception as e:
                self.bad[base] = time.time()
                log(f"registry {base} failed: {e}")
        self.registered = False
        if self.mdns:
            self.mdns.advertise(registered=False)      # peer-to-peer mode
        return False

    def registrar(self):
        self.bad = {}
        last_hb = 0
        while True:
            if not self.registered:
                if self._connect():
                    last_hb = time.time()
                else:
                    self.wake.wait(2)
                    self.wake.clear()
                    continue
            pr = self.paired_registry()
            if pr and self.registry and pr != self.registry and time.time() - self.bad.get(pr, 0) > 30:
                # paired with another hub: register there first, leave the old registry only once that worked (a
                # paired registry that does not answer must not cost us the registration we have)
                try:
                    code = self._heartbeat(pr)
                    if code == 404:
                        self.register_all(pr)
                    elif code != 200:
                        raise RuntimeError(f"HTTP {code or 'no answer'}")
                    old = self.registry
                    self.unregister()
                    self._set_registered(pr, f"paired with a hub: moved from {old} to")
                    last_hb = time.time()
                except Exception as e:
                    self.bad[pr] = time.time()
                    log(f"paired registry {pr} not reachable ({e}); staying with {self.registry}")
            try:
                c = self._clock()                   # clock changes (PTP lock / GM) bump the node
                if c != self.clock:
                    self.clock, self.node_version = c, new_version()
                    self.mark("node", self.n.node_id)
                with self.lock:
                    dirty, self.dirty = self.dirty, set()
                if dirty:
                    rmap = {(t, d["id"]): d for t, d in self.resources()}
                    try:
                        for key in dirty:
                            if key in rmap:
                                self._post(self.registry, key[0], rmap[key])
                    except Exception:
                        with self.lock:
                            self.dirty |= dirty
                        raise
                if time.time() - last_hb >= HEARTBEAT:
                    code = self._heartbeat(self.registry)
                    if code == 404:
                        log("heartbeat 404: the registry forgot us, re-registering")
                        self.register_all(self.registry)
                    elif code != 200:
                        raise RuntimeError(f"heartbeat HTTP {code or 'no answer'}")
                    last_hb = time.time()
            except Exception as e:
                log(f"registry {self.registry} lost ({e}); failing over")
                if self._connect(failed=self.registry):
                    last_hb = time.time()
            self.wake.wait(0.5)
            self.wake.clear()

    def unregister(self):
        if self.registered and self.registry:
            try:
                self._http("DELETE", f"{self.registry}/x-nmos/registration/{IS04}/resource/nodes/{self.n.node_id}", timeout=2)
            except Exception:
                pass

    # ---------------------------------------------------------------- status
    def status(self):
        audio = read_json(config.AUDIO_STATUS) or {}
        if audio and time.time() - audio.get("time", 0) > 15:
            audio["stale"] = True
        rx = {}
        for r in self.n.receivers:
            ep = self.eps[r["id"]]
            a = ep.active
            rx[r["id"]] = {"key": r["key"], "node": r["node"], "master_enable": a["master_enable"],
                           "sender_id": a.get("sender_id"), "transport": a["transport_params"][0],
                           "wire": self.mon.stats(r["key"]),
                           "process": (audio.get("receivers") or {}).get(r["key"])}
        tx = {s["id"]: {"key": s["key"], "node": s["node"], "master_enable": self.eps[s["id"]].active["master_enable"],
                        "group": self.eps[s["id"]].active["transport_params"][0].get("destination_ip"),
                        "process": (audio.get("senders") or {}).get(s["key"])} for s in self.n.senders}
        video = {}
        for role in ("enc", "dec"):
            v = read_json(config.VIDEO_STATUS.format(role))
            if v:
                v["stale"] = time.time() - v.get("time", 0) > 15
                video[role] = v
        vtx = {}
        for s in self.n.vsenders:
            a = self.eps[s["id"]].active
            tp = a["transport_params"][0]
            proc = ((video.get("enc") or {}).get("streams") or {}).get(s.get("source_of") or s["key"]) or {}
            vtx[s["id"]] = {"key": s["key"], "codec": s["codec"], "master_enable": a["master_enable"],
                            "receiver_id": a.get("receiver_id"),
                            "preview": s["preview_path"] if s["preview"] or s.get("preview_tag") else None,
                            "process": proc if s["codec"] != "jxs" else dict(
                                {k: proc.get(k) for k in ("running", "up_s", "fps", "fps_nominal", "cpu_pct", "clock", "pid", "stale")},
                                jxs=(proc.get("jxs") or {}).get(s["key"]))}
            if s["codec"] == "jxs":
                vtx[s["id"]]["destination"] = f"{tp.get('destination_ip')}:{tp.get('destination_port')}"
            else:
                vtx[s["id"]]["listen"] = f"{self.n.ip}:{tp.get('source_port')}"
        vrx = {}
        for r in self.n.vreceivers:
            a = self.eps[r["id"]].active
            vrx[r["id"]] = {"key": r["key"], "master_enable": a["master_enable"], "sender_id": a.get("sender_id"),
                            "transport": a["transport_params"][0], "display": r["display"],
                            "process": ((video.get("dec") or {}).get("streams") or {}).get(r["key"])}
        return {"node_id": self.n.node_id, "name": self.n.name, "label": self.n.label, "area": self.n.area,
                "roles": self.n.roles,
                "version": VERSION, "time": time.time(), "registered": self.registered, "registry": self.registry,
                "ptp": self.ptp(), "clock": self.clock, "audio": {k: audio.get(k) for k in ("time", "stale", "base", "pipewire", "host_pipewire", "ts_direct")},
                "senders": tx, "receivers": rx,
                "video": {"supervisors": {k: {x: v.get(x) for x in ("time", "stale", "gst", "va", "decoders")}
                                          for k, v in video.items()}, "senders": vtx, "receivers": vrx}}

    def sender_sdp(self, s):
        ep = self.eps[s["id"]]
        if ep.video and ep.transport == "rtp":
            tp = ep.active["transport_params"][0]
            return sdp.jxsv_sender_sdp(s, tp.get("source_ip") or self.n.ip, tp.get("destination_ip") or self.n.ip,
                                       tp.get("destination_port") or s["port"], version=int(ep.version.split(":")[0]),
                                       localmac=self.n.mac)
        if ep.transport == "srt":
            tp = ep.active["transport_params"][0]
            return sdp.video_sender_sdp(s, tp.get("source_ip") or self.n.ip, tp.get("source_port") or s["port"],
                                        version=int(ep.version.split(":")[0]))
        tp = ep.active["transport_params"][0]
        live = dict(s, group=tp.get("destination_ip") or s["group"], port=tp.get("destination_port") or s["port"])
        gm = gmid_nmos(self.ptp().get("gm")) if self.n.phc_if else None
        audio = read_json(config.AUDIO_STATUS) or {}
        rtp = ((audio.get("senders") or {}).get(s["key"]) or {}).get("rtp")
        return sdp.sender_sdp(live, tp.get("source_ip") or self.n.ip, gm, self.n.ptp_domain,
                              version=int(ep.version.split(":")[0]), rtp=rtp,
                              localmac=None if self.n.phc_if else (self.n.mac or "00-00-00-00-00-00"))

    # IS-04 v1.x deprecated receiver target (PUT /receivers/{id}/target) -> an immediate IS-05 activation
    def put_target(self, rid, body):
        ep = self.eps[rid]
        if not body:
            patch = {"sender_id": None, "master_enable": False, "activation": {"mode": "activate_immediate"}}
        else:
            href = body.get("manifest_href")
            code, data = self._http("GET", href) if href else (0, b"")
            if code != 200:
                raise PatchError(400, f"cannot fetch the sender's manifest {href}: HTTP {code}")
            patch = {"sender_id": body.get("id"), "master_enable": True, "activation": {"mode": "activate_immediate"},
                     "transport_file": {"data": data.decode(), "type": "application/sdp"}}
        ep.patch(patch, self.on_activate)


# ==================================================================== HTTP
class Handler(BaseHTTPRequestHandler):
    agent = None
    server_version = "owl-node/" + VERSION
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if os.environ.get("OWL_NODE_HTTP_LOG"):
            log("http", self.address_string(), fmt % args)

    def _send(self, code, body=None, ctype="application/json", extra=None):
        if isinstance(body, (dict, list)) or (body is None and ctype == "application/json" and code != 204):
            data = json.dumps(body if body is not None else {}).encode()
        elif isinstance(body, str):
            data = body.encode()
        else:
            data = body or b""
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, PUT, POST, PATCH, HEAD, OPTIONS, DELETE")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Accept, Authorization")
        self.send_header("Access-Control-Max-Age", "3600")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _err(self, code, msg):
        self._send(code, {"code": code, "error": msg, "debug": None})

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            raise PatchError(400, "body is not valid JSON")

    def _parts(self):
        path = urlsplit(self.path).path
        return [p for p in path.split("/") if p]

    def do_OPTIONS(self):
        self._send(200, b"", "text/plain")

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        self._wrap("GET")

    def do_PATCH(self):
        self._wrap("PATCH")

    def do_POST(self):
        self._wrap("POST")

    def do_PUT(self):
        self._wrap("PUT")

    def do_DELETE(self):
        self._wrap("DELETE")

    def _wrap(self, method):
        try:
            self.route(method)
        except PatchError as e:
            self._err(e.code, str(e))
        except Exception as e:
            traceback.print_exc()
            self._err(500, f"{type(e).__name__}: {e}")

    # -------------------------------------------------------------- router
    def route(self, method):
        a, p = self.agent, self._parts()
        if not p:
            return self._send(200, ["x-nmos/", "x-owl/"]) if method == "GET" else self._err(405, "method not allowed")
        if p[0] == "x-owl":
            if p == ["x-owl"]:
                return self._send(200, ["v1/"])
            if p[1:] in (["v1"],):
                return self._send(200, ["status/"])
            if p[1:] == ["v1", "status"]:
                return self._send(200, a.status())
            return self._err(404, "not found")
        if p[0] != "x-nmos":
            return self._err(404, "not found")
        if len(p) == 1:
            return self._send(200, ["node/", "connection/"])
        if p[1] == "node":
            return self.node_api(method, p[2:])
        if p[1] == "connection":
            return self.conn_api(method, p[2:])
        return self._err(404, "not found")

    def node_api(self, method, p):
        a = self.agent
        if not p:
            return self._send(200, [IS04 + "/"])
        if p[0] != IS04:
            return self._err(404, f"API version {p[0]} not supported")
        p = p[1:]
        if method == "PUT" and len(p) == 3 and p[0] == "receivers" and p[2] == "target":
            if p[1] not in a.eps or a.eps[p[1]].kind != "receiver":
                return self._err(404, "no such receiver")
            body = self._body() or {}
            a.put_target(p[1], body)
            return self._send(202, body)
        if method != "GET" and method != "HEAD":
            return self._err(405, "method not allowed")
        if not p:
            return self._send(200, ["self/", "sources/", "flows/", "devices/", "senders/", "receivers/"])
        n = a.n
        coll = {}
        for typ, data in a.resources()[1:]:
            coll.setdefault(typ + "s", []).append(data)
        for k in ("sources", "flows", "devices", "senders", "receivers"):
            coll.setdefault(k, [])
        if p[0] == "self" and len(p) == 1:
            return self._send(200, a.r_node())
        if p[0] in coll:
            if len(p) == 1:
                return self._send(200, coll[p[0]])
            hit = [x for x in coll[p[0]] if x["id"] == p[1]]
            if len(p) == 2 and hit:
                return self._send(200, hit[0])
        return self._err(404, "not found")

    def conn_api(self, method, p):
        a = self.agent
        if not p:
            return self._send(200, [IS05 + "/"])
        if p[0] != IS05:
            return self._err(404, f"API version {p[0]} not supported")
        p = p[1:]
        if not p:
            return self._send(200, ["bulk/", "single/"])
        if p[0] == "bulk":
            if len(p) == 1:
                return self._send(200, ["senders/", "receivers/"])
            if len(p) == 2 and p[1] in ("senders", "receivers"):
                if method != "POST":
                    return self._err(405, "bulk endpoints only accept POST")
                return self.bulk(p[1][:-1])
            return self._err(404, "not found")
        if p[0] != "single":
            return self._err(404, "not found")
        if len(p) == 1:
            return self._send(200, ["senders/", "receivers/"])
        kind = {"senders": "sender", "receivers": "receiver"}.get(p[1])
        if not kind:
            return self._err(404, "not found")
        eps = [e for e in a.eps.values() if e.kind == kind]
        if len(p) == 2:
            return self._send(200, [e.id + "/" for e in eps])
        ep = a.eps.get(p[2])
        if not ep or ep.kind != kind:
            return self._err(404, f"no such {kind}")
        if len(p) == 3:
            items = ["constraints/", "staged/", "active/", "transporttype/"] + (["transportfile/"] if kind == "sender" else [])
            return self._send(200, items)
        leaf = p[3]
        if leaf == "staged" and method == "PATCH":
            body = self._body()
            code, view = ep.patch(body, a.on_activate)
            return self._send(code, view)
        if method not in ("GET", "HEAD"):
            return self._err(405, "method not allowed")
        if leaf == "constraints":
            return self._send(200, ep.constraints)
        if leaf == "staged":
            return self._send(200, ep.view_staged())
        if leaf == "active":
            return self._send(200, ep.view_active())
        if leaf == "transporttype":
            return self._send(200, json.dumps(ep.transport_type).encode())  # IS-05: base type; .mcast is IS-04 only
        if leaf == "transportfile" and kind == "sender":
            return self._send(200, a.sender_sdp(ep.cfg), "application/sdp")
        return self._err(404, "not found")

    def bulk(self, kind):
        a = self.agent
        body = self._body()
        if not isinstance(body, list):
            raise PatchError(400, "bulk body must be an array of {id, params}")
        out = []
        for item in body:
            rid = item.get("id") if isinstance(item, dict) else None
            ep = a.eps.get(rid)
            if not isinstance(item, dict) or "params" not in item or not rid:
                out.append({"id": rid, "code": 400, "error": "each item needs id and params"})
                continue
            if not ep or ep.kind != kind:
                out.append({"id": rid, "code": 404, "error": f"no such {kind}"})
                continue
            try:
                code, _ = ep.patch(item["params"], a.on_activate)
                out.append({"id": rid, "code": code})
            except PatchError as e:
                out.append({"id": rid, "code": e.code, "error": str(e)})
        return self._send(200, out)


# ==================================================================== mDNS
class MDNS:
    """Browse _nmos-register._tcp (and the legacy _nmos-registration._tcp); advertise _nmos-node._tcp."""

    def __init__(self, agent):
        from zeroconf import IPVersion, ServiceBrowser, Zeroconf
        self.a = agent
        self.zc = Zeroconf(interfaces=[agent.n.ip], ip_version=IPVersion.V4Only)
        self.found = {}
        self.info = None
        self.mode = None
        self.browsers = [ServiceBrowser(self.zc, t, handlers=[self._on]) for t in
                         ("_nmos-register._tcp.local.", "_nmos-registration._tcp.local.")]

    def _on(self, zeroconf, service_type, name, state_change):
        from zeroconf import ServiceStateChange
        if state_change is ServiceStateChange.Removed:
            self.found.pop(name, None)
            return
        info = zeroconf.get_service_info(service_type, name, 2000)
        if not info:
            return
        txt = {k.decode(): (v or b"").decode() for k, v in (info.properties or {}).items()}
        if txt.get("api_proto", "http") != "http" or IS04 not in txt.get("api_ver", IS04).split(","):
            return
        addrs = info.parsed_addresses()
        if addrs:
            self.found[name] = (int(txt.get("pri", 100)), f"http://{addrs[0]}:{info.port}")
            log(f"mDNS: registry {name} -> {self.found[name]}")

    def registries(self):
        return [u for _, u in sorted(self.found.values())]

    def advertise(self, registered):
        """IS-04 v1.3: a Node advertises _nmos-node._tcp only in peer-to-peer mode (no registry reachable)."""
        from zeroconf import ServiceInfo
        if self.mode == registered:
            return
        a = self.a
        try:
            if registered:
                if self.info:
                    self.zc.unregister_service(self.info)
                    self.info = None
            elif not self.info:
                txt = {"api_proto": "http", "api_ver": IS04, "api_auth": "false",
                       "ver_slf": "0", "ver_src": "0", "ver_flw": "0", "ver_dvc": "0", "ver_snd": "0", "ver_rcv": "0"}
                info = ServiceInfo("_nmos-node._tcp.local.", f"owl-node-{a.n.name}._nmos-node._tcp.local.",
                                   addresses=[socket.inet_aton(a.n.ip)], port=a.n.http_port, properties=txt,
                                   server=f"owl-node-{a.n.name}.local.")
                self.zc.register_service(info, allow_name_change=True)
                self.info = info
            self.mode = registered
        except Exception as e:
            log(f"mDNS advertise failed: {e}")

    def close(self):
        try:
            if self.info:
                self.zc.unregister_service(self.info)
            self.zc.close()
        except Exception:
            pass


def main():
    node = config.Node()
    if not node.ip:
        sys.exit(f"interface {node.media_if} has no IPv4 address")
    agent = Agent(node)
    agent.mdns = None
    if os.environ.get("OWL_NODE_MDNS", "1") != "0":
        try:
            agent.mdns = MDNS(agent)
        except Exception as e:
            log(f"mDNS disabled: {e}")
    Handler.agent = agent
    srv = ThreadingHTTPServer(("0.0.0.0", node.http_port), Handler)
    srv.daemon_threads = True
    log(f"owl-node {VERSION} agent: node {node.name} ({node.node_id}) on {agent.base}, "
        f"{len(node.senders)} sender(s), {len(node.receivers)} receiver(s), registry {(['paired: ' + agent.paired_registry()] if agent.paired_registry() else []) + list(node.registries) or 'mDNS (not paired yet: add this node in the hub designer)'}")
    threading.Thread(target=agent.registrar, daemon=True, name="registrar").start()
    import signal

    def stop(*_):
        log("stopping: unregistering")
        agent.unregister()
        if agent.mdns:
            agent.mdns.close()
        os._exit(0)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    srv.serve_forever()
