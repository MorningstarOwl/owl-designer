"""The injector: a test signal played INTO any input of the graph (the probe's opposite), for an engineer checking a
signal path: white noise, pink noise or a sine, at a set level, for a limited time.

  POST   /api/dsp/inject        {block, port, kind: white|pink|sine, level_db, freq_hz, duration_s} (hub: resolves the
                                canvas port; on a satellite's port the hub asks that satellite) -> {id, label, ...}
  POST   /api/dsp/inject/<id>   {level_db}: change the level while it plays
  DELETE /api/dsp/inject/<id>   stop (also: every injection stops by itself after duration_s, at most MAX_S)
  GET    /api/dsp/inject        what is injecting now

A satellite's dsp role takes {node, ports, label, ...} directly (the hub has resolved the canvas port).

One injection = one `pw-play` stream (owl-inject-N, not autoconnected) fed raw float32 by a thread, linked by us into
the target node's input ports (and re-linked if the target restarts). Each channel gets its own noise (uncorrelated,
as a measurement wants); the level is RMS in dBFS. The audio path itself is not changed: the signal is ADDED to
whatever else plays into that input."""
import itertools, subprocess, threading, time

import numpy as np

from . import pw
from .meters import record_has_raw

RATE = 48000
FRAME = 960                      # 20 ms per write
KINDS = ("white", "pink", "sine")
MAX_INJECTIONS = 4
MAX_S = 600
MIN_DB, MAX_DB = -80.0, -3.0
DEFAULT_DB = -30.0
PINK_LOOP_S = 10                 # pink noise: a seamless 10 s loop per channel (FFT-shaped, periodic by construction)


class InjectError(Exception):
    pass


