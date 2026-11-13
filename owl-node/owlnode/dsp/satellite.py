"""D3: processing on the satellites.

A satellite's `dsp` role is a PipeWire *executor*, not a designer: the hub compiles the design, splits off each
satellite's **share** (the block processes placed on that host, the links between its own PipeWire nodes, its meter
taps and the live control values) and pushes it over HTTP. The satellite applies it against its own PipeWire daemon
(PipeWire 1.2 on pinode: the block processes are `pipewire -c` clients, like the audio role's), persists it in
/var/lib/owl-node/dsp-share.json, restores it after a restart on its own, and reports its graph, the state of every
share link and the meter levels back. One deployer in the house (the hub's); the satellites keep their piece true.

  Hub side        SatClient: one polling thread per satellite with a dsp role (status at 2 Hz for live meters, the
                  graph at 1 Hz for the inventory), put_share(), control(), clear()
  Satellite side  ShareDeployer: apply/reconcile/control/status of one share; the server's satellite endpoints:
                    GET  /api/dsp/graph    this host's pw-dump (nodes, ports, links) for the hub's inventory
                    GET  /api/dsp/status   share digest, processes, wire states, meter taps, errors
                    GET  /api/dsp/share    PUT /api/dsp/share {share}   apply (or clear with an empty share)
                    POST /api/dsp/control  {set: [[node, control, value], ...], procs: {block: conf}}  live values
                    POST /api/dsp/clear

Share format (built by the compiler, see compiler.Desired.shares; `digest` identifies it on both sides):
  {design, host, blocks: {id: {type, label, params}}, procs: {id: conf}, block_nodes: {id: [node names]},
   links: [{src: {node, monitor, ports}, dst: {node, ports}, wire}], taps: {name: {node, monitor, ports, channels, ...}},
   controls: [[node, control, value]], digest}
"""
import hashlib, json, os, threading, time, urllib.error, urllib.request

from .. import config
from ..audio import Proc
from ..config import atomic_write, read_json
from . import pw

SHARE = os.path.join(config.STATE_DIR, "dsp-share.json")
PW_DIR = os.path.join(config.RUN_DIR, "dsp")
NODE_WAIT = 8
KEEP_PROPS = ("node.name", "media.class", "node.description", "node.nick", "application.name", "device.api",
              "priority.driver", "node.passive", "owl.dsp.block", "owl.dsp.meter", "audio.channels", "object.serial", "alsa.card")


def log(*a):
    print(time.strftime("%H:%M:%S"), "dsp:", *a, flush=True)


def share_digest(share):
    """Stable over what the satellite must run; `digest`, `design` and timing fields do not count."""
    # not `blocks` (informational: its params change type after a live set, 0 -> 0.0, and would force a re-push)
    core = {k: share.get(k) for k in ("procs", "links", "taps", "block_nodes")}
    core["controls"] = sorted([list(c) for c in share.get("controls") or []], key=lambda c: (c[0], c[1]))
    return hashlib.sha1(json.dumps(core, sort_keys=True).encode()).hexdigest()[:12]


def empty_share(host, design=None):
    return {"design": design, "host": host, "blocks": {}, "procs": {}, "block_nodes": {}, "links": [], "taps": {}, "controls": []}


def graph_objects(timeout=10):
    """pw-dump reduced to what pw.Graph() reads, small enough to ship every second."""
    import subprocess
    out = subprocess.run(["pw-dump", "--no-colors"], capture_output=True, timeout=timeout, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"pw-dump: {out.stderr.strip()[:200]}")
    objs = []
    for o in pw.parse_dump(out.stdout):
        t = o.get("type")
        info = o.get("info") or {}
        if t == "PipeWire:Interface:Node":
            p = info.get("props") or {}
            objs.append({"id": o["id"], "type": t, "info": {"state": info.get("state"), "props": {k: p[k] for k in KEEP_PROPS if k in p}}})
        elif t == "PipeWire:Interface:Port":
            p = info.get("props") or {}
            objs.append({"id": o["id"], "type": t, "info": {"props": {k: p[k] for k in ("node.id", "port.name", "port.direction", "port.id", "port.monitor", "audio.channel") if k in p}}})
        elif t == "PipeWire:Interface:Link":
            objs.append({"id": o["id"], "type": t, "info": {k: info.get(k) for k in ("output-port-id", "input-port-id", "output-node-id", "input-node-id", "state")}})
    return objs


# ====================================================================== hub side
def http(method, url, body=None, timeout=3):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.loads(r.read() or b"null")


