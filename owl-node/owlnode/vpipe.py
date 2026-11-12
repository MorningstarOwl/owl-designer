"""One video stream = one process. The `video` role's supervisor starts these:  python3 -m owlnode.vpipe SPEC.json

  enc  source -> [stamp] -> [overlays] -> encoder -> MPEG-TS -> SRT listener            (an NMOS sender)
                                      \\-> intervideo -> preview: 640x360 H.264 -> SRT publish to MediaMTX on the hub
                                      \\-> valve -> svtjpegxsenc -> rtpjxsvpay -> udpsink   (N4: 0..n JPEG XS senders)
  dec  SRT caller -> tsdemux -> parser -> decoder -> kmssink (a display) and/or the verifier (headless)
       udpsrc -> owlrtpstat -> rtpjxsvdepay -> svtjpegxsdec -> the same sinks                  (N4: JPEG XS over RTP)

N4 latency echo: a JPEG XS receiver sends each decoded frame's stamp back to the sender's echo port (16-byte UDP); the
sender looks up that frame's capture time (its own PHC, ns) and records now - capture. That is capture -> decoded on
the far node plus one small packet back (~0.1-0.2 ms on the LAN), with no clock sync on the receiving node at all.

Sources (`source.kind`): `testsrc` (videotestsrc + a machine-readable frame stamp + burnt-in clocks), `v4l2` (a UVC
capture stick, e.g. MS2130 at /dev/videoN: YUY2 or MJPEG), `kmsgrab` (reserved; see the plan). Encoders: VA-API
(vah265enc / vah264enc) when the node has a render node, else x264 in software.

Frame stamp: the top 24 rows of a stamped frame carry 64 bits as 24x24 luma blocks (black 0 / white 1): an 8-bit sync
0xA5, the capture time in ms (40 bits, mod 2^40) and a 16-bit frame counter. The time is read from the NIC's PTP
hardware clock when the node has one (the box is the grandmaster and pinode follows it at ~100 ns), so a decoder on
another PTP node measures true capture-to-decoded latency; nodes without a PHC fall back to CLOCK_REALTIME (NTP).
"""
import hashlib, json, os, signal, socket, struct, sys, threading, time

import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gio, GLib, Gst  # noqa: E402,F401  (Gio: SRT stats carry GInetSocketAddress)

SYNC = 0xA5
BITS = 64
BLK = 24
STATUS_EVERY = 1.0
ECHO = struct.Struct("!4sHQQ")      # b"OWLE", frame counter, stamp (ms), receiver clock at decode (ns, information only)


def log(*a):
    print(time.strftime("%H:%M:%S"), f"vpipe[{NAME[0]}]:", *a, flush=True)


NAME = ["?"]


# ------------------------------------------------------------------------------------------------ clock
class Clock:
    """PTP hardware clock (/dev/ptpN, read-only is enough for clock_gettime) or the system clock."""

    def __init__(self, dev=None):
        self.fd, self.kind = None, "realtime"
        if dev and dev != "/dev/null":
            try:
                self.fd = os.open(dev, os.O_RDONLY)
                self.cid = ((~self.fd) << 3) | 3          # FD_TO_CLOCKID
                time.clock_gettime_ns(self.cid)
                self.kind = f"phc {dev}"
            except OSError as e:
                log(f"PHC {dev} not readable ({e}); stamping with CLOCK_REALTIME")
                self.fd = None

    def ns(self):
        return time.clock_gettime_ns(self.cid) if self.fd is not None else time.time_ns()

    def ms(self):
        return self.ns() // 1_000_000


# ------------------------------------------------------------------------------------------------ stamp
def stamp_bits(ms, counter):
    v = (SYNC << 56) | ((ms & ((1 << 40) - 1)) << 16) | (counter & 0xFFFF)
    return [(v >> (63 - i)) & 1 for i in range(BITS)]


def stamp_row(width, ms, counter):
    """One luma row of the stamp band (black 16 / white 235 blocks)."""
    import numpy as np
    row = np.full(min(width, BITS * BLK), 16, dtype=np.uint8)
    for i, b in enumerate(stamp_bits(ms, counter)):
        if b:
            row[i * BLK:(i + 1) * BLK] = 235
    return row