def pink_loop(channels, seconds=PINK_LOOP_S, rng=None):
    """Periodic pink noise (1/f power from 20 Hz up), unit RMS per channel: white noise shaped in the frequency domain
    over the whole loop, so the loop repeats without a seam."""
    rng = rng or np.random.default_rng()
    n = RATE * seconds
    spec = rng.standard_normal((n // 2 + 1, channels)) + 1j * rng.standard_normal((n // 2 + 1, channels))
    f = np.arange(n // 2 + 1, dtype=np.float64) * RATE / n          # Hz
    spec /= np.sqrt(np.maximum(f, 1.0))[:, None]
    spec[f < 20.0] = 0                           # audio band only: sub-20 Hz drift would swing the level and is inaudible
    x = np.fft.irfft(spec, n=n, axis=0)
    x /= np.sqrt(np.mean(x * x, axis=0))
    return x.astype(np.float32)


class Generator:
    """Blocks of FRAME frames x channels float32 at the current RMS level (set_level works while it runs)."""

    def __init__(self, kind, channels, level_db, freq_hz=1000.0, rng=None):
        if kind not in KINDS:
            raise InjectError(f"kind must be one of {', '.join(KINDS)}")
        self.kind, self.ch, self.freq = kind, channels, float(freq_hz)
        self.rng = rng or np.random.default_rng()
        self.set_level(level_db)
        self.pos = 0
        self.pink = pink_loop(channels, rng=self.rng) if kind == "pink" else None

    def set_level(self, level_db):
        lv = float(level_db)
        if not MIN_DB <= lv <= MAX_DB:
            raise InjectError(f"level_db must be {MIN_DB:g}..{MAX_DB:g} dBFS")
        self.level_db, self.rms = lv, 10 ** (lv / 20)

    def block(self):
        if self.kind == "white":
            x = self.rng.standard_normal((FRAME, self.ch)).astype(np.float32)
        elif self.kind == "pink":
            idx = (self.pos + np.arange(FRAME)) % len(self.pink)
            x = self.pink[idx]
        else:
            t = (self.pos + np.arange(FRAME)) / RATE
            x = np.repeat((np.sqrt(2) * np.sin(2 * np.pi * self.freq * t)).astype(np.float32)[:, None], self.ch, axis=1)
        self.pos += FRAME
        return np.clip(x * self.rms, -1.0, 1.0).astype(np.float32)


class Injection:
    def __init__(self, iid, node, ports, label, kind, level_db, freq_hz, duration_s, channels, log):
        self.id, self.node, self.ports, self.label, self.log = iid, node, ports, label, log
        self.gen = Generator(kind, channels, level_db, freq_hz)
        self.duration = max(1.0, min(float(duration_s), MAX_S))
        self.started, self.ends_at = time.time(), time.time() + self.duration
        self.me = f"owl-inject-{iid}"
        self.p, self.alive, self.linked = None, True, 0

    def argv(self):
        props = (f'{{ node.name = "{self.me}" node.description = "owl inject {self.gen.kind} -> {self.label}" '
                 f'node.autoconnect = false media.class = Stream/Output/Audio owl.dsp.inject = "{self.id}" }}')
        return (["pw-play"] + (["--raw"] if record_has_raw() else []) +
                ["--format", "f32", "--rate", str(RATE), "--channels", str(self.gen.ch), "--target", "0",
                 "--latency", "960", "-P", props, "-"])

    def start(self):
        self.p = subprocess.Popen(self.argv(), stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                  start_new_session=True, bufsize=0)
        threading.Thread(target=self._feed, daemon=True, name=self.me).start()
        threading.Thread(target=self._link, daemon=True, name=self.me + "-link").start()
        # the time limit holds even while the feeder is blocked (pw-play reads nothing until it is linked)
        self.timer = threading.Timer(self.duration, self.stop, args=("time is up",))
        self.timer.daemon = True
        self.timer.start()

    def _feed(self):
        """Write ahead of time by at most ~100 ms: pw-play only reads while it is linked and running, so the pipe
        fills and blocks us; level changes then take effect within that."""
        p = self.p
        try:
            while self.alive and time.time() < self.ends_at and p.poll() is None:
                p.stdin.write(self.gen.block().tobytes())
        except (OSError, ValueError):
            pass
        self.stop("time is up" if time.time() >= self.ends_at else None)

    def _link(self):
        while self.alive:
            try:
                g = pw.dump()
                sp, dp = g.node_ports(self.me, "out"), g.node_ports(self.node, "in", idx=self.ports)
                have = g.link_set()
                for a, b in pw.pair_ports(sp, dp):
                    if (a["id"], b["id"]) not in have:
                        ok, _ = pw.link(a["id"], b["id"])
                        self.linked += ok
            except Exception as e:
                self.log(f"inject link: {type(e).__name__}: {e}")
            for _ in range(20):
                if not self.alive:
                    return
                time.sleep(0.1)

    def stop(self, why=None):
        if not self.alive:
            return
        self.alive = False
        if getattr(self, "timer", None) is not None and threading.current_thread() is not self.timer:
            self.timer.cancel()
        p, self.p = self.p, None
        if p is not None:
            # end the process first: the feeder may be blocked writing into its full pipe (pw-play reads nothing while
            # it is not linked), and closing the pipe under it would wait for that write forever
            p.terminate()
            try:
                p.wait(2)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait(2)
            try:
                p.stdin.close()
            except (OSError, ValueError):
                pass
        self.log(f"inject {self.id} -> {self.label} stopped" + (f" ({why})" if why else ""))

    def info(self):
        return {"id": self.id, "label": self.label, "node": self.node, "kind": self.gen.kind, "level_db": self.gen.level_db,
                "freq_hz": self.gen.freq if self.gen.kind == "sine" else None, "channels": self.gen.ch,
                "started": self.started, "ends_at": self.ends_at, "remaining_s": max(0, round(self.ends_at - time.time())),
                "playing": self.alive}


class Injector:
    """The injections of this host's PipeWire graph."""

    def __init__(self, log=print):
        self.log, self.lock, self.items, self.seq = log, threading.Lock(), {}, itertools.count(1)

    def start(self, node, ports=None, label=None, kind="white", level_db=DEFAULT_DB, freq_hz=1000.0, duration_s=30):
        g = pw.dump()
        if not g.node(node):
            raise InjectError(f"{label or node}: its PipeWire node {node} is not there now")
        dp = g.node_ports(node, "in", idx=ports)
        if not dp:
            raise InjectError(f"{label or node}: {node} has no input ports to play into")
        with self.lock:
            self._reap()
            if len(self.items) >= MAX_INJECTIONS:
                raise InjectError(f"{MAX_INJECTIONS} injections are already playing; stop one first")
            iid = f"i{next(self.seq)}"
            inj = Injection(iid, node, ports, label or node, kind, level_db, freq_hz, duration_s, min(len(dp), 8), self.log)
            self.items[iid] = inj
        inj.start()
        self.log(f"inject {iid}: {kind} at {inj.gen.level_db:g} dBFS for {inj.duration:g} s -> {inj.label} ({node})")
        return inj.info()

    def _reap(self):
        for k in [k for k, v in self.items.items() if not v.alive]:
            del self.items[k]

    def get(self, iid):
        inj = self.items.get(iid)
        if not inj or not inj.alive:
            raise InjectError(f"no injection {iid} (it ended?)")
        return inj

    def set_level(self, iid, level_db):
        inj = self.get(iid)
        inj.gen.set_level(level_db)
        return inj.info()

    def stop(self, iid):
        self.get(iid).stop("stopped")
        return {"ok": True, "id": iid}

    def active(self):
        with self.lock:
            self._reap()
            return [v.info() for v in self.items.values()]

    def stop_all(self):
        for v in list(self.items.values()):
            v.stop("shutting down")


def resolve_input(dep, bid, pid):
    """A canvas port (block id, port id) -> {host, node, ports, label} of a PipeWire INPUT to inject into."""
    hub, d = dep.cfg.name, dep.desired
    design = dep.design or {}
    dblock = next((b for b in design.get("blocks") or [] if b.get("id") == bid), None)
    if dblock is not None:
        if d is None:
            raise InjectError(f"{bid} is not deployed yet: deploy, then inject")
        bt = dep.types.get(dblock.get("type"))
        name = dblock.get("label") or (bt.label if bt else bid)
        label = f"{name} · {pid}"
        if bt is None:
            raise InjectError(f"{label}: unknown block type")
        if bt.engine == "voice":
            raise InjectError(f"{label}: the assistant records from whatever is wired into it; inject into that (the mic or the AEC)")
        spec = bt.node_map(bt.ctx(bid, dblock.get("label"), dblock.get("params"))).get(pid)
        if bid in (d.devices or {}):
            if not d.devices[bid].get("node"):
                raise InjectError(f"{label}: the device is not plugged in")
            return {"host": d.block_host.get(bid, hub), "node": d.devices[bid]["node"], "ports": None, "label": label}
        if not spec or spec.get("dir") != "in":
            raise InjectError(f"{label}: not an input; drop the injector on an input (the left side of a block)")
        return {"host": d.block_host.get(bid, hub), "node": spec["node"], "ports": spec.get("ports"), "label": label}
    blk, port = dep.inv.port(bid, pid)
    if not blk:
        raise InjectError(f"{bid}: not in the inventory now (unplugged, or an app that is not playing)")
    label = f"{blk.get('label') or bid} · {(port or {}).get('label') or pid}"
    if not port or port.get("dir") != "in":
        raise InjectError(f"{label}: not an input; drop the injector on an input (the left side of a block)")
    if "pw" not in port:
        raise InjectError(f"{label}: an NMOS receiver is fed by the network; inject into the sender's machine instead")
    return {"host": blk.get("host") or hub, "node": port["pw"]["node"], "ports": None, "label": label}