class SatClient(threading.Thread):
    """The hub's view of one satellite dsp role. Polls its status (meters, wire states, share digest) and graph; the
    deployer pushes shares and live controls through it. `ok` = answered within the last few seconds."""

    def __init__(self, host, url):
        super().__init__(daemon=True, name=f"sat-{host}")
        self.host, self.url = host, url.rstrip("/")
        self.status, self.graph, self.graph_time = None, None, 0
        self.ok, self.error, self.last_ok, self.seen = False, None, 0, False
        self.version = None
        self._stop = threading.Event()
        self.lock = threading.Lock()

    def run(self):
        while not self._stop.is_set():
            try:
                code, st = http("GET", f"{self.url}/api/dsp/status", timeout=1.5)
                if code == 200 and isinstance(st, dict) and st.get("mode") == "satellite":
                    with self.lock:
                        self.status, self.ok, self.error, self.last_ok, self.seen = st, True, None, time.time(), True
                        self.version = st.get("version")
                    if time.time() - self.graph_time > 1.0:
                        code, g = http("GET", f"{self.url}/api/dsp/graph", timeout=2)
                        if code == 200:
                            with self.lock:
                                self.graph, self.graph_time = pw.Graph(g.get("objects") or []), time.time()
                else:
                    self._fail(f"HTTP {code}" if code != 200 else "not a satellite dsp role")
            except Exception as e:
                self._fail(f"{type(e).__name__}: {str(e)[:80]}")
            self._stop.wait(0.5 if self.ok else 5.0)

    def _fail(self, why):
        with self.lock:
            if self.ok:
                log(f"satellite {self.host}: lost ({why})")
            self.ok, self.error = False, why

    def stop(self):
        self._stop.set()

    def put_share(self, share, timeout=30):
        try:
            code, r = http("PUT", f"{self.url}/api/dsp/share", {"share": share}, timeout=timeout)
            if code == 200 and r.get("ok"):
                self.poke()
                return True, r
            return False, (r.get("error") if isinstance(r, dict) else str(r)) or f"HTTP {code}"
        except Exception as e:
            return False, f"{self.host} unreachable: {type(e).__name__}: {str(e)[:80]}"

    def control(self, sets, procs=None):
        try:
            code, r = http("POST", f"{self.url}/api/dsp/control", {"set": sets, "procs": procs or {}}, timeout=5)
            return code == 200 and r.get("ok"), r
        except Exception as e:
            return False, f"{self.host} unreachable: {type(e).__name__}"

    def poke(self):
        """Refresh the status right away (after a push)."""
        try:
            code, st = http("GET", f"{self.url}/api/dsp/status", timeout=2)
            if code == 200:
                with self.lock:
                    self.status, self.ok, self.last_ok = st, True, time.time()
        except Exception:
            pass

    def share_digest(self):
        return ((self.status or {}).get("share") or {}).get("digest")

    def public(self):
        st = self.status or {}
        return {"host": self.host, "url": self.url, "ok": self.ok, "error": self.error, "version": self.version,
                "share": st.get("share"), "pipewire": st.get("pipewire"), "procs": len(st.get("procs") or {}),
                "last_ok": self.last_ok}


