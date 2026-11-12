"""AMWA IS-05 v1.1 connection management for one node: constraints, staged/active, activations, bulk.

Activation calls `on_activate(kind, endpoint)`, which persists the new active parameters (state.json, read by the
audio role) and updates the IS-04 subscription. Pure state machine: no HTTP here.
"""
import copy, ipaddress, re, threading

from . import sdp
from .config import AES67_PORT, new_version, parse_tai, tai_now, tai_str

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
MODES = (None, "activate_immediate", "activate_scheduled_absolute", "activate_scheduled_relative")
TAI_RE = re.compile(r"^[0-9]+:[0-9]+$")
SENDER_KEYS = ("source_ip", "destination_ip", "source_port", "destination_port", "rtp_enabled")
RECEIVER_KEYS = ("source_ip", "multicast_ip", "interface_ip", "destination_port", "rtp_enabled")
# SRT (N3 video), as Matrox's "NMOS With SRT" defines it (urn:x-matrox:transport:srt; AMWA has no SRT transport yet):
# senders are listeners, receivers callers; Stream ID unsupported (constraint enum [null])
SRT_KEYS = ("source_ip", "source_port", "destination_ip", "destination_port", "protocol", "latency", "stream_id")
TRANSPORT_TYPE = {"rtp": "urn:x-nmos:transport:rtp", "srt": "urn:x-matrox:transport:srt"}


class PatchError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


def null_activation():
    return {"mode": None, "requested_time": None, "activation_time": None}


def _is_ip(v):
    try:
        ipaddress.IPv4Address(v)
        return True
    except (ValueError, TypeError):
        return False


