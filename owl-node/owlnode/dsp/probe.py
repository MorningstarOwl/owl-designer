"""The listening probe: hear any point of the hub's graph on the designer's computer.

  GET /api/dsp/probe?block=<block id>&port=<port id>     WebSocket (the controller tunnels it like /api/dsp/ws)

The server resolves (block, port) to a PipeWire node of the hub: a block or device output is that node; an input that
is a sink (a filter-chain input, the AEC reference, a speaker) is its monitor; any other input (the AEC's mic stream,
the voice assistant's listen) is what is wired into it. A `pw-record` stream (owl-probe-N, not autoconnected) is
linked to it exactly like a meter tap, so the audio path itself is never touched, and the raw PCM goes to the browser:

  server -> client  text   {"type": "probe", "label", "node", "monitor", "rate": 48000, "channels": 2, "format": "s16le"}
                    binary  interleaved s16le stereo, 40 ms per frame (a mono source is on both channels)
                    text   {"type": "error", "error"} (then the socket closes)
  client -> server  text   {"type": "stop"} or just close the socket

Hub only for now (blocks and devices on a satellite say so). At most MAX_PROBES at once. 48 kHz s16 stereo is about
1.5 Mbit/s per probe, fine on the LAN."""
import json, socket, subprocess, threading, time
from urllib.parse import parse_qs, urlsplit

from . import pw
from .ws import accept_key, frame, read_frame

RATE, CHANNELS = 48000, 2
CHUNK = RATE // 25 * CHANNELS * 2          # 40 ms of s16 stereo
MAX_PROBES = 4
SINK_CLASSES = ("Audio/Sink", "Audio/Sink/Virtual", "Audio/Duplex")

_lock = threading.Lock()
_active = {}                               # probe id -> {label, node, monitor, started, peer}
_seq = [0]


class ProbeError(Exception):
    pass


def active():
    with _lock:
        return [dict(v, id=k, age_s=round(time.time() - v["started"], 1)) for k, v in _active.items()]


def _is_sink(g, node):
    n = g.node(node)
    return bool(n and (n["class"] or "") in SINK_CLASSES)


def resolve(dep, bid, pid):
    """(block id, port id) of the canvas -> {node, monitor, ports, label} on the hub, or ProbeError."""
    hub = dep.cfg.name
    d = dep.desired
    inv = dep.inv.refresh(1.0)
    g = inv.graph or pw.dump()
    design = dep.design or {}
    dblock = next((b for b in design.get("blocks") or [] if b.get("id") == bid), None)

    def done(node, monitor, ports, label, note=None):
        if not node or not g.node(node):
            raise ProbeError(f"{label}: its PipeWire node {node or '(none)'} is not there now")
        if not g.node_ports(node, "out", monitor=monitor, idx=ports):
            if not monitor and g.node_ports(node, "out", monitor=True, idx=ports):
                monitor = True
            else:
                raise ProbeError(f"{label}: {node} has no {'monitor ' if monitor else ''}output to listen to")
        return {"node": node, "monitor": bool(monitor), "ports": ports, "label": label, "note": note}

    def wired_into(node, label):
        """What the deployed design links into `node` (a stream input has no monitor of its own)."""
        srcs = []
        for l in (d.links if d else []):
            if l["dst"]["node"] == node and (l["src"]["node"], l["src"].get("monitor"), l["src"].get("ports")) not in srcs:
                srcs.append((l["src"]["node"], l["src"].get("monitor"), l["src"].get("ports")))
        if not srcs:
            raise ProbeError(f"{label}: nothing is wired into it")
        s = srcs[0]
        return done(s[0], s[1], s[2], label, note=f"what is wired into it ({s[0]})" + (f", the first of {len(srcs)} sources" if len(srcs) > 1 else ""))

    if dblock is not None and d is not None:                      # a block of the deployed design
        host = d.block_host.get(bid, hub)
        bt = dep.types.get(dblock.get("type"))
        name = dblock.get("label") or (bt.label if bt else bid)
        label = f"{name} · {pid}"
        if host != hub:
            raise ProbeError(f"{name} runs on {host}: the probe listens on the hub's nodes only for now")
        v = getattr(d, "voice", None)
        if v and v.get("block") == bid:
            if pid == "listen":
                if not v.get("mic"):
                    raise ProbeError(f"{label}: nothing is wired into it")
                return done(v["mic"], False, None, label, note=f"what the assistant hears ({v['mic']})")
            if not v.get("reply"):
                raise ProbeError(f"{label}: the reply goes nowhere (no wire)")
            return done(v["reply"], _is_sink(g, v["reply"]), None, label, note=f"the sink its replies play into ({v['reply']}), with whatever else plays there")
        if bid in (d.devices or {}):
            return done(d.devices[bid]["node"], False, None, label)
        if bid in d.taps:                                          # a Meter block: what it measures
            t = d.taps[bid]
            return done(t["node"], t.get("monitor"), t.get("ports"), label)
        for opid, node, ports, mon in d.block_outputs.get(bid, []):
            if opid == pid:
                return done(node, mon, ports, label)
        if bt:
            spec = bt.node_map(bt.ctx(bid, dblock.get("label"), dblock.get("params"))).get(pid)
            if spec and spec["dir"] == "in":
                if _is_sink(g, spec["node"]):
                    return done(spec["node"], True, spec.get("ports"), label)
                return wired_into(spec["node"], label)
        raise ProbeError(f"{label}: no such port in the deployed design (deploy first?)")
    if dblock is not None:
        raise ProbeError(f"{bid} is not deployed yet: deploy, then probe it")

    blk, port = inv.port(bid, pid)
    if not blk:
        raise ProbeError(f"{bid}: not in the inventory now (unplugged, or an app that is not playing)")
    label = f"{blk.get('label') or bid} · {(port or {}).get('label') or pid}"
    if not port:
        raise ProbeError(f"{label}: no such port")
    if blk.get("host") != hub and not blk.get("is_hub"):
        raise ProbeError(f"{blk.get('label')} is on {blk.get('host')}: the probe listens on the hub's nodes only for now")
    if "pw" in port:
        node = port["pw"]["node"]
        if port["dir"] == "out":
            return done(node, bool(port["pw"].get("monitor")), None, label)
        if _is_sink(g, node):
            return done(node, True, None, label, note="what plays into it (its monitor)")
        return wired_into(node, label)
    if "nmos" in port:
        node = port["nmos"].get("pw_node")
        if not node:
            raise ProbeError(f"{label}: this NMOS port has no PipeWire node on the hub (video, or a satellite's)")
        return done(node, _is_sink(g, node), None, label)
    raise ProbeError(f"{label}: nothing to listen to")