class TestSource:
    """Renders the test pattern on a thread and pushes it into appsrc `tsrc` at the nominal rate: videotestsrc's
    SMPTE bars (static: the PSNR reference), a horizontally scrolling strip (motion for the encoder), and the stamp
    (capture time from the PHC + frame counter). The clocks and label are burnt in downstream by the pango overlays."""

    def __init__(self, appsrc, w, h, fps, clock, pattern="smpte", strip="pinwheel"):
        import numpy as np
        self.src, self.w, self.h, self.fps, self.clock = appsrc, w, h, fps, clock
        base = render_pattern(pattern, w, h, "NV12")
        st = render_pattern(strip, w, h, "NV12")
        self.y0, self.y1 = int(h * 0.45) & ~1, int(h * 0.70) & ~1
        self.base = base
        self.strip_y = st[:w * h].reshape(h, w)[self.y0:self.y1].copy()
        uv = st[w * h:].reshape(h // 2, w)
        self.strip_uv = uv[self.y0 // 2:self.y1 // 2].copy()
        self.counter = 0
        self.stop = False
        self.np = np
        self.cap_ns = np.zeros(65536, dtype=np.int64)      # capture time (clock ns) by frame counter, for the echo

    def run(self):
        np, w, h = self.np, self.w, self.h
        period = 1.0 / self.fps
        t_next = time.monotonic()
        step = max(2, w // 240) & ~1
        while not self.stop:
            frame = self.base.copy()
            y = frame[:w * h].reshape(h, w)
            uv = frame[w * h:].reshape(h // 2, w)
            sh = (self.counter * step) % w
            y[self.y0:self.y1] = np.roll(self.strip_y, sh, axis=1)
            uv[self.y0 // 2:self.y1 // 2] = np.roll(self.strip_uv, sh, axis=1)
            t_ns = self.clock.ns()
            self.cap_ns[self.counter & 0xFFFF] = t_ns
            row = stamp_row(w, t_ns // 1_000_000, self.counter)
            y[:BLK, :len(row)] = row
            uv[:BLK // 2, :len(row)] = 128
            self.src.emit("push-buffer", Gst.Buffer.new_wrapped(frame.tobytes()))
            self.counter += 1
            t_next += period
            d = t_next - time.monotonic()
            if d > 0:
                time.sleep(d)
            elif d < -1:            # fell far behind (suspended?): resync instead of bursting
                t_next = time.monotonic()


def render_pattern(pattern, w, h, fmt):
    """One frame of a videotestsrc pattern as a flat numpy array (tightly packed planes)."""
    import numpy as np
    p = Gst.parse_launch(f"videotestsrc pattern={pattern} num-buffers=1 ! video/x-raw,format={fmt},width={w},height={h} "
                         f"! appsink name=a sync=false")
    p.set_state(Gst.State.PLAYING)
    smp = p.get_by_name("a").emit("try-pull-sample", 5 * Gst.SECOND)
    b = smp.get_buffer()
    ok, inf = b.map(Gst.MapFlags.READ)
    arr = np.frombuffer(inf.data, dtype=np.uint8).copy()
    b.unmap(inf)
    p.set_state(Gst.State.NULL)
    need = w * h * 3 // 2 if fmt == "NV12" else w * h
    if len(arr) != need:
        raise RuntimeError(f"{pattern} {fmt} {w}x{h}: {len(arr)} bytes, expected {need} (padded strides?)")
    return arr


def read_stamp(luma_rows):
    """luma_rows: callable(y0, y1) -> numpy uint8 array [rows, >= 1536]. Returns (ms, counter) or None."""
    import numpy as np
    band = luma_rows(BLK // 4, BLK - BLK // 4)            # the middle half of the band
    cols = band[:, :BITS * BLK].reshape(band.shape[0], BITS, BLK)[:, :, BLK // 4:BLK - BLK // 4]
    bits = (cols.mean(axis=(0, 2)) > 128).astype(np.uint64)
    v = 0
    for b in bits:
        v = (v << 1) | int(b)
    if v >> 56 != SYNC:
        return None
    return (v >> 16) & ((1 << 40) - 1), v & 0xFFFF


# ------------------------------------------------------------------------------------------------ helpers
def have(factory):
    return Gst.ElementFactory.find(factory) is not None


def struct_dict(s):
    """Gst.Structure (stats) -> plain dict; nested structures/arrays best effort."""
    out = {}
    if s is None:
        return out
    for i in range(s.n_fields()):
        k = s.nth_field_name(i)
        try:
            v = s.get_value(k)
        except TypeError:
            continue
        if isinstance(v, Gst.Structure):
            v = struct_dict(v)
        elif isinstance(v, (list, tuple)) or type(v).__name__ in ("ValueArray", "GValueArray"):
            v = [struct_dict(x) if isinstance(x, Gst.Structure) else x for x in list(v)]
        elif hasattr(v, "get_address") and hasattr(v, "get_port"):
            v = f"{v.get_address().to_string()}:{v.get_port()}"
        elif not isinstance(v, (int, float, str, bool, type(None))):
            v = str(v)
        out[k] = v
    return out


def srt_uri(host, port, mode, extra=None):
    q = f"mode={mode}"
    for k, v in (extra or {}).items():
        q += f"&{k}={v}"
    return f"srt://{host or ''}:{port}?{q}"


def proc_cpu(pid="self"):
    try:
        with open(f"/proc/{pid}/stat") as f:
            p = f.read().rsplit(")", 1)[1].split()
        return (int(p[11]) + int(p[12])) / os.sysconf("SC_CLK_TCK")
    except OSError:
        return None


def drm_engines(pid="self"):
    """amdgpu/i915/v3d fdinfo: busy ns per engine of every DRM fd of this process (proves hardware encode/decode)."""
    out = {}
    try:
        for fd in os.listdir(f"/proc/{pid}/fdinfo"):
            try:
                with open(f"/proc/{pid}/fdinfo/{fd}") as f:
                    txt = f.read()
            except OSError:
                continue
            if "drm-driver" not in txt:
                continue
            for line in txt.splitlines():
                if line.startswith("drm-engine-") and line.endswith(" ns"):
                    k, v = line.split(":", 1)
                    out[k[11:]] = out.get(k[11:], 0) + int(v.split()[0])
    except OSError:
        pass
    return out


class Stats:
    def __init__(self):
        self.t0 = time.time()
        self.cpu0 = proc_cpu()
        self.eng0 = drm_engines()
        self.last = (self.t0, self.cpu0, self.eng0)

    def sample(self):
        t, c, e = time.time(), proc_cpu(), drm_engines()
        lt, lc, le = self.last
        dt = max(1e-6, t - lt)
        self.last = (t, c, e)
        return {"cpu_pct": round(100 * (c - lc) / dt, 1) if c is not None and lc is not None else None,
                "engine_busy_pct": {k: round(100 * (v - le.get(k, 0)) / 1e9 / dt, 1) for k, v in e.items()
                                    if v - le.get(k, 0) > 0}}


class Trace:
    """OWL_VPIPE_TRACE=1: per-frame times at named pads, matched by buffer PTS; status() reports the median time
    between consecutive stages (in the order the stages were declared)."""

    def __init__(self):
        import collections
        self.on = os.environ.get("OWL_VPIPE_TRACE") == "1"
        self.order, self.t = [], {}
        self.od = collections.OrderedDict

    def mark(self, stage, pts):
        if self.on and pts is not None and pts != Gst.CLOCK_TIME_NONE:
            d = self.t[stage]
            d.setdefault(pts, time.monotonic())
            while len(d) > 300:
                d.popitem(last=False)

    def probe(self, pad, stage):
        if self.on:
            self.order.append(stage)
            self.t[stage] = self.od()
            pad.add_probe(Gst.PadProbeType.BUFFER,
                          lambda p, i: (self.mark(stage, i.get_buffer().pts), Gst.PadProbeReturn.OK)[1])

    def report(self):
        if not self.on:
            return None
        import statistics
        out = {}
        for a, b in zip(self.order, self.order[1:]):
            ta, tb = dict(self.t[a]), dict(self.t[b])
            d = [tb[k] - ta[k] for k in ta if k in tb]
            if len(d) > 10:
                out[f"{a}->{b}"] = round(1000 * statistics.median(d), 1)
        return out


TRACE = Trace()


# ------------------------------------------------------------------------------------------------ encoder
def enc_source(spec):
    s = spec["source"]
    w, h, fps = int(s.get("width", 1920)), int(s.get("height", 1080)), int(s.get("fps", 30))
    kind = s.get("kind", "testsrc")
    # interlace-mode/pixel-aspect-ratio stated: rtpjxsvpay (N4) requires progressive, and svtjpegxsenc proxies that
    # upstream, so caps without the field would be refused by the first element after appsrc (not-negotiated)
    raw = f"video/x-raw,format=NV12,width={w},height={h},framerate={fps}/1,interlace-mode=progressive,pixel-aspect-ratio=1/1"
    if kind == "testsrc":   # frames are rendered by TestSource (pattern + moving strip + stamp) and pushed here
        return (f"appsrc name=tsrc is-live=true format=time do-timestamp=true block=false max-buffers=2 "
                f"leaky-type=downstream caps={raw}"), w, h, fps, True
    if kind == "v4l2":
        dev = s.get("device", "/dev/video0")
        if s.get("format", "yuy2").lower() in ("mjpeg", "jpeg"):
            dec = "vajpegdec ! vapostproc" if have("vajpegdec") else "jpegdec ! videoconvert"
            cap = f"image/jpeg,width={w},height={h},framerate={fps}/1 ! jpegparse ! {dec}"
        else:
            cap = f"video/x-raw,format=YUY2,width={w},height={h},framerate={fps}/1 ! videoconvert"
        return f"v4l2src name=src device={dev} do-timestamp=true io-mode=mmap ! {cap} ! {raw}", w, h, fps, False
    raise SystemExit(f"source kind {kind!r} not supported (testsrc, v4l2)")


def encoder(codec, kbps, gop, va, preview=False):
    if codec == "h265":
        if not va or not have("vah265enc"):
            raise SystemExit("H.265 needs VA-API (vah265enc); this node has none. Use codec h264 (x264).")
        enc = f"vah265enc name=venc bitrate={kbps} rate-control=cbr key-int-max={gop} b-frames=0 ref-frames=1 target-usage=6"
        return f"vapostproc ! video/x-raw(memory:VAMemory),format=NV12 ! {enc} ! h265parse config-interval=-1"
    if va and have("vah264enc"):
        enc = f"vah264enc name=venc bitrate={kbps} rate-control=cbr key-int-max={gop} b-frames=0 ref-frames=1 target-usage=6"
        scale = ",width=640,height=360" if preview else ""
        return (f"vapostproc ! video/x-raw(memory:VAMemory),format=NV12{scale} ! {enc} ! "
                f"h264parse config-interval=-1 ! video/x-h264,profile=constrained-baseline" if preview else
                f"vapostproc ! video/x-raw(memory:VAMemory),format=NV12 ! {enc} ! h264parse config-interval=-1")
    scale = "videoscale ! video/x-raw,width=640,height=360 ! " if preview else ""
    return (f"{scale}videoconvert ! x264enc tune=zerolatency speed-preset=ultrafast bitrate={kbps} key-int-max={gop} "
            f"bframes=0 ! h264parse config-interval=-1")


class Encoder:
    def __init__(self, spec):
        self.spec = spec
        self.clock = Clock(spec.get("clock_dev"))
        self.va = os.path.exists(spec.get("render_node", "/dev/dri/renderD128")) and have("vapostproc")
        src, self.w, self.h, self.fps, stamped = enc_source(spec)
        self.stamped = stamped
        ov = ""
        if spec["source"].get("overlay", stamped):
            font = f"Sans {max(12, self.h // 30)}"
            ov = (f' ! timeoverlay halignment=left valignment=bottom font-desc="{font}" shaded-background=true'
                  f' ! clockoverlay halignment=right valignment=bottom time-format="%H:%M:%S" font-desc="{font}"'
                  f' shaded-background=true ! textoverlay text="{spec.get("label", "")}" valignment=center'
                  f' halignment=center font-desc="{font}" shaded-background=true')
        codec, kbps = spec.get("codec", "h265"), int(spec.get("bitrate_kbps", 12000))
        self.codec = codec
        prev = spec.get("preview")
        branches = []
        if codec != "jxs":                  # the N3 sender: H.265/H.264 in RTP (or MPEG-TS) over an SRT listener
            gop = int(spec.get("gop", self.fps * 2))
            srt = srt_uri("", spec["port"], "listener")
            # RTP (default): one packet per SRT message (1316 bytes = SRT's live payload); MPEG-TS: 7 TS packets per message
            wrap = ("mpegtsmux name=mux alignment=7" if spec.get("encap") == "ts" else
                    f"rtp{codec}pay name=pay mtu=1316 config-interval=-1 pt=96")
            branches.append(f"queue name=eq max-size-buffers=3 leaky=downstream ! "
                            f"{encoder(codec, kbps, gop, self.va)} ! identity name=es silent=true ! {wrap} ! "
                            f'srtsink name=srt uri="{srt}" latency={int(spec.get("latency_ms", 40))} '
                            f"wait-for-connection=false sync=false")
        if prev:
            branches.append(f"queue name=pq leaky=downstream max-size-buffers=1 ! intervideosink channel=owl-{NAME[0]}")
        # N4: JPEG XS outputs. The valve stays shut (and the encoder idle) until the sender has a destination.
        self.jxs = {j["key"]: dict(j, frames=0, bytes=0, on=False, host=None, dport=j["port"], lat=[], lat_all=[],
                                   echo_from=None, echoes=0, echo_unknown=0) for j in spec.get("jxs") or []}
        for k, j in self.jxs.items():
            mc = (f" auto-multicast=false ttl-mc={int(j['ttl'])} multicast-iface={j['media_if']}"
                  if j["multicast"] else "")
            threads = f" threads={int(j['threads'])}" if j.get("threads") else ""
            branches.append(
                f"valve name=jv_{k} drop=true drop-mode=forward-sticky-events ! "
                f"queue name=jq_{k} leaky=downstream max-size-buffers=2 max-size-bytes=0 max-size-time=0 ! "
                f"videoconvert n-threads=2 ! video/x-raw,format={j['gst_format']} ! "
                f"svtjpegxsenc name=je_{k} bits-per-pixel={float(j['bpp'])}{threads} ! "
                f'capssetter join=true replace=false caps="image/x-jxsc,profile=(string){j["profile"]},'
                f'level=(string){j["level"]},sublevel=(string){j["sublevel"]}" ! identity name=jo_{k} silent=true ! '
                f"rtpjxsvpay name=jp_{k} mtu={int(j['mtu'])} pt=96 ! "
                f"udpsink name=ju_{k} host={j['own_ip']} port={j['port']} sync=false async=false qos=false "
                f"buffer-size=4194304{mc}")
        if not branches:
            raise SystemExit("nothing to send: no codec output, preview or JPEG XS sender")
        tail = branches[0] if len(branches) == 1 else "tee name=t " + " ".join(f"t. ! {b}" for b in branches)
        self.desc = f"{src}{ov} ! {tail}"
        self.counter = 0
        self.bytes = 0
        self.frames_out = 0
        self.keyframes = 0
        self.preview = None
        self.dyn_path, self.dyn_mtime = spec.get("dyn"), None
        self.lock = threading.Lock()
        if prev:
            pv_uri = srt_uri(prev["host"], prev.get("port", 8890), "caller", {"streamid": f"publish:{prev['path']}"})
            self.preview_desc = (
                f"intervideosrc channel=owl-{NAME[0]} timeout=1000000000 ! "
                f"video/x-raw,format=NV12,width={self.w},height={self.h},framerate={self.fps}/1 ! "
                f"queue leaky=downstream max-size-buffers=2 ! "
                f"{encoder('h264', int(prev.get('bitrate_kbps', 1200)), self.fps, self.va, preview=True)} ! "
                f'mpegtsmux alignment=7 ! srtsink uri="{pv_uri}" latency=200 wait-for-connection=false sync=false')
        log(f"pipeline: {self.desc}")

    def _count(self, pad, info):
        buf = info.get_buffer()
        if buf is not None:
            self.bytes += buf.get_size()
            self.frames_out += 1
            if not buf.has_flags(Gst.BufferFlags.DELTA_UNIT):
                self.keyframes += 1
        return Gst.PadProbeReturn.OK

    def _jcount(self, key):
        j = self.jxs[key]

        def probe(pad, info):
            buf = info.get_buffer()
            if buf is not None:
                j["frames"] += 1
                j["bytes"] += buf.get_size()
            return Gst.PadProbeReturn.OK
        return probe

    def build(self):
        self.p = Gst.parse_launch(self.desc)
        self.tsrc = None
        if self.spec["source"].get("kind", "testsrc") == "testsrc":
            s = self.spec["source"]
            self.tsrc = TestSource(self.p.get_by_name("tsrc"), self.w, self.h, self.fps, self.clock,
                                   s.get("pattern", "smpte"), s.get("strip", "pinwheel"))
        TRACE.probe(self.p.get_by_name("tsrc").get_static_pad("src") if self.p.get_by_name("tsrc") else
                    self.p.get_by_name("src").get_static_pad("src"), "source")
        if self.p.get_by_name("eq"):
            TRACE.probe(self.p.get_by_name("eq").get_static_pad("sink"), "queue_in")
            TRACE.probe(self.p.get_by_name("eq").get_static_pad("src"), "encode_in")
        if self.p.get_by_name("venc"):
            TRACE.probe(self.p.get_by_name("venc").get_static_pad("sink"), "venc_in")
            TRACE.probe(self.p.get_by_name("venc").get_static_pad("src"), "venc_out")
        if self.p.get_by_name("es"):
            TRACE.probe(self.p.get_by_name("es").get_static_pad("src"), "encoded")
            self.p.get_by_name("es").get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, self._count)
        for k in self.jxs:
            if len(self.jxs) == 1:
                TRACE.probe(self.p.get_by_name(f"jq_{k}").get_static_pad("src"), "jxs_in")
                TRACE.probe(self.p.get_by_name(f"jo_{k}").get_static_pad("src"), "jxs_encoded")
            self.p.get_by_name(f"jo_{k}").get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, self._jcount(k))
        self.apply_dyn()
        return self.p

    # ---- N4: patches arrive as a dyn file from the supervisor (no restart, the other outputs keep running)
    def apply_dyn(self):
        if not self.dyn_path:
            return True
        try:
            m = os.stat(self.dyn_path).st_mtime_ns
        except OSError:
            return True
        if m == self.dyn_mtime:
            return True
        self.dyn_mtime = m
        try:
            with open(self.dyn_path) as f:
                dyn = json.load(f)
        except (OSError, ValueError):
            return True
        for k, d in dyn.items():
            j = self.jxs.get(k)
            if j is None:
                continue
            on = bool(d.get("on")) and bool(d.get("host"))
            host, port = d.get("host"), int(d.get("port") or j["port"])
            sink, valve = self.p.get_by_name(f"ju_{k}"), self.p.get_by_name(f"jv_{k}")
            if on and (host != j["host"] or port != j["dport"]):
                sink.set_property("host", host)
                sink.set_property("port", port)
            if on != j["on"] or host != j["host"] or port != j["dport"]:
                log(f"JPEG XS {k}: " + (f"sending to {host}:{port}" if on else "parked (no destination)"))
                with self.lock:
                    j["lat"], j["lat_all"] = [], []
            valve.set_property("drop", not on)
            j.update(on=on, host=host if on else None, dport=port)
        return True

    def start_echo(self):
        """One UDP socket per JPEG XS output: receivers send back each decoded frame's stamp (see the docstring)."""
        for k, j in self.jxs.items():
            if not j.get("echo_port") or not self.tsrc:
                continue
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("0.0.0.0", int(j["echo_port"])))
            except OSError as e:
                log(f"echo port {j['echo_port']} for {k}: {e}")
                continue
            threading.Thread(target=self._echo, args=(k, sock), daemon=True, name=f"echo-{k}").start()

    def _echo(self, key, sock):
        j, cap = self.jxs[key], self.tsrc.cap_ns
        while True:
            data, addr = sock.recvfrom(64)
            now = self.clock.ns()
            if len(data) < ECHO.size:
                continue
            magic, counter, ms, _ = ECHO.unpack_from(data)
            t0 = int(cap[counter])
            with self.lock:
                if magic != b"OWLE" or t0 == 0 or (t0 // 1_000_000) & ((1 << 40) - 1) != ms:
                    j["echo_unknown"] += 1      # not ours, or the frame is too old for the ring
                    continue
                j["echoes"] += 1
                j["echo_from"] = addr[0]
                j["lat"].append((now - t0) / 1e6)

    def start_preview(self):
        if not getattr(self, "preview_desc", None):
            return
        try:
            self.preview = Gst.parse_launch(self.preview_desc)
            bus = self.preview.get_bus()
            bus.add_signal_watch()
            bus.connect("message::error", self._preview_error)
            self.preview.set_state(Gst.State.PLAYING)
            self.preview_state = "running"
        except GLib.Error as e:
            self.preview_state = f"error: {e}"

    def _preview_error(self, bus, msg):
        err, dbg = msg.parse_error()
        log(f"preview error: {err.message}; retrying in 10 s")
        self.preview_state = f"error: {err.message}"
        self.preview.set_state(Gst.State.NULL)
        GLib.timeout_add_seconds(10, lambda: (self.start_preview(), False)[1])

    def jxs_status(self, dt):
        import statistics
        out = {}
        for k, j in self.jxs.items():
            with self.lock:
                lat, j["lat"] = j["lat"], []
                j["lat_all"] = (j["lat_all"] + lat)[-20000:]
                la = sorted(j["lat_all"])
            q = lambda a, p: round(a[min(len(a) - 1, int(p * len(a)))], 2) if a else None
            last = j.setdefault("_last", (0, 0))
            j["_last"] = (j["frames"], j["bytes"])
            out[k] = {"on": j["on"], "destination": f"{j['host']}:{j['dport']}" if j["on"] else None,
                      "frames": j["frames"], "bytes": j["bytes"],
                      "fps": round((j["frames"] - last[0]) / dt, 2) if dt > 0 else None,
                      "codestream_kbps": round(8 * (j["bytes"] - last[1]) / dt / 1000) if dt > 0 else None,
                      "bpp": j["bpp"], "echo": {
                          "from": j["echo_from"], "n": j["echoes"], "unknown": j["echo_unknown"],
                          "window": {"n": len(lat), "median": round(statistics.median(lat), 2) if lat else None},
                          "recent": {"n": len(la), "min": q(la, 0), "median": q(la, 0.5), "p95": q(la, 0.95),
                                     "p99": q(la, 0.99), "max": q(la, 1.0)}}}
        return out

    def status(self, dt):
        srt = struct_dict(self.p.get_by_name("srt").get_property("stats")) if self.p.get_by_name("srt") else None
        out = {"kind": "enc", "codec": self.codec, "hardware": self.va,
               "size": f"{self.w}x{self.h}", "fps_nominal": self.fps, "stamped": self.stamped, "clock": self.clock.kind,
               "frames_encoded": self.frames_out if self.codec != "jxs" else (
                   max((j["frames"] for j in self.jxs.values()), default=0)),
               "keyframes": self.keyframes, "bytes": self.bytes,
               "srt": srt, "preview": getattr(self, "preview_state", None)}
        if self.jxs:
            out["jxs"] = self.jxs_status(dt)
        return out


# ------------------------------------------------------------------------------------------------ decoder
DECODERS = {"h265": ["v4l2slh265dec", "vah265dec", "avdec_h265"], "h264": ["v4l2slh264dec", "vah264dec", "avdec_h264"],
            "jxs": ["svtjpegxsdec"]}


def udp_counters():
    """Host-wide UDP drop counters (/proc/net/snmp; host networking, so they are the host's): RcvbufErrors = datagrams
    the kernel dropped because a socket buffer was full."""
    try:
        with open("/proc/net/snmp") as f:
            rows = [line.split() for line in f if line.startswith("Udp:")]
        return dict(zip(rows[0][1:], map(int, rows[1][1:])))
    except (OSError, IndexError, ValueError):
        return {}


class Decoder:
    def __init__(self, spec):
        self.spec = spec
        self.clock = Clock(spec.get("clock_dev"))
        self.display = spec.get("display", "none")          # kms | none
        self.verify = bool(spec.get("verify", self.display == "none"))
        self.echo = None
        if spec.get("codec") == "jxs":      # N4: JPEG XS in RTP (RFC 9134) over plain UDP, unicast or multicast
            self.encap = "rtp-udp"
            g = spec.get("group")
            addr = (f"address={g} auto-multicast=true multicast-iface={spec.get('media_if', 'eth0')}" if g else
                    "address=0.0.0.0")
            # no jitter buffer (a LAN does not reorder; it would only add latency); owlrtpstat counts sequence gaps
            self.desc = (f'udpsrc name=udp port={int(spec["port"])} {addr} buffer-size=4194304 caps="{spec["caps"]}" ! '
                         f"owlrtpstat name=rs ! rtpjxsvdepay name=depay")
            if spec.get("echo"):
                host, port = spec["echo"].rsplit(":", 1)
                self.echo = (host, int(port))
                self.echo_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.udp0 = udp_counters()
        elif spec.get("encap", "rtp") == "ts":
            self.encap = "ts"
        else:
            self.encap = "rtp"
        if self.encap != "rtp-udp":         # N3: SRT caller
            lat = int(spec.get("latency_ms", 40))
            uri = srt_uri(spec["source_ip"], spec["source_port"], "caller",
                          {"streamid": spec["streamid"]} if spec.get("streamid") else None)
            src = (f'srtsrc name=srt uri="{uri}" latency={lat} ! queue name=nq max-size-buffers=0 max-size-time=0 '
                   f'max-size-bytes=4000000')
        if self.encap == "ts":
            self.desc = f'{src} ! tsdemux name=demux latency={int(spec.get("demux_latency_ms", 0))}'
        elif self.encap == "rtp":   # RTP over SRT: one RTP packet per SRT message; the depayloader emits each AU on its marker bit
            codec = spec.get("encoding", "H265").upper()
            self.desc = (f"{src} ! application/x-rtp,media=video,clock-rate=90000,encoding-name={codec},"
                         f"payload={int(spec.get('payload', 96))} ! rtp{codec.lower()}depay name=depay")
        self.frames = 0
        self.codec = None
        self.decoder = None
        self.out_caps = None
        self.v = {"stamped": 0, "unstamped": 0, "gaps": 0, "missing": 0, "repeats": 0, "last_counter": None,
                  "lat": [], "lat_all": [], "checks": [], "first_frame": None}
        self.ref = None
        self.lock = threading.Lock()

    def build(self):
        self.p = Gst.parse_launch(self.desc)
        if self.encap == "ts":
            self.p.get_by_name("demux").connect("pad-added", self._pad)
        elif self.encap == "rtp-udp":
            self._chain(self.p.get_by_name("depay").get_static_pad("src"), "image/x-jxsc", rtp=True)
        else:
            depay = self.p.get_by_name("depay")
            name = "video/x-h265" if self.spec.get("encoding", "H265").upper() == "H265" else "video/x-h264"
            self._chain(depay.get_static_pad("src"), name, rtp=True)
        return self.p

    def _pad(self, demux, pad):
        caps = pad.get_current_caps() or pad.query_caps(None)
        self._chain(pad, caps.get_structure(0).get_name(), rtp=False)

    def _chain(self, pad, name, rtp):
        """parser -> decoder -> sinks, linked to `pad` (tsdemux's video pad or the RTP depayloader)."""
        codec = {"video/x-h265": "h265", "video/x-h264": "h264", "image/x-jxsc": "jxs"}.get(name)
        if not codec or self.codec:
            log(f"ignoring stream {name}")
            return
        self.codec = codec
        dec = self.spec.get("decoder") or next((d for d in DECODERS[codec] if have(d)), None)
        if not dec:
            log(f"no decoder for {codec}")
            return
        self.decoder = dec
        sinks = []
        if self.display == "kms":
            kms = "kmssink name=kms sync=false" + "".join(
                f" {k}={v}" for k, v in (("driver-name", self.spec.get("kms_driver")),
                                         ("connector-id", self.spec.get("connector_id"))) if v)
            sinks.append(kms)
        if self.verify:
            sinks.append("appsink name=vsink emit-signals=true max-buffers=2 drop=true sync=false")
        if not sinks:
            sinks.append("fakesink sync=false")
        tail = sinks[0] if len(sinks) == 1 else "tee name=dt " + " ".join(
            f"dt. ! queue leaky=downstream max-size-buffers=2 ! {s}" for s in sinks)
        parse = {"h265": "h265parse", "h264": "h264parse", "jxs": ""}[codec]
        # tsdemux pushes one whole PES = one access unit but does not say so; without alignment=au the parser holds
        # every frame until the next one starts (+1 frame, 33 ms at 30 fps). The RTP depayloader already says au.
        au = "" if rtp else f'capssetter join=true replace=false caps="{name},alignment=(string)au,stream-format=(string)byte-stream" ! '
        # v4l2slh265dec (Pi 5) only outputs SAND128 DMA-BUFs; owlpoolfix (our element, gst/) answers the allocation
        # query so it negotiates with kmssink and appsink alike (see gst/gstowlpoolfix.c)
        fix = " ! video/x-raw(memory:DMABuf),format=DMA_DRM ! owlpoolfix" if dec.startswith("v4l2sl") else ""
        if codec == "jxs":      # whole frames from the depayloader; a decoder that cannot keep up drops (counted)
            desc = (f"queue max-size-buffers=3 max-size-time=0 max-size-bytes=0 leaky=downstream ! {dec} name=dec ! "
                    f"identity name=dout silent=true ! {tail}")
        else:
            desc = (f"queue max-size-buffers=0 max-size-time=0 max-size-bytes=8000000 ! {au}{parse} ! {dec} name=dec{fix} ! "
                    f"identity name=dout silent=true ! {tail}")
        log(f"{'RTP/UDP' if self.encap == 'rtp-udp' else 'RTP' if rtp else 'MPEG-TS'} {name} -> {dec} -> "
            f"{', '.join(s.split()[0] for s in sinks)}" + (f"; latency echo to {self.echo[0]}:{self.echo[1]}" if self.echo else ""))
        bin_ = Gst.parse_bin_from_description(desc, True)
        self.p.add(bin_)
        bin_.sync_state_with_parent()
        TRACE.probe(pad, "depayloaded" if rtp else "demuxed")
        pad.link(bin_.get_static_pad("sink"))
        TRACE.probe(bin_.get_by_name("dec").get_static_pad("sink"), "parsed")
        TRACE.probe(bin_.get_by_name("dout").get_static_pad("src"), "decoded")
        bin_.get_by_name("dout").get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, self._count)
        if self.verify:
            bin_.get_by_name("vsink").connect("new-sample", self._sample)

    def _count(self, pad, info):
        self.frames += 1
        if self.out_caps is None:
            c = pad.get_current_caps()
            self.out_caps = c.to_string() if c else None
        return Gst.PadProbeReturn.OK

    # luma access for the stamp reader: linear NV12/I420 or the Pi's column (SAND, NV12_128C8 "NC12") layout
    def _luma(self, sample):
        import numpy as np
        buf, caps = sample.get_buffer(), sample.get_caps()
        st = caps.get_structure(0)
        fmt, w, h = st.get_value("format"), st.get_value("width"), st.get_value("height")
        if fmt == "DMA_DRM":                                    # e.g. NV12:0x0700000000000004 (Broadcom SAND128)
            drm = st.get_value("drm-format") or ""
            fmt = "SAND128" if drm.lower().endswith("0x0700000000000004") else "DMA_DRM " + drm
        ok, info = buf.map(Gst.MapFlags.READ)
        if not ok:
            return None
        raw = info.data
        try:
            gi.require_version("GstVideo", "1.0")
            from gi.repository import GstVideo
            meta = GstVideo.buffer_get_video_meta(buf)
        except Exception:
            meta = None
        stride = meta.stride[0] if meta else w
        off0 = meta.offset[0] if meta else 0
        if fmt in ("NV12", "I420", "GRAY8", "NV21", "YV12", "Y42B", "Y444"):
            def rows(y0, y1):
                a = np.frombuffer(raw, dtype=np.uint8, count=(y1 - y0) * stride, offset=off0 + y0 * stride)
                return a.reshape(y1 - y0, stride)[:, :w]
            full = (lambda: rows(0, h))
        elif fmt == "SAND128":
            # Pi 5 'NC12': 128-byte-wide columns, each `colh` rows tall (luma rows, then the chroma rows); GstVideoMeta
            # stride[0] is that column height (1632 = 1088 + 544 for 1080p)
            colh = stride
            ncol = (w + 127) // 128

            def rows(y0, y1):
                a = np.frombuffer(raw, dtype=np.uint8, count=ncol * colh * 128, offset=off0)
                a = a.reshape(ncol, colh, 128)[:, y0:y1, :]
                return a.transpose(1, 0, 2).reshape(y1 - y0, ncol * 128)[:, :w]
            full = (lambda: rows(0, h))
        else:
            buf.unmap(info)
            return None
        return buf, info, rows, full, fmt, w, h

    def _sample(self, sink):
        s = sink.emit("pull-sample")
        if s is None:
            return Gst.FlowReturn.OK
        now = self.clock.ms()
        r = self._luma(s)
        if r is None:
            self.v["unstamped"] += 1
            return Gst.FlowReturn.OK
        buf, info, rows, full, fmt, w, h = r
        try:
            st = read_stamp(rows)
            with self.lock:
                if st is None:
                    self.v["unstamped"] += 1
                    return Gst.FlowReturn.OK
                ms, counter = st
                if self.echo:
                    try:
                        self.echo_sock.sendto(ECHO.pack(b"OWLE", counter, ms, self.clock.ns()), self.echo)
                    except OSError:
                        pass
                self.v["stamped"] += 1
                lat = (now - ms) % (1 << 40)
                if lat > 1 << 39:
                    lat -= 1 << 40
                self.v["lat"].append(lat)
                lc = self.v["last_counter"]
                if lc is not None:
                    d = (counter - lc) % 65536
                    if d == 0:
                        self.v["repeats"] += 1
                    elif d > 1 and d < 32768:
                        self.v["gaps"] += 1
                        self.v["missing"] += d - 1
                self.v["last_counter"] = counter
                if self.v["first_frame"] is None:
                    self.v["first_frame"] = {"counter": counter, "latency_ms": lat, "format": fmt}
                if counter % int(self.spec.get("check_every", 300)) == 0:
                    import numpy as np
                    y = np.ascontiguousarray(full())
                    chk = {"counter": counter, "md5_luma": hashlib.md5(y.tobytes()).hexdigest()[:16],
                           "md5_luma_unstamped": hashlib.md5(y[BLK:].tobytes()).hexdigest()[:16]}
                    ref = self._reference(w, h)
                    if ref is not None:
                        # the bars area only: below the stamp band, above the overlays (bottom 25 %) and the
                        # centred label text
                        y0, y1 = BLK * 2, int(h * 0.40)
                        a, b = y[y0:y1].astype(np.float64), ref[y0:y1].astype(np.float64)
                        mse = float(((a - b) ** 2).mean())
                        chk["psnr_db"] = round(99.0 if mse == 0 else 10 * np.log10(255 ** 2 / mse), 2)
                    self.v["checks"] = (self.v["checks"] + [chk])[-30:]
        finally:
            buf.unmap(info)
        return Gst.FlowReturn.OK

    def _reference(self, w, h):
        """The sender's test pattern, rendered here (videotestsrc is deterministic), luma plane."""
        src = self.spec.get("ref_pattern")
        if src and self.ref is None:
            self.ref = render_pattern(src, w, h, "NV12")[:w * h].reshape(h, w)   # limited-range luma, as sent
        return self.ref

    def status(self, dt):
        import statistics
        srt = struct_dict(self.p.get_by_name("srt").get_property("stats")) if self.p.get_by_name("srt") else None
        rtp = None
        if self.encap == "rtp-udp":
            rtp = struct_dict(self.p.get_by_name("rs").get_property("stats"))
            u = udp_counters()
            rtp["host_udp_rcvbuf_errors"] = u.get("RcvbufErrors", 0) - self.udp0.get("RcvbufErrors", 0)
            rtp["host_udp_in_errors"] = u.get("InErrors", 0) - self.udp0.get("InErrors", 0)
            rtp["mbit_s"] = round(8 * (rtp.get("bytes", 0) - getattr(self, "_rb", 0)) / max(dt, 1e-6) / 1e6, 2)
            self._rb = rtp.get("bytes", 0)
        with self.lock:
            lat, self.v["lat"] = self.v["lat"], []
            self.v["lat_all"] = (self.v["lat_all"] + lat)[-20000:]
            la = sorted(self.v["lat_all"])
            v = {k: self.v[k] for k in ("stamped", "unstamped", "gaps", "missing", "repeats", "checks", "first_frame")}
        q = lambda a, p: a[min(len(a) - 1, int(p * len(a)))] if a else None
        v["latency_ms"] = {"window": {"n": len(lat), "min": min(lat) if lat else None,
                                      "median": statistics.median(lat) if lat else None, "max": max(lat) if lat else None},
                           "recent": {"n": len(la), "min": la[0] if la else None, "median": q(la, 0.5),
                                      "p95": q(la, 0.95), "p99": q(la, 0.99), "max": la[-1] if la else None}}
        return {"kind": "dec", "codec": self.codec, "decoder": self.decoder, "display": self.display,
                "clock": self.clock.kind, "frames_decoded": self.frames, "out_caps": self.out_caps,
                "source": (f"{self.spec.get('source_ip')} -> :{self.spec['port']}" if self.encap == "rtp-udp" else
                           f"{self.spec['source_ip']}:{self.spec['source_port']}"), "srt": srt, "rtp": rtp,
                "echo": f"{self.echo[0]}:{self.echo[1]}" if self.echo else None,
                "verify": v if self.verify else None}


# ------------------------------------------------------------------------------------------------ main
def main():
    spec_path = sys.argv[1]
    with open(spec_path) as f:
        spec = json.load(f)
    NAME[0] = spec.get("name", os.path.basename(spec_path))
    Gst.init(None)
    obj = Encoder(spec) if spec["kind"] == "enc" else Decoder(spec)
    pipe = obj.build()
    loop = GLib.MainLoop()
    rc = [0]
    stats = Stats()
    status_path = spec.get("status")
    t_start = time.time()
    last = [time.time(), 0]

    def on_msg(bus, msg):
        t = msg.type
        if t == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            log(f"ERROR from {msg.src.get_name()}: {err.message} ({dbg})")
            rc[0] = 1
            loop.quit()
        elif t == Gst.MessageType.EOS:
            log("EOS")
            rc[0] = 2
            loop.quit()
        elif t == Gst.MessageType.WARNING:
            w, dbg = msg.parse_warning()
            log(f"warning from {msg.src.get_name()}: {w.message}")
        return True

    bus = pipe.get_bus()
    bus.add_signal_watch()
    bus.connect("message", on_msg)

    def tick():
        now = time.time()
        dt = now - last[0]
        try:
            st = obj.status(dt)
        except Exception as e:  # status must never kill the stream
            st = {"error": f"{type(e).__name__}: {e}"}
        n = st.get("frames_encoded", st.get("frames_decoded", 0)) or 0
        if TRACE.on:
            st["trace_ms"] = TRACE.report()
        st.update({"name": NAME[0], "pid": os.getpid(), "time": now, "up_s": round(now - t_start, 1),
                   "fps": round((n - last[1]) / dt, 2) if dt > 0 else None}, **stats.sample())
        last[0], last[1] = now, n
        if status_path:
            tmp = status_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(st, f)
            os.replace(tmp, status_path)
        return True

    GLib.timeout_add(int(STATUS_EVERY * 1000), tick)
    try:
        gi.require_version("GLibUnix", "2.0")
        from gi.repository import GLibUnix
        add = GLibUnix.signal_add
    except (ValueError, ImportError):
        add = GLib.unix_signal_add
    for s in (signal.SIGTERM, signal.SIGINT):
        add(GLib.PRIORITY_HIGH, s, lambda *a: (loop.quit(), False)[1])
    if pipe.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
        log("pipeline failed to start")
        sys.exit(1)
    if isinstance(obj, Encoder):
        if obj.tsrc:
            threading.Thread(target=obj.tsrc.run, daemon=True, name="testsrc").start()
        obj.start_preview()
        obj.start_echo()
        if obj.dyn_path:
            GLib.timeout_add(250, obj.apply_dyn)
    log(f"running ({obj.clock.kind})")
    loop.run()
    pipe.set_state(Gst.State.NULL)
    if isinstance(obj, Encoder) and obj.tsrc:
        obj.tsrc.stop = True
    if isinstance(obj, Encoder) and obj.preview is not None:
        obj.preview.set_state(Gst.State.NULL)
    sys.exit(rc[0])


if __name__ == "__main__":
    main()