class Endpoint:
    def __init__(self, kind, cfg, node, saved=None):
        self.kind, self.cfg, self.node = kind, cfg, node
        # audio: RTP (AES67). video: SRT (N3, H.265/H.264), or RTP over UDP (N4 JPEG XS, cfg transport "rtp")
        self.transport = cfg.get("transport") or ("srt" if cfg.get("kind") == "video" else "rtp")
        self.video = cfg.get("kind") == "video"
        self.id, self.key = cfg["id"], cfg["key"]
        self.lock = threading.RLock()
        self.timer = None
        ip = node.ip or "0.0.0.0"
        if self.transport == "srt":
            lat = {"minimum": 20, "maximum": 1000}
            if kind == "sender":
                self.constraints = [{"source_ip": {"enum": [ip]}, "source_port": {"minimum": 1024, "maximum": 65535},
                                     "destination_ip": {}, "destination_port": {}, "protocol": {"enum": ["listener"]},
                                     "latency": lat, "stream_id": {"enum": [None]}}]
                self.staged = {"receiver_id": None, "master_enable": True, "activation": null_activation(),
                               "transport_params": [{"source_ip": "auto", "source_port": "auto", "destination_ip": None,
                                                     "destination_port": "auto", "protocol": "listener",
                                                     "latency": cfg["latency_ms"], "stream_id": None}]}
            else:
                self.constraints = [{"source_ip": {}, "source_port": {"minimum": 1, "maximum": 65535},
                                     "destination_ip": {"enum": [ip]}, "destination_port": {},
                                     # stream_id: an SRT listener that names its streams (MediaMTX read:<path>, the
                                     # hub's RTSP camera relay) needs it; any string or null
                                     "protocol": {"enum": ["caller"]}, "latency": lat, "stream_id": {}}]
                self.staged = {"sender_id": None, "master_enable": False, "activation": null_activation(),
                               "transport_file": {"data": None, "type": None},
                               "transport_params": [{"source_ip": None, "source_port": "auto", "destination_ip": "auto",
                                                     "destination_port": "auto", "protocol": "caller",
                                                     "latency": cfg["latency_ms"], "stream_id": None}]}
        elif kind == "sender":
            self.constraints = [{
                "source_ip": {"enum": [ip]},
                "destination_ip": {},
                "source_port": {"minimum": 1024, "maximum": 65535},
                "destination_port": {"minimum": 1024, "maximum": 65535},
                "rtp_enabled": {},
            }]
            # an N4 unicast JPEG XS sender has nowhere to send until a controller sets destination_ip: it starts disabled
            self.staged = {"receiver_id": None, "master_enable": not (self.video and not cfg.get("multicast")),
                           "activation": null_activation(),
                           "transport_params": [{"source_ip": "auto", "destination_ip": "auto", "source_port": "auto",
                                                 "destination_port": "auto", "rtp_enabled": True}]}
        else:
            self.constraints = [{
                "source_ip": {},
                "multicast_ip": {},
                "interface_ip": {"enum": [ip]},
                "destination_port": {"minimum": 1024, "maximum": 65535},
                "rtp_enabled": {},
            }]
            self.staged = {"sender_id": None, "master_enable": False, "activation": null_activation(),
                           "transport_file": {"data": None, "type": None},
                           "transport_params": [{"source_ip": None, "multicast_ip": None, "interface_ip": "auto",
                                                 "destination_port": "auto", "rtp_enabled": True}]}
        self.active = self.resolve(copy.deepcopy(self.staged))
        if saved:                                   # restore the last active state (survives restarts/reboots)
            for k in ("master_enable", "receiver_id", "sender_id", "transport_file"):
                if k in saved and k in self.active:
                    self.active[k] = saved[k]
            if saved.get("transport_params"):
                self.active["transport_params"] = [dict(self.active["transport_params"][0], **{
                    k: v for k, v in saved["transport_params"][0].items() if k in self.constraints[0]})]
            self.active["activation"] = saved.get("activation") or null_activation()
            if self.kind == "receiver" and self.active.get("transport_file", {}).get("data"):
                try:                        # anything still 'auto' in a saved state is resolved again
                    self.active["_sdp"] = self.parse_sdp(self.active["transport_file"]["data"])
                except sdp.SDPError:
                    pass
            self.active = self.resolve(self.active)
            st = copy.deepcopy(self.active)
            st["activation"] = null_activation()
            self.staged = st
        self.version = new_version()

    # ---- resolution of "auto" -------------------------------------------------------------------------
    def resolve(self, p):
        tp = p["transport_params"][0]
        ip = self.node.ip
        if self.transport == "srt":
            fmt = p.get("_sdp") or {}
            if self.kind == "sender":       # a listener: no peer address; the port is the one it listens on
                for k, v in (("source_ip", ip), ("source_port", self.cfg["port"]), ("destination_port", 0),
                             ("destination_ip", None)):
                    if tp.get(k) == "auto":
                        tp[k] = v
            else:                           # a caller: the sender's address from its SDP; local port ephemeral
                for k, v in (("destination_ip", ip), ("destination_port", 0), ("source_port", fmt.get("port") or 0)):
                    if tp.get(k) == "auto":
                        tp[k] = v
                if tp.get("source_ip") == "auto":
                    tp["source_ip"] = fmt.get("source_ip")
            p.pop("_sdp", None)
            return p
        if self.kind == "sender":
            # unicast JPEG XS: 'auto' = this node itself, which the video role treats as parked (it sends nothing)
            dest = self.cfg["group"] if not self.video or self.cfg.get("multicast") else ip
            for k, v in (("source_ip", ip), ("destination_ip", dest),
                         ("source_port", self.cfg["port"]), ("destination_port", self.cfg["port"])):
                if tp.get(k) == "auto":
                    tp[k] = v
        else:
            fmt = p.get("_sdp")
            if tp.get("interface_ip") == "auto":
                tp["interface_ip"] = ip
            if tp.get("destination_port") == "auto":
                tp["destination_port"] = (fmt or {}).get("port") or self.cfg.get("port") or AES67_PORT
            for k, f in (("multicast_ip", "group"), ("source_ip", "source_ip")):
                if tp.get(k) == "auto":
                    tp[k] = (fmt or {}).get(f)
        p.pop("_sdp", None)
        return p

    # ---- validation -----------------------------------------------------------------------------------
    def _check_params(self, params):
        if not isinstance(params, list) or len(params) != len(self.constraints):
            raise PatchError(400, f"transport_params must be an array of {len(self.constraints)} object(s)")
        allowed = SRT_KEYS if self.transport == "srt" else SENDER_KEYS if self.kind == "sender" else RECEIVER_KEYS
        for leg, (tp, con) in enumerate(zip(params, self.constraints)):
            if not isinstance(tp, dict):
                raise PatchError(400, "transport_params entries must be objects")
            for k, v in tp.items():
                if k not in allowed:
                    raise PatchError(400, f"unknown transport parameter {k!r}")
                if self.transport == "srt" and k in ("protocol", "latency", "stream_id", "destination_ip"):
                    if k == "protocol" and v not in ("caller", "listener", "rendezvous"):
                        raise PatchError(400, "protocol must be caller, listener or rendezvous")
                    if k == "latency" and (not isinstance(v, int) or isinstance(v, bool) or not 0 <= v <= 1000):
                        raise PatchError(400, "latency must be an integer 0..1000 (ms)")
                    if k == "stream_id" and v is not None and not isinstance(v, str):
                        raise PatchError(400, "stream_id must be a string or null")
                    if k == "destination_ip" and v not in ("auto", None) and not _is_ip(v):
                        raise PatchError(400, "destination_ip must be an IPv4 address, 'auto' or null")
                elif k == "rtp_enabled":
                    if not isinstance(v, bool):
                        raise PatchError(400, "rtp_enabled must be a boolean")
                elif k.endswith("_port"):
                    if v != "auto" and (not isinstance(v, int) or isinstance(v, bool) or not 0 <= v <= 65535):
                        raise PatchError(400, f"{k} must be an integer port or 'auto'")
                else:
                    nullable = (self.kind == "receiver" and k in ("source_ip", "multicast_ip")) or (
                        self.transport == "srt" and k == "source_ip" and self.kind == "receiver")
                    if v is None and nullable:
                        pass
                    elif v != "auto" and not _is_ip(v):
                        raise PatchError(400, f"{k} must be an IPv4 address or 'auto'" + (" or null" if nullable else ""))
                c = con.get(k, {})
                if "enum" in c and v is None and None not in c["enum"] and k != "destination_ip":
                    raise PatchError(400, f"{k}=null violates constraint enum {c['enum']}")
                if v == "auto" and k in ("protocol", "latency", "stream_id"):
                    raise PatchError(400, f"{k} cannot be 'auto'")
                if v not in ("auto", None):
                    if "enum" in c and v not in c["enum"]:
                        raise PatchError(400, f"{k}={v!r} violates constraint enum {c['enum']}")
                    if "minimum" in c and isinstance(v, int) and v < c["minimum"]:
                        raise PatchError(400, f"{k}={v} below constraint minimum {c['minimum']}")
                    if "maximum" in c and isinstance(v, int) and v > c["maximum"]:
                        raise PatchError(400, f"{k}={v} above constraint maximum {c['maximum']}")
        return params

    def _check_activation(self, a):
        if not isinstance(a, dict):
            raise PatchError(400, "activation must be an object")
        for k in a:
            if k not in ("mode", "requested_time"):
                raise PatchError(400, f"unknown activation key {k!r}")
        mode = a.get("mode")
        if mode not in MODES:
            raise PatchError(400, f"unknown activation mode {mode!r}")
        rt = a.get("requested_time")
        if rt is not None and (not isinstance(rt, str) or not TAI_RE.match(rt)):
            raise PatchError(400, "requested_time must be 'seconds:nanoseconds' or null")
        if mode in ("activate_scheduled_absolute", "activate_scheduled_relative") and rt is None:
            raise PatchError(400, f"{mode} needs a requested_time")
        return mode, rt

    # ---- PATCH staged -----------------------------------------------------------------------------------
    def patch(self, body, on_activate):
        """Returns (http_code, staged_view)."""
        if not isinstance(body, dict):
            raise PatchError(400, "body must be a JSON object")
        other = "receiver_id" if self.kind == "sender" else "sender_id"
        allowed = {other, "master_enable", "activation", "transport_params"} | (
            {"transport_file"} if self.kind == "receiver" else set())
        for k in body:
            if k not in allowed:
                raise PatchError(400, f"unknown property {k!r}")
        with self.lock:
            pending = self.staged["activation"]["mode"] in ("activate_scheduled_absolute", "activate_scheduled_relative")
            act = body.get("activation")
            if pending:
                if act is not None and isinstance(act, dict) and act.get("mode") is None and set(body) == {"activation"}:
                    self._cancel()
                    return 200, self.view_staged()
                raise PatchError(423, "a scheduled activation is pending; PATCH activation mode null to cancel it")
            new = copy.deepcopy(self.staged)
            if other in body:
                v = body[other]
                if v is not None and (not isinstance(v, str) or not UUID_RE.match(v)):
                    raise PatchError(400, f"{other} must be a UUID or null")
                new[other] = v
            if "master_enable" in body:
                if not isinstance(body["master_enable"], bool):
                    raise PatchError(400, "master_enable must be a boolean")
                new["master_enable"] = body["master_enable"]
            explicit = set()
            if "transport_params" in body:
                self._check_params(body["transport_params"])
                for leg, tp in enumerate(body["transport_params"]):
                    new["transport_params"][leg].update(tp)
                    explicit |= set(tp)
            if "transport_file" in body:
                tf = body["transport_file"]
                if not isinstance(tf, dict) or set(tf) - {"data", "type"}:
                    raise PatchError(400, "transport_file must be {data, type}")
                data, typ = tf.get("data"), tf.get("type")
                if data is not None:
                    if typ != "application/sdp":
                        raise PatchError(400, "transport_file.type must be application/sdp")
                    try:
                        f = self.parse_sdp(data)
                    except sdp.SDPError as e:
                        raise PatchError(400, f"transport file rejected: {e}")
                    tp = new["transport_params"][0]
                    pairs = ((("source_ip", f["source_ip"]), ("source_port", f["port"])) if self.transport == "srt" else
                             (("multicast_ip", f["group"]), ("source_ip", f["source_ip"]), ("destination_port", f["port"])))
                    for k, v in pairs:
                        if k not in explicit:
                            tp[k] = v
                    if self.transport == "rtp" and "interface_ip" not in explicit and tp.get("interface_ip") is None:
                        tp["interface_ip"] = "auto"
                new["transport_file"] = {"data": data, "type": typ if data is not None else None}
            mode, rt = (None, None)
            if act is not None:
                mode, rt = self._check_activation(act)
            new["activation"] = null_activation()
            self.staged = new
            if mode is None:
                return 200, self.view_staged()
            if mode == "activate_immediate":
                self.staged["activation"] = {"mode": mode, "requested_time": None, "activation_time": None}
                t = self._activate(on_activate)
                view = self.view_staged()
                view["activation"] = {"mode": mode, "requested_time": None, "activation_time": t}
                return 200, view
            now = tai_now()
            at = now + parse_tai(rt) if mode == "activate_scheduled_relative" else parse_tai(rt)
            self.staged["activation"] = {"mode": mode, "requested_time": rt, "activation_time": tai_str(at)}
            delay = max(0.0, (at - now) / 1e9)
            self.timer = threading.Timer(delay, self._scheduled, args=(on_activate,))
            self.timer.daemon = True
            self.timer.start()
            return 202, self.view_staged()

    def _cancel(self):
        if self.timer:
            self.timer.cancel()
            self.timer = None
        self.staged["activation"] = null_activation()

    def _scheduled(self, on_activate):
        with self.lock:
            if self.staged["activation"]["mode"] is None:
                return
            self._activate(on_activate)

    def _activate(self, on_activate):
        """staged -> active (with 'auto' resolved), then the side effects. Returns the activation time."""
        st = copy.deepcopy(self.staged)
        if self.kind == "receiver" and st["transport_file"].get("data"):
            st["_sdp"] = self.parse_sdp(st["transport_file"]["data"])
        mode, rt = st["activation"]["mode"], st["activation"]["requested_time"]
        t = tai_str()
        act = self.resolve(st)
        act["activation"] = {"mode": mode, "requested_time": rt, "activation_time": t}
        self.active = act
        self.staged["activation"] = null_activation()
        self.timer = None
        self.version = new_version()
        on_activate(self.kind, self)
        return t

    def parse_sdp(self, data):
        if self.transport == "srt":
            return sdp.parse_video(data, self.cfg.get("codecs"))
        return sdp.parse_jxsv(data) if self.video else sdp.parse(data)

    @property
    def transport_type(self):
        """IS-05 /transporttype: the base URN (subclassifications are IS-04 only)."""
        return TRANSPORT_TYPE[self.transport]

    # ---- views --------------------------------------------------------------------------------------------
    def view_staged(self):
        with self.lock:
            return copy.deepcopy(self.staged)

    def view_active(self):
        with self.lock:
            return copy.deepcopy(self.active)

    def persist(self):
        a = self.view_active()
        return a

    @property
    def subscribed(self):
        """IS-04 subscription view of the active state."""
        a = self.active
        on = bool(a["master_enable"])           # parked (master_enable false) reports no peer
        if self.kind == "sender":
            return {"receiver_id": a.get("receiver_id") if on else None, "active": on}
        return {"sender_id": a.get("sender_id") if on else None, "active": on}