class ProbeSession:
    def __init__(self, handler, dep, log):
        self.h, self.dep, self.log = handler, dep, log
        self.sock = handler.connection
        self.wlock = threading.Lock()
        self.alive = True
        self.rec = None

    def _send(self, data, opcode=1):
        try:
            with self.wlock:
                self.sock.sendall(frame(data, opcode))
            return True
        except OSError:
            self.alive = False
            return False

    def _reader(self):
        try:
            while self.alive:
                fr = read_frame(self.h.rfile)
                if fr is None or fr[0] == 8:
                    break
                if fr[0] == 9:
                    self._send(fr[1], 10)
                elif fr[0] == 1:
                    try:
                        if json.loads(fr[1].decode()).get("type") == "stop":
                            break
                    except ValueError:
                        pass
        except OSError:
            pass
        self.alive = False
        self._kill()

    def _kill(self):
        rec, self.rec = self.rec, None
        if rec is not None:
            try:
                rec.terminate()
                rec.wait(2)
            except Exception:
                try:
                    rec.kill()
                except Exception:
                    pass

    def _linker(self, src, me):
        """Link src -> the probe stream, and again whenever the source comes back (a block restarted)."""
        while self.alive:
            try:
                g = pw.dump()
                sp = g.node_ports(src["node"], "out", monitor=src["monitor"], idx=src.get("ports"))
                dp = g.node_ports(me, "in")
                have = g.link_set()
                for a, b in pw.pair_ports(sp, dp):
                    if (a["id"], b["id"]) not in have:
                        pw.link(a["id"], b["id"])
            except Exception as e:
                self.log(f"probe link: {type(e).__name__}: {e}")
            for _ in range(20):
                if not self.alive:
                    return
                time.sleep(0.1)

    def run(self):
        key = self.h.headers.get("Sec-WebSocket-Key")
        if not key or "websocket" not in (self.h.headers.get("Upgrade") or "").lower():
            return False
        self.sock.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                           f"Sec-WebSocket-Accept: {accept_key(key)}\r\n\r\n").encode())
        self.h.close_connection = True
        self.sock.settimeout(None)
        q = parse_qs(urlsplit(self.h.path).query)
        bid, pid = (q.get("block") or [""])[0], (q.get("port") or [""])[0]
        pid_ = None
        try:
            with _lock:
                if len(_active) >= MAX_PROBES:
                    raise ProbeError(f"{MAX_PROBES} probes are already listening; stop one first")
            src = resolve(self.dep, bid, pid)
            with _lock:
                _seq[0] += 1
                pid_ = f"p{_seq[0]}"
                _active[pid_] = {"label": src["label"], "node": src["node"], "monitor": src["monitor"], "started": time.time(),
                                 "peer": self.h.client_address[0]}
            me = f"owl-probe-{pid_}"
            props = (f'{{ node.name = "{me}" node.description = "owl probe {src["label"]}" node.autoconnect = false '
                     f'media.class = Stream/Input/Audio owl.dsp.probe = "{pid_}" }}')
            self.rec = subprocess.Popen(["pw-record", "--raw", "--format", "s16", "--rate", str(RATE), "--channels", str(CHANNELS),
                                         "--target", "0", "--latency", "960", "-P", props, "-"],
                                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
            self._send(json.dumps({"type": "probe", "id": pid_, "label": src["label"], "node": src["node"], "monitor": src["monitor"],
                                   "note": src.get("note"), "rate": RATE, "channels": CHANNELS, "format": "s16le"}))
            self.log(f"probe {pid_} on {src['node']}{' (monitor)' if src['monitor'] else ''} for {self.h.client_address[0]}: {src['label']}")
            threading.Thread(target=self._reader, daemon=True).start()
            threading.Thread(target=self._linker, args=(src, me), daemon=True).start()
            rec = self.rec
            while self.alive and rec.poll() is None:
                buf = rec.stdout.read(CHUNK)
                if not buf:
                    break
                if not self._send(buf, 2):
                    break
        except ProbeError as e:
            self._send(json.dumps({"type": "error", "error": str(e)}))
        except Exception as e:
            self._send(json.dumps({"type": "error", "error": f"{type(e).__name__}: {e}"}))
            self.log(f"probe: {type(e).__name__}: {e}")
        finally:
            self.alive = False
            self._kill()
            if pid_:
                with _lock:
                    _active.pop(pid_, None)
                self.log(f"probe {pid_} stopped")
            try:
                self._send(b"", 8)
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        return True