# ====================================================================== satellite side
class ShareDeployer:
    """One share, kept true against this host's PipeWire. Mirrors the hub's Deployer for processes, links and taps
    (the hub's GraphOps), without patches, moves, inventory or a registry."""

    def __init__(self, cfg, taps):
        from .deploy import GraphOps
        self.ops = GraphOps(taps)
        self.cfg, self.taps = cfg, taps
        self.lock = threading.RLock()
        self.persist_share = pw.Debounced(self._write_share, 1.0)      # live controls: the file once a second
        self.share = read_json(SHARE, None) or empty_share(cfg.name)
        self.share.setdefault("digest", share_digest(self.share))
        self.applied_at, self.error, self.events, self.repairs = None, None, [], 0
        self.last_graph_error = None
        os.makedirs(PW_DIR, exist_ok=True)
        if self.share.get("procs") or self.share.get("links") or self.share.get("taps"):
            try:
                self.apply(self.share, restore=True)
            except Exception as e:
                self.error = str(e)
                self.event(f"restore of the saved share failed: {e} (the reconcile keeps trying)")

    @property
    def procs(self):
        return self.ops.procs

    def event(self, msg):
        log(msg)
        self.events.insert(0, {"time": time.time(), "msg": msg})
        del self.events[40:]

    def _taps_for(self, share, g):
        want = {}
        for name, t in (share.get("taps") or {}).items():
            ch = t.get("channels") or len(g.node_ports(t["node"], "out", monitor=bool(t.get("monitor")), idx=t.get("ports"))) or 2
            want[name] = ch
        return want

    def apply(self, share, restore=False):
        with self.lock:
            t0 = time.time()
            share = dict(share)
            share["host"] = self.cfg.name
            share["digest"] = share_digest(share)
            self.ops.start_procs(share.get("procs") or {})
            g, missing = self.ops.wait_nodes([n for bid in (share.get("procs") or {}) for n in (share.get("block_nodes") or {}).get(bid, [])])
            if missing and not restore:
                raise RuntimeError(f"block nodes did not appear: {', '.join(missing)}")
            self.taps.want(self._taps_for(share, g))
            g, _ = self.ops.wait_nodes(self.taps.nodes(), timeout=4)
            owned = []
            links = list(share.get("links") or []) + self.ops.tap_links(share.get("taps") or {})
            miss, created, pending = self.ops.ensure_links(g, links, owned)
            if miss and not restore:
                raise RuntimeError("links could not be made: " + "; ".join(f"{l['src']['node']} -> {l['dst']['node']} {l.get('error', '')}" for l in miss))
            stale = [r for r in (self.share.get("owned") or []) if r not in owned]
            removed = self.ops.remove_links(pw.dump(), stale) if stale else 0
            for node, ctl, val in share.get("controls") or []:
                n = g.node(node)
                if n:
                    pw.set_control(n["id"], ctl, val)
            share["owned"] = owned
            self.share, self.applied_at, self.error = share, time.time(), None
            atomic_write(SHARE, share)
            self.event(f"{'restored' if restore else 'applied'} share of {share.get('design')!r} ({share['digest']}) in {time.time() - t0:.1f} s: "
                       f"{len(share.get('procs') or {})} block process(es), {len(share.get('links') or [])} link(s) ({created} made, {removed} removed), "
                       f"{len(share.get('taps') or {})} tap(s)" + (f", {len(pending)} pending" if pending else "") + (f", {len(missing)} node(s) missing" if missing else ""))
            return {"ok": True, "digest": share["digest"], "seconds": round(time.time() - t0, 1), "links_created": created,
                    "pending": [l["wire"] for l in pending], "missing_nodes": missing}

    def clear(self):
        return self.apply(empty_share(self.cfg.name))

    def _write_share(self):
        with self.lock:
            atomic_write(SHARE, self.share)

    def control(self, sets, procs):
        # live: straight to PipeWire (cached node ids, the long-lived pw-cli), not behind the reconcile's lock
        done = self.ops.send_controls([tuple(x) for x in sets or []])
        with self.lock:
            # the running process record and the saved share follow the live values (no restart; a crash restart or
            # a reboot comes back with them)
            for bid, conf in (procs or {}).items():
                if bid in self.procs:
                    self.procs[bid].conf = conf
                self.share.setdefault("procs", {})[bid] = conf
            for node, ctl, val in sets or []:
                cs = self.share.setdefault("controls", [])
                cs[:] = [c for c in cs if not (c[0] == node and c[1] == ctl)] + [[node, ctl, val]]
            self.share["digest"] = share_digest(self.share)
        self.persist_share()
        return {"ok": True, "controls_set": done, "digest": self.share["digest"]}

    def reconcile(self):
        with self.lock:
            for p in self.procs.values():
                was = p.running()
                p.check()
                if not was and p.running():
                    self.event(f"block process {p.name} restarted")
            try:
                g = self.ops.remember(pw.dump())
                self.last_graph_error = None
            except Exception as e:
                self.last_graph_error = str(e)
                return
            self.taps.want(self._taps_for(self.share, g))
            owned = []
            links = list(self.share.get("links") or []) + self.ops.tap_links(self.share.get("taps") or {})
            missing, created, pending = self.ops.ensure_links(g, links, owned)
            if created:
                self.repairs += created
                self.event(f"re-made {created} link(s)")
            if owned != self.share.get("owned"):
                self.share["owned"] = owned
                atomic_write(SHARE, self.share)

    def status(self):
        with self.lock:
            try:
                g = pw.dump()
            except Exception as e:
                g = pw.Graph([])
                self.last_graph_error = str(e)
            have = g.link_set()
            wires = {}
            for l in self.share.get("links") or []:
                w = wires.setdefault(l["wire"], {"state": "ok", "detail": []})
                if not g.node(l["src"]["node"]) or not g.node(l["dst"]["node"]):
                    w["state"] = "pending"
                    w["detail"].append(f"waiting for {l['src']['node'] if not g.node(l['src']['node']) else l['dst']['node']} on {self.cfg.name}")
                    continue
                pairs = self.ops.resolve(g, l)
                bad = [1 for sp, dp in pairs if (sp["id"], dp["id"]) not in have]
                if bad or not pairs:
                    w["state"] = "missing"
                    w["detail"].append(f"{len(bad)} of {len(pairs)} links missing on {self.cfg.name}")
            tstats = self.taps.stats()
            taps = {name: dict(t, stats=tstats.get(name)) for name, t in (self.share.get("taps") or {}).items()}
            procs = {bid: dict(p.info(), nodes={n: bool(g.node(n)) for n in (self.share.get("block_nodes") or {}).get(bid, [])})
                     for bid, p in self.procs.items()}
            from .. import VERSION
            return {"mode": "satellite", "host": self.cfg.name, "version": VERSION, "time": time.time(),
                    "share": {"digest": self.share.get("digest"), "design": self.share.get("design"), "applied_at": self.applied_at,
                              "blocks": sorted((self.share.get("blocks") or {}).keys()), "error": self.error},
                    "procs": procs, "wires": wires, "taps": taps, "repairs": self.repairs, "events": self.events[:20],
                    "pw_error": self.last_graph_error}

    def stop(self):
        self.persist_share.flush()
        self.ops.pwctl.stop()
        with self.lock:
            for p in self.procs.values():
                p.stop()
            self.taps.stop()
