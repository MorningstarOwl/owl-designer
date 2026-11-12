"""The `video-enc` and `video-dec` roles (N3, video tier 1): the audio supervisor's pattern for GStreamer.

  video-enc   one `vpipe` process per enabled video sender: source -> VA-API H.265/H.264 -> RTP (or MPEG-TS) over
              an SRT listener, plus a 640x360 H.264 preview published to MediaMTX on the hub (WebRTC in the browser)
  video-dec   one `vpipe` process per *patched* video receiver: SRT caller -> decode (v4l2slh265dec on the Pi 5,
              VA-API on x86, libav otherwise) -> kmssink on an attached display, or the headless verifier

N4 (JPEG XS, RTP over UDP): a JPEG XS sender with `source_of: <sender>` is one more output of that sender's process
(one source, two flows: the capture device can only be opened once, and the preview is shared); one without it gets a
process of its own, run only while it has somewhere to send. Its destination and on/off are *dynamic*: the supervisor
writes them to <process>.dyn.json and the running process re-targets its udpsink and opens/closes its valve, so a
patch never restarts the H.265 stream next to it. A JPEG XS receiver is udpsrc -> rtpjxsvdepay -> svtjpegxsdec.

Desired state = node.yaml + the IS-05 active parameters the agent writes to state.json (video_senders /
video_receivers), so patches come back after any restart. Each process writes its own status file; this supervisor
merges them into /run/owl-node/video-<enc|dec>.json for the agent's /x-owl/v1/status.
"""
import hashlib, json, os, signal, subprocess, sys, time

from . import config, sdp
from .config import read_json

VDIR = os.path.join(config.RUN_DIR, "video")


def log(role, *a):
    print(time.strftime("%H:%M:%S"), f"video-{role}:", *a, flush=True)


def clock_dev():
    d = os.environ.get("OWL_PTP_DEV")
    return d if d and d != "/dev/null" and os.path.exists(d) else None


def enc_spec(n, s, a):
    tp = (a.get("transport_params") or [{}])[0]
    spec = {"kind": "enc", "name": f"tx-{s['key']}", "label": s["label"], "source": s["source"], "codec": s["codec"],
            "encap": s["encap"], "bitrate_kbps": s["bitrate_kbps"], "gop": s["gop"],
            "port": int(tp.get("source_port") or s["port"]), "latency_ms": int(tp.get("latency") or s["latency_ms"]),
            "render_node": n.render_node, "clock_dev": clock_dev()}
    if s["preview"]:
        spec["preview"] = {"host": n.preview["host"], "port": n.preview["port"], "path": s["preview_path"]}
    return spec


def jxs_static(n, s):
    """The part of a JPEG XS output that needs a pipeline (re)build."""
    return {k: s[k] for k in ("key", "label", "bpp", "mtu", "profile", "level", "sublevel", "gst_format", "threads",
                              "multicast", "ttl", "echo_port", "port")} | {"media_if": n.media_if, "own_ip": n.ip}


def jxs_dyn(n, s, a):
    """The part that follows IS-05 activations at run time: where to send, and whether to send at all. Unicast
    'auto' resolves to this node's own address, which means parked."""
    tp = (a.get("transport_params") or [{}])[0]
    dst, port = tp.get("destination_ip"), tp.get("destination_port") or s["port"]
    on = bool(a.get("master_enable")) and tp.get("rtp_enabled", True) is not False and bool(dst) and dst != n.ip
    return {"host": dst if on else None, "port": int(port) if isinstance(port, int) else s["port"], "on": on,
            "receiver_id": a.get("receiver_id")}


def jxs_enc_spec(n, s):
    """A process whose own sender is JPEG XS (no H.265/SRT output)."""
    spec = {"kind": "enc", "name": f"tx-{s['key']}", "label": s["label"], "source": s["source"], "codec": "jxs",
            "render_node": n.render_node, "clock_dev": clock_dev()}
    if s["preview"]:
        spec["preview"] = {"host": n.preview["host"], "port": n.preview["port"], "path": s["preview_path"]}
    return spec


def dec_spec(n, r, a):
    tp = a["transport_params"][0]
    if r["transport"] == "rtp":         # N4 JPEG XS over RTP/UDP
        f = a.get("format") or {}
        echo = r.get("echo")
        if echo is None and f.get("echo_port") and (f.get("origin_ip") or f.get("source_ip")):
            echo = f"{f.get('origin_ip') or f.get('source_ip')}:{f['echo_port']}"
        return {"kind": "dec", "name": f"rx-{r['key']}", "codec": "jxs", "port": int(tp["destination_port"]),
                "group": tp.get("multicast_ip"), "source_ip": tp.get("source_ip"), "media_if": n.media_if,
                "caps": sdp.jxsv_caps(f), "fmtp": f.get("fmtp"), "display": r["display"], "verify": r["verify"],
                "decoder": r["decoder"], "kms_driver": r["kms_driver"], "connector_id": r["connector_id"],
                "ref_pattern": f.get("ref_pattern"), "clock_dev": clock_dev(), "sender_id": a.get("sender_id"),
                "echo": echo or None}
    f = a.get("format") or {}
    return {"kind": "dec", "name": f"rx-{r['key']}", "source_ip": tp["source_ip"], "source_port": int(tp["source_port"]),
            "latency_ms": int(tp.get("latency") or r["latency_ms"]), "encap": f.get("encap") or "rtp",
            "encoding": f.get("encoding") or "H265", "payload": f.get("payload") or 96,
            "streamid": f.get("streamid") or tp.get("stream_id"),
            "display": r["display"], "verify": r["verify"], "decoder": r["decoder"], "kms_driver": r["kms_driver"],
            "connector_id": r["connector_id"], "ref_pattern": f.get("ref_pattern"), "clock_dev": clock_dev(),
            "sender_id": a.get("sender_id")}


class Proc:
    def __init__(self, role, name, spec):
        self.role, self.name, self.spec = role, name, spec
        self.p, self.since, self.restarts, self.next_try, self.exits = None, None, 0, 0, 0
        self.path = os.path.join(VDIR, f"{name}.json")
        self.status_path = os.path.join(VDIR, f"{name}.status.json")
        self.spec["status"] = self.status_path

    @property
    def digest(self):
        return hashlib.sha1(json.dumps(self.spec, sort_keys=True).encode()).hexdigest()[:10]

    def running(self):
        return self.p is not None and self.p.poll() is None

    def start(self):
        config.atomic_write(self.path, self.spec)
        try:
            os.unlink(self.status_path)
        except OSError:
            pass
        log(self.role, f"start {self.name} ({self.digest})")
        self.p = subprocess.Popen([sys.executable, "-m", "owlnode.vpipe", self.path], stdout=sys.stdout,
                                  stderr=sys.stderr, start_new_session=True)
        self.since = time.time()

    def stop(self):
        if self.p and self.p.poll() is None:
            log(self.role, f"stop {self.name}")
            self.p.terminate()
            try:
                self.p.wait(5)
            except subprocess.TimeoutExpired:
                self.p.kill()
                self.p.wait()
        self.p = None

    def check(self):
        """Restart after an exit, with backoff (2, 4, ... 30 s). A decoder exits when its sender goes away."""
        if self.running():
            if time.time() - self.since > 60:
                self.restarts = 0
            return
        if self.p is not None:
            log(self.role, f"{self.name} exited with {self.p.returncode}")
            self.p = None
            self.restarts += 1
            self.exits += 1
            self.next_try = time.time() + min(30, 2 ** self.restarts)
        if time.time() >= self.next_try:
            self.start()

    def info(self):
        st = read_json(self.status_path) or {}
        if st and time.time() - st.get("time", 0) > 10:
            st["stale"] = True
        return dict(st, running=self.running(), up_s=round(time.time() - self.since) if self.running() else 0,
                    restarts=self.restarts, exits=self.exits, spec=self.digest)


def gst_facts():
    """GStreamer version and which hardware codecs this node really has (va/v4l2codecs register only with devices)."""
    out = {"gst": None, "va": False, "decoders": [], "encoders": []}
    try:
        import gi
        gi.require_version("Gst", "1.0")
        from gi.repository import Gst
        Gst.init(None)
        out["gst"] = Gst.version_string().split()[-1]
        for f in ("vah265enc", "vah264enc", "x264enc", "svtjpegxsenc"):
            if Gst.ElementFactory.find(f):
                out["encoders"].append(f)
        for f in ("v4l2slh265dec", "v4l2slh264dec", "vah265dec", "vah264dec", "avdec_h265", "avdec_h264", "svtjpegxsdec"):
            if Gst.ElementFactory.find(f):
                out["decoders"].append(f)
        out["va"] = bool(Gst.ElementFactory.find("vapostproc"))
        out["rtp_jxsv"] = bool(Gst.ElementFactory.find("rtpjxsvpay") and Gst.ElementFactory.find("rtpjxsvdepay"))
    except Exception as e:
        out["error"] = str(e)
    return out


def main(role):
    n = config.Node()
    os.makedirs(VDIR, exist_ok=True)
    facts = gst_facts()
    log(role, f"node {n.name}: GStreamer {facts['gst']}, VA-API {facts['va']}, encoders {facts['encoders']}, "
              f"decoders {facts['decoders']}, PTP clock {clock_dev() or 'none (CLOCK_REALTIME)'}")
    procs, stopping = {}, []
    signal.signal(signal.SIGTERM, lambda *_: stopping.append(1))
    signal.signal(signal.SIGINT, lambda *_: stopping.append(1))
    last_status = 0
    while not stopping:
        st = read_json(config.STATE, {}) or {}
        want = {}
        dyns = {}
        if role == "enc":
            vst = st.get("video_senders") or {}
            for s in n.vsenders:
                if s.get("source_of"):
                    continue                    # an extra output of its source's process
                jx = ([s] if s["codec"] == "jxs" else []) + [x for x in n.vsenders if x.get("source_of") == s["key"]]
                dyn = {x["key"]: jxs_dyn(n, x, vst.get(x["key"]) or {}) for x in jx}
                if s["codec"] == "jxs":
                    run = any(d["on"] for d in dyn.values())   # a JPEG XS-only process runs while it has a receiver
                    spec = jxs_enc_spec(n, s) if run else None
                else:
                    a = vst.get(s["key"]) or {"master_enable": True, "transport_params": [{}]}
                    run = a.get("master_enable", True) or any(d["on"] for d in dyn.values())
                    spec = enc_spec(n, s, a) if run else None
                if spec:
                    if jx:
                        spec["jxs"] = [jxs_static(n, x) for x in jx]
                        spec["dyn"] = os.path.join(VDIR, f"tx-{s['key']}.dyn.json")
                        dyns[spec["dyn"]] = dyn
                    want[f"tx-{s['key']}"] = spec
        else:
            for r in n.vreceivers:
                a = (st.get("video_receivers") or {}).get(r["key"])
                if not a or not a.get("master_enable"):
                    continue
                tp = (a.get("transport_params") or [{}])[0]
                if r["transport"] == "rtp":
                    if tp.get("rtp_enabled", True) and tp.get("destination_port") and (a.get("format") or {}).get("fmtp"):
                        want[f"rx-{r['key']}"] = dec_spec(n, r, a)
                elif tp.get("source_ip") and tp.get("source_port"):
                    want[f"rx-{r['key']}"] = dec_spec(n, r, a)
        for path, dyn in dyns.items():          # before any (re)start, so a new process starts in the right state
            if read_json(path) != dyn:
                config.atomic_write(path, dyn)
        for name in list(procs):
            if name not in want or procs[name].digest != Proc(role, name, dict(want[name])).digest:
                procs.pop(name).stop()
        for name, spec in want.items():
            if name not in procs:
                procs[name] = Proc(role, name, spec)
            procs[name].check()
        if time.time() - last_status > 2:
            last_status = time.time()
            keyed = {name.split("-", 1)[1]: p.info() for name, p in procs.items()}
            config.atomic_write(config.VIDEO_STATUS.format(role), dict(facts, time=time.time(), streams=keyed))
        time.sleep(0.25)
    log(role, "stopping all pipelines")
    for p in procs.values():
        p.stop()
