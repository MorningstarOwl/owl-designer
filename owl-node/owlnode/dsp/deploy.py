"""Deploy a design and keep it true.

Files in /var/lib/owl-node:  dsp-design.json (the deployed design), dsp-design.prev.json (the one before it, for
rollback), dsp-state.json (what the deployer owns: links, patches, allocation, procs), dsp-draft.json (the canvas's
work in progress, shared between browsers). /run/owl-node/dsp/ holds the generated PipeWire configs.

apply(): compile -> start/stop/restart block processes -> IS-05 patches -> PipeWire links -> meter taps -> verify
(every node and link present) and, when verification fails, re-apply the previous design and raise. Idempotent: the
same design again touches nothing. reconcile() runs every 2 s: restarts crashed processes, re-creates links whose
nodes came back (a reboot of the hub or a satellite), re-patches a receiver the far end lost, and reports.
Removing a wire removes only what the compiler had created for it; links WirePlumber or apps made are never touched."""
import copy, json, os, subprocess, threading, time
from collections import deque

from .. import config
from ..audio import Proc
from ..config import atomic_write, read_json
from . import pw
from .blocks import CMD_PREFIX
from .compiler import CompileError, compile_design
from .satellite import share_digest

DESIGN = os.path.join(config.STATE_DIR, "dsp-design.json")
PREV = os.path.join(config.STATE_DIR, "dsp-design.prev.json")
STATE = os.path.join(config.STATE_DIR, "dsp-state.json")
DRAFT = os.path.join(config.STATE_DIR, "dsp-draft.json")
PW_DIR = os.path.join(config.RUN_DIR, "dsp")
PATCH_GRACE = 15        # s a far end gets to restore its own patch before we re-patch
SHARE_RETRY = 5         # s between pushes of a satellite's share while it does not match
NODE_WAIT = 8           # s for a started process's nodes / a patched receiver's node to appear


def log(*a):
    print(time.strftime("%H:%M:%S"), "dsp:", *a, flush=True)


def tap_name(node, monitor):
    return (node + ("-mon" if monitor else "")).replace(".", "_").replace(":", "_").lower()[:40]


class DeployError(Exception):
    pass


def control_aliases(prev, new, types):
    """{old control name: key} for blocks whose label changed between two deployed designs (same id and type), so
    the old `<label>.<param>` names keep resolving until the next deploy."""
    from . import blocks as B
    out = {}
    if not prev or not new:
        return out
    cur = {b["id"]: b for b in prev.get("blocks", [])}
    for b in new.get("blocks", []):
        old = cur.get(b["id"])
        bt = types.get(b.get("type"))
        if not old or not bt or old.get("type") != b.get("type"):
            continue
        ol, nl = B.slug(old.get("label") or bt.label), B.slug(b.get("label") or bt.label)
        if ol != nl:
            for p in bt.params:
                out[f"{ol}.{p}"] = f"{b['id']}.{p}"
    return out


class CmdProc(Proc):
    """A block process that is a command line (blocks.CMD_PREFIX + JSON argv: the RTSP block's audio puller), with the
    same interface and restart-with-backoff as a `pipewire -c` block process."""

    def start(self):
        import sys as _sys
        argv = json.loads(self.conf[len(CMD_PREFIX):])
        log(f"start {self.name} ({self.digest})")
        self.p = subprocess.Popen(argv, stdout=_sys.stdout, stderr=_sys.stderr, start_new_session=True)
        self.since = time.time()


def make_proc(bid, conf):
    return CmdProc(bid, conf) if conf.startswith(CMD_PREFIX) else Proc(bid, conf)


class StreamWatcher(threading.Thread):
    """D3 (D2's open item 10): moved app streams (wire "move": true) re-targeted the moment they appear, not at the
    next 2 s reconcile. `pw-mon` streams every object event; a node whose name is in `want` gets its target.object set
    before WirePlumber has linked it anywhere (the first announcement after the announce player restarts was reaching
    the speaker un-ducked for ~2 s: WirePlumber 0.5 does not persist target.object for these streams, see the box's
    restore-stream state). pw-mon's initial dump re-asserts existing streams too (idempotent)."""

    def __init__(self, on_event):
        super().__init__(daemon=True, name="stream-watcher")
        self.want = {}                   # node name -> target node name
        self.on_event = on_event
        self.p, self.hits, self.started = None, 0, None
        self._stop = threading.Event()

    def set(self, moves):
        self.want = {m["node"]: m["target"] for m in moves or []}

    def run(self):
        while not self._stop.is_set():
            try:
                self.p = subprocess.Popen(["pw-mon", "-N", "-p", "-a"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                          text=True, start_new_session=True)
                self.started = time.time()
                block = []
                for line in self.p.stdout:
                    if line.strip():
                        block.append(line)
                        continue
                    self._block(block)
                    block = []
            except Exception as e:
                log(f"stream watcher: {type(e).__name__}: {e}")
            self._stop.wait(2)

    def _block(self, lines):
        if not lines or not lines[0].startswith("added:") or not any("type: PipeWire:Interface:Node" in l for l in lines):
            return
        name = next((l.split('"', 2)[1] for l in lines if "node.name = " in l and '"' in l), None)
        if not name or name not in self.want:
            return
        nid = next((l.split(":", 1)[1].strip() for l in lines if l.strip().startswith("id:")), None)
        if not nid:
            return
        target = self.want[name]
        try:
            if pw.get_target(nid) != target:
                ok, err = pw.set_target(nid, target)
                self.hits += 1
                self.on_event(f"stream {name} (node {nid}) appeared: moved into {target} at once" + ("" if ok else f" FAILED {err}"))
        except Exception as e:
            log(f"stream watcher {name}: {type(e).__name__}: {e}")

    def running(self):
        return self.p is not None and self.p.poll() is None

    def stop(self):
        self._stop.set()
        if self.running():
            self.p.terminate()


class GraphOps:
    """Block processes, PipeWire links and meter taps against one PipeWire daemon: what the hub's Deployer and a
    satellite's ShareDeployer (satellite.py) have in common."""

    def __init__(self, taps):
        self.taps, self.procs = taps, {}

    def _full_conf(self, conf):
        return conf

    def start_procs(self, confs):
        """confs: {block id: pipewire config}; stops what is not wanted or changed, starts the rest."""
        for bid in list(self.procs):
            if bid not in confs or self.procs[bid].conf != self._full_conf(confs[bid]):
                self.procs.pop(bid).stop()
        for bid, conf in confs.items():
            if bid not in self.procs:
                self.procs[bid] = make_proc(bid, self._full_conf(conf))
                self.procs[bid].path = os.path.join(PW_DIR, f"{bid}.conf")
                self.procs[bid].start()

    def wait_nodes(self, names, timeout=NODE_WAIT):
        """Until every named node exists *with ports* (ports arrive a moment after the node)."""
        t0 = time.time()
        while True:
            g = pw.dump()
            missing = [n for n in names if not g.node(n) or not g.ports.get(g.node(n)["id"])]
            if not missing or time.time() - t0 > timeout:
                return g, missing
            time.sleep(0.3)

    # ------------------------------------------------------------------ links
    def resolve(self, g, l):
        s, d = l["src"], l["dst"]
        sp = g.node_ports(s["node"], "out", monitor=bool(s.get("monitor")), idx=s.get("ports"))
        dp = g.node_ports(d["node"], "in", idx=d.get("ports"))
        return pw.pair_ports(sp, dp)

    def ensure_links(self, g, links, owned, create=True):
        """Create the missing port links of `links`; returns (missing_after, created, pending nodes)."""
        have = g.link_set()
        missing, created, pending = [], 0, []
        for l in links:
            sn, dn = g.node(l["src"]["node"]), g.node(l["dst"]["node"])
            if not sn or not dn or not g.ports.get(sn["id"]) or not g.ports.get(dn["id"]):
                pending.append(l)                      # a node (or its ports) is not there yet
                continue
            pairs = self.resolve(g, l)
            if not pairs:
                missing.append(dict(l, error="no matching ports"))
                continue
            for sp, dp in pairs:
                key = (sp["id"], dp["id"])
                rec = {"out_node": l["src"]["node"], "out_port": sp["name"], "in_node": l["dst"]["node"], "in_port": dp["name"]}
                if l.get("wp"):
                    if key not in have:
                        pending.append(l)              # WirePlumber links a moved stream itself (target.object); not ours
                    continue
                if rec not in owned:
                    owned.append(rec)
                if key in have:
                    continue
                if not create:
                    missing.append(l)
                    continue
                ok, err = pw.link(sp["id"], dp["id"])
                if ok:
                    created += 1
                else:
                    missing.append(dict(l, error=err))
        return missing, created, pending

    def remove_links(self, g, recs):
        n = 0
        for l in g.links:
            for r in recs:
                if (l["out_node"], l["out_port"], l["in_node"], l["in_port"]) == (r["out_node"], r["out_port"], r["in_node"], r["in_port"]):
                    ok, _ = pw.unlink(l["id"])
                    n += ok
        return n

    def tap_links(self, taps):
        return [{"src": {"node": t["node"], "monitor": t["monitor"], "ports": t.get("ports")},
                 "dst": {"node": f"owl-meter-{name}", "ports": None}, "wire": f"meter:{name}"} for name, t in taps.items()]

    def tap_channels(self, g, taps):
        out = {}
        for name, t in taps.items():
            ch = t.get("channels")
            if not ch:
                ch = len(g.node_ports(t["node"], "out", monitor=t["monitor"], idx=t.get("ports"))) or 2
            out[name] = ch
        return out


class Deployer:
    def __init__(self, cfg, inv, types, taps, controller):
        self.cfg, self.inv, self.types, self.taps, self.ctl = cfg, inv, types, taps, controller
        self.lock = threading.RLock()
        self.ops = GraphOps(taps)
        self.sats = inv.sats                # host -> SatClient (satellite dsp roles found by the inventory)
        self.share_pending = {}             # host -> why its share is not applied (unreachable); reconcile retries
        self.share_retry = {}
        self._setup_done = {}               # device block id -> params signature its setup commands ran for
        self.state = read_json(STATE, None) or {"links": [], "patches": {}, "alloc": {}, "procs": [], "taps": [],
                                                 "adopted": [], "deployed_at": None}
        self.design = read_json(DESIGN, None)
        self.desired = None
        self.repairs, self.events = 0, deque(maxlen=40)
        self.patch_bad_since = {}
        self.last_graph_error = None
        self.design_error, self.restore_next = None, 0
        self.on_change = None               # set by Controls: (kind, **info) after apply/rollback/restore and per param
        os.makedirs(PW_DIR, exist_ok=True)
        self.watcher = StreamWatcher(self.event)
        self.watcher.start()
        if self.design:
            self._restore()

    @property
    def procs(self):
        return self.ops.procs

    def _restore(self):
        """Compile and start the saved design. At boot the registry and the hub's own agent come up alongside this
        role, so the inventory can be empty for a while: on failure keep the error and retry from reconcile()."""
        try:
            # satellites the design uses: give their probes a moment (the inventory starts them on its first refresh),
            # or every wire of theirs restores as pending and the design is re-applied 5 s later
            hosts = {b.get("host") for b in self.design.get("blocks") or [] if b.get("host") and b.get("host") != self.cfg.name}
            hosts |= {bid.split(":")[1] for w in self.design.get("wires") or [] for bid in (w.get("from", "").rsplit("|", 1)[0], w.get("to", "").rsplit("|", 1)[0])
                      if bid.startswith("pw:") and bid.split(":")[1] != self.cfg.name}
            for _ in range(20):
                if not hosts or hosts <= set(self.inv.refresh(0).sats):
                    break
                time.sleep(0.25)
            self.desired = self._compile(self.design, strict=False)
            self.desired._design = self.design
            self.ops.start_procs(self.desired.procs)
            self.watcher.set(self.desired.moves)
            self._push_shares(self.desired, strict=False)
            self.design_error = None
            self.recompile_next = time.time() + 5
            self._notify("restore")
            self.event(f"restored design {self.design.get('name')!r} ({len(self.design.get('wires') or [])} wires, "
                       f"{len(self.desired.procs)} block process(es)"
                       + (f", {len(self.desired.pending)} wire(s) pending: " + "; ".join(self.desired.pending.values()) if self.desired.pending else "") + ")")
            return True
        except Exception as e:
            if str(e) != self.design_error:
                self.event(f"saved design does not compile yet: {e}")
            self.design_error = str(e)
            self.restore_next = time.time() + 3
            return False

    # ------------------------------------------------------------------ helpers
    def _notify(self, kind, **info):
        if self.on_change:
            try:
                self.on_change(kind, **info)
            except Exception as e:
                log(f"on_change({kind}): {type(e).__name__}: {e}")

    def event(self, msg):
        log(msg)
        self.events.appendleft({"time": time.time(), "msg": msg})

    def _compile(self, design, strict=True):
        inv = self.inv.refresh(0)
        return compile_design(design, inv, self.types, self.state.get("alloc"), strict=strict,
                              prev_tx_alloc=self.state.get("tx_alloc"))

    def _full_conf(self, conf):
        return conf

    def _auto_taps(self, desired):
        """Run-mode meters: every block output, every hub node a wire reads from, every Meter block. One tap per
        (node, monitor, ports): a block output that also feeds a wire is tapped once."""
        taps, seen = {}, set()

        def add(name, node, monitor, ports, **extra):
            key = (node, bool(monitor), tuple(ports) if ports else None)
            if node and not node.startswith("owl-meter-") and key not in seen:
                seen.add(key)
                taps[name] = dict({"node": node, "monitor": bool(monitor), "ports": ports, "channels": None, "auto": True}, **extra)
        for bid, outs in desired.block_outputs.items():
            if desired.block_host.get(bid, self.cfg.name) != self.cfg.name:
                continue                                   # tapped on its satellite (share taps)
            for pid, node, ports, mon in outs:
                add(f"{bid}-{pid}", node, mon, ports, block=bid, port=pid)
        for l in desired.links:
            s_ = l["src"]
            add(tap_name(s_["node"], s_["monitor"]), s_["node"], s_["monitor"], s_.get("ports"))
        for name, t in desired.taps.items():
            taps[name] = dict(t, auto=False)
        return taps

    # ------------------------------------------------------------------ satellites (D3)
    def _share_taps(self, desired, host):
        """The satellite's Run-mode meters, named as the hub's: every output of a block placed there, every node its
        share links read from, and its Meter blocks."""
        sh = desired.shares[host]
        taps, seen = {}, set()

        def add(name, node, monitor, ports, **extra):
            key = (node, bool(monitor), tuple(ports) if ports else None)
            if node and not node.startswith("owl-meter-") and key not in seen:
                seen.add(key)
                taps[name] = dict({"node": node, "monitor": bool(monitor), "ports": ports, "channels": None, "auto": True}, **extra)
        for bid, outs in desired.block_outputs.items():
            if desired.block_host.get(bid) == host:
                for pid, node, ports, mon in outs:
                    add(f"{bid}-{pid}", node, mon, ports, block=bid, port=pid)
        for l in sh["links"]:
            s_ = l["src"]
            add(tap_name(s_["node"], s_["monitor"]), s_["node"], s_["monitor"], s_.get("ports"))
        for name, t in sh["taps"].items():
            taps[name] = dict(t, auto=False)
        return taps

    def _full_share(self, desired, host):
        sh = dict(desired.shares[host])
        sh["taps"] = self._share_taps(desired, host)
        sh["digest"] = share_digest(sh)
        desired.shares[host] = sh
        return sh

    def _push_shares(self, desired, strict):
        """Push every satellite's share; clear the shares of satellites this design no longer uses. strict: an
        unreachable or failing satellite fails the deploy (rollback); else its share stays pending and the reconcile
        retries every SHARE_RETRY s."""
        pushed = {}
        for host in list(desired.shares):
            sh = self._full_share(desired, host)
            c = self.sats.get(host)
            if not c or not c.ok:
                why = f"satellite {host} is unreachable ({c.error if c else 'no dsp role seen yet'})"
                if strict:
                    raise DeployError(why)
                self.share_pending[host] = why
                self.share_retry[host] = time.time() + SHARE_RETRY
                continue
            ok, r = c.put_share(sh)
            if not ok:
                why = f"satellite {host}: {r}"
                if strict:
                    raise DeployError(why)
                self.share_pending[host] = why
                self.share_retry[host] = time.time() + SHARE_RETRY
                self.event(f"share for {host} not applied: {r}")
                continue
            self.share_pending.pop(host, None)
            pushed[host] = sh["digest"]
            self.event(f"share pushed to {host}: {len(sh['procs'])} block process(es), {len(sh['links'])} link(s), {len(sh['taps'])} tap(s) ({sh['digest']})"
                       + (f", pending: {', '.join(r.get('pending') or [])}" if r.get("pending") else ""))
        for host in (self.state.get("shares") or {}):
            if host not in desired.shares and host in self.sats:
                ok, r = self.sats[host].put_share({"design": None, "host": host, "blocks": {}, "procs": {}, "block_nodes": {}, "links": [], "taps": {}, "controls": []})
                self.event(f"share on {host} cleared" if ok else f"could not clear the share on {host}: {r}")
        return pushed

    def _device_setup(self, desired, g, force=False):
        """D3 device blocks: run their setup commands once per (block, param values) while the device is present
        (hub only; a satellite's device blocks are matched there but their setup is not pushed). Best effort."""
        import shlex, subprocess
        for bid, dv in (desired.devices or {}).items():
            if dv["host"] != self.cfg.name or not dv["node"] or not dv["setup"]:
                continue
            if not g.node(dv["node"]):
                self._setup_done.pop(bid, None)
                continue
            sig = json.dumps(dv["params"], sort_keys=True)
            if not force and self._setup_done.get(bid) == sig:
                continue
            self._setup_done[bid] = sig
            for cmd, _ in dv["setup"]:
                try:
                    r = subprocess.run(shlex.split(cmd), capture_output=True, text=True, timeout=10)
                    self.event(f"device {dv['label']}: `{cmd}` -> {'ok' if r.returncode == 0 else 'exit ' + str(r.returncode) + ' ' + (r.stderr or r.stdout).strip()[:120]}")
                except FileNotFoundError:
                    self.event(f"device {dv['label']}: `{cmd}`: tool not found (install it in the image or on the host)")
                except Exception as e:
                    self.event(f"device {dv['label']}: `{cmd}`: {type(e).__name__}: {e}")

    def _reconcile_shares(self):
        if not self.desired or not self.desired.shares:
            return
        now = time.time()
        for host, sh in self.desired.shares.items():
            c = self.sats.get(host)
            if not c:
                continue
            want = sh.get("digest") or self._full_share(self.desired, host)["digest"]
            if c.ok and c.share_digest() == want:
                if host in self.share_pending:
                    self.share_pending.pop(host, None)
                    self.event(f"share on {host} is current again")
                continue
            if now < self.share_retry.get(host, 0):
                continue
            self.share_retry[host] = now + SHARE_RETRY
            if not c.ok:
                self.share_pending[host] = f"satellite {host} is unreachable ({c.error})"
                continue
            ok, r = c.put_share(self._full_share(self.desired, host))
            if ok:
                self.share_pending.pop(host, None)
                self.repairs += 1
                self.event(f"share re-pushed to {host} (it had {c.share_digest() or 'none'})")
            else:
                self.share_pending[host] = f"satellite {host}: {r}"
                self.event(f"share for {host} still not applied: {r}")

    def tap_stats(self):
        """Meter readings from the hub's taps and every satellite's (the satellite reports its share's taps)."""
        out = dict(self.taps.stats())
        for host, c in self.sats.items():
            st = c.status if c.ok else None
            for name, t in ((st or {}).get("taps") or {}).items():
                if t.get("stats") is not None:
                    out[name] = dict(t["stats"], host=host)
        return out

    # ------------------------------------------------------------------ patches
    def _sub(self, rid):
        r = self.inv.receivers.get(rid) or {}
        s = r.get("subscription") or {}
        return s.get("sender_id") if s.get("active") else None

    # ------------------------------------------------------------------ RTSP sources (MediaMTX relay + own transport files)
    MTX = os.environ.get("OWL_MEDIAMTX_API", "http://127.0.0.1:9997").rstrip("/")

    def _mtx(self, method, path, body=None, timeout=3):
        import urllib.request, urllib.error
        req = urllib.request.Request(self.MTX + path, data=json.dumps(body).encode() if body is not None else None,
                                     method=method, headers={"Content-Type": "application/json"} if body is not None else {})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                b = r.read()
                return r.status, (json.loads(b) if b else None)
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode(errors="replace")[:200]

    def _tls_fingerprint(self, url, refresh=False):
        """rtsps:// cameras and NVRs present self-signed certificates, which MediaMTX accepts only with the certificate's
        SHA-256 fingerprint (sourceFingerprint). Read on the first deploy that uses the camera and pinned (state
        "fingerprints", per host:port); a later Deploy re-reads it (refresh) so a regenerated certificate is picked up
        by redeploying, not silently."""
        import hashlib, ssl
        from urllib.parse import urlsplit
        u = urlsplit(url)
        key = f"{u.hostname}:{u.port or 322}"
        pins = self.state.setdefault("fingerprints", {})
        if key in pins and not refresh:
            return pins[key]
        try:
            pem = ssl.get_server_certificate((u.hostname, u.port or 322), timeout=5)
            fp = hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest().upper()
        except Exception as e:
            self.event(f"TLS certificate of {key} not readable ({type(e).__name__}: {e}): the video relay cannot pull it")
            return pins.get(key)
        if pins.get(key) not in (None, fp):
            self.event(f"TLS certificate of {key} changed: re-pinned on this deploy")
        pins[key] = fp
        return fp

    def _sync_mediamtx(self, desired, refresh_pins=False):
        """MediaMTX paths owl-cam-<block> = the design's RTSP blocks with video (pulled once, served over SRT and
        WebRTC); paths of ours that the design no longer has are removed. Idempotent (reconcile calls it too: paths
        added over the API do not survive a MediaMTX restart)."""
        want = {v["path"]: v for v in (getattr(desired, "rtsp_video", None) or {}).values()} if desired else {}
        try:
            code, lst = self._mtx("GET", "/v3/config/paths/list?itemsPerPage=500")
        except Exception as e:
            if want:
                self.event(f"MediaMTX API unreachable ({type(e).__name__}): RTSP video relay not configured")
            return
        have = {p["name"]: p for p in ((lst or {}).get("items") or [])} if code == 200 and isinstance(lst, dict) else {}
        for name, v in want.items():
            conf = {"source": v["url"], "rtspTransport": "tcp", "sourceOnDemand": False}
            if v["url"].startswith("rtsps://"):
                fp = self._tls_fingerprint(v["url"], refresh=refresh_pins)
                if fp:
                    conf["sourceFingerprint"] = fp
            cur = have.get(name)
            if cur is None:
                c, r = self._mtx("POST", f"/v3/config/paths/add/{name}", conf)
                self.event(f"MediaMTX path {name} for {v['label']}: " + ("added" if c == 200 else f"HTTP {c} {r}"))
            elif (cur.get("source") != v["url"] or cur.get("rtspTransport") != "tcp"
                  or (conf.get("sourceFingerprint") or "") != (cur.get("sourceFingerprint") or "")):
                c, r = self._mtx("PATCH", f"/v3/config/paths/patch/{name}", conf)
                self.event(f"MediaMTX path {name} for {v['label']}: " + ("updated" if c == 200 else f"HTTP {c} {r}"))
        for name in have:
            if name.startswith("owl-cam-") and name not in want:
                c, _ = self._mtx("DELETE", f"/v3/config/paths/delete/{name}")
                self.event(f"MediaMTX path {name} removed (HTTP {c})")

    def _rtsp_codec(self, v, wait_s=0):
        """h264 | h265 of a relayed camera: the block's setting, else what MediaMTX reads from the stream."""
        if v.get("codec") in ("h264", "h265"):
            return v["codec"]
        t_end = time.time() + wait_s
        while True:
            try:
                c, p = self._mtx("GET", f"/v3/paths/get/{v['path']}")
            except Exception:
                c, p = 0, None
            if c == 200 and isinstance(p, dict):
                names = [str(t if isinstance(t, str) else t.get("codec") or "") for t in (p.get("tracks2") or p.get("tracks") or [])]
                if any("265" in n or "HEVC" in n.upper() for n in names):
                    return "h265"
                if any("264" in n for n in names):
                    return "h264"
            if time.time() >= t_end:
                return None
            time.sleep(0.5)

    def _rtsp_sdp(self, rp, codec):
        """The transport file of a relayed camera: MediaMTX's SRT listener on the hub, MPEG-TS, read:<path>."""
        ip = self.cfg.ip
        sid = int(__import__("hashlib").sha1(rp["path"].encode()).hexdigest()[:8], 16)
        enc = {"h264": "H264", "h265": "H265"}[codec]
        return "\r\n".join([
            "v=0", f"o=- {sid} 1 IN IP4 {ip}", f"s={rp['label']}", "t=0 0",
            "m=application 8890 UDP mp2t", f"c=IN IP4 {ip}",
            f"a=x-owl-srt:listener latency={int(rp['latency_ms'])} streamid=read:{rp['path']}",
            f"a=x-owl-video:{enc} 0x0@0 0kbps", "a=x-owl-source:rtsp", ""])

    def _raw_ok(self, rid):
        r = self.inv.receivers.get(rid) or {}
        s = r.get("subscription") or {}
        return bool(s.get("active")) and not s.get("sender_id")

    def _apply_raw_patches(self, desired, wait_s=8):
        """Receivers patched with a transport file of our own (no NMOS sender behind it): wired from an RTSP block's
        video (MediaMTX's relay), or from an AES67 stream announced over SAP (its own SDP, sap.py). Patched when the
        file changed or the receiver is not playing it; receivers a previous design patched this way and this one does
        not are released."""
        changes, applied = [], {}
        before = dict(self.state.get("raw_patches") or {})
        for rid, rp in (getattr(desired, "raw_patches", None) or {}).items():
            if rp.get("sdp"):                                  # an announced AES67 stream
                dig = __import__("hashlib").sha1(rp["sdp"].encode()).hexdigest()[:10]
                applied[rid] = dig
                if before.get(rid) == dig and self._raw_ok(rid):
                    continue
                code, resp = self.ctl.patch_sdp(rid, rp["sdp"], [{"rtp_enabled": True}])
                if code not in (200, 202):
                    raise DeployError(f"IS-05 patch of receiver {rid} <- {rp['label']} (AES67/SAP): HTTP {code} {str(resp)[:200]}")
                changes.append(f"patched {rid[:8]} <- {rp['label']} (AES67/SAP)")
                continue
            codec = self._rtsp_codec(rp, wait_s=wait_s)
            if codec is None:
                codec = "h264"
                self.event(f"{rp['label']}: the camera's codec is not known yet (stream not up?): assuming H.264; "
                           "set the block's video codec if it is H.265")
            sdp = self._rtsp_sdp(rp, codec)
            dig = __import__("hashlib").sha1(sdp.encode()).hexdigest()[:10]
            applied[rid] = dig
            if before.get(rid) == dig and self._raw_ok(rid):
                continue
            code, resp = self.ctl.patch_sdp(rid, sdp, [{"stream_id": f"read:{rp['path']}", "latency": min(1000, int(rp["latency_ms"]))}])
            if code not in (200, 202):
                raise DeployError(f"IS-05 patch of receiver {rid} <- {rp['label']}: HTTP {code} {str(resp)[:200]}")
            changes.append(f"patched {rid[:8]} <- {rp['label']} (RTSP, {codec})")
        for rid in before:
            if rid not in applied and rid not in desired.patches and rid in self.inv.receivers and self._raw_ok(rid):
                code, _ = self.ctl.patch(rid, None)
                changes.append(f"released {rid[:8]} (no camera wired) -> {code}")
        self._raw_applied = applied
        return changes

    def _rx_override(self, rid):
        """The latency override an owl-node reports for one of its receivers (its /x-owl/v1/status), or '?' unknown."""
        nst = (self.inv.nmos or {}).get("status") or {}
        for st in nst.values():
            r = ((st or {}).get("receivers") or {}).get(rid)
            if r is not None:
                return r.get("latency_override")
        return "?"

    def _apply_latency(self, desired, force=False):
        """Receivers' playout latency (design / wire latency_ms) on their nodes; receivers a previous design set and
        this one does not go back to their node's default. Best effort: a node that is down is retried by reconcile."""
        changes, applied = [], {}
        before = dict(self.state.get("rx_latency") or {})
        want = dict(getattr(desired, "rx_latency", None) or {})
        for rid in set(before) | set(want):
            ms = want.get(rid)
            cur = self._rx_override(rid)
            if not force and cur != "?" and cur == ms:
                if ms is not None:
                    applied[rid] = ms
                continue
            if not force and cur == "?" and before.get(rid) == ms:
                if ms is not None:
                    applied[rid] = ms
                continue
            try:
                code, resp = self.ctl.set_receiver_latency(rid, ms)
            except Exception as e:
                code, resp = 0, str(e)
            if code == 200:
                if ms is not None:
                    applied[rid] = ms
                changes.append(f"receiver {rid[:8]} latency " + (f"{ms:g} ms" if ms is not None else "back to its node's default"))
            else:
                applied[rid] = before.get(rid)           # not done (node down?): kept as it was, reconcile retries
                self.event(f"latency of receiver {rid[:8]} not set (HTTP {code} {str(resp)[:120]}); retrying")
        self.state["rx_latency"] = {k: v for k, v in applied.items() if v is not None}
        return changes

    def _apply_patches(self, desired, owned_before, adopted):
        changes = []
        self.inv.refresh(0)
        for rid, sid in desired.patches.items():
            if self._sub(rid) != sid:
                code, resp = self.ctl.patch(rid, sid)
                if code not in (200, 202):
                    raise DeployError(f"IS-05 patch of receiver {rid} <- {sid}: HTTP {code} {str(resp)[:200]}")
                changes.append(f"patched {rid[:8]} <- {sid[:8]}")
        raw = getattr(desired, "raw_patches", None) or {}
        for rid in set(owned_before) | set(adopted):
            if rid not in desired.patches and rid not in raw and rid in self.inv.receivers and self._sub(rid) is not None:
                code, resp = self.ctl.patch(rid, None)
                changes.append(f"disconnected {rid[:8]} -> {code}")
        return changes

    # ------------------------------------------------------------------ stream moves (wire "move": true)
    def _apply_moves(self, desired, g):
        """Set target.object metadata on moved app streams; clear it on streams a previous design moved and this one
        does not. Returns (changes, pending)."""
        changes, pending, rec = [], [], []
        want = {m["node"]: m["target"] for m in desired.moves}
        for m in desired.moves:
            n = g.node(m["node"])
            if not n:
                pending.append(m["node"])
                continue
            cur = pw.get_target(n["id"])
            if cur != m["target"]:
                ok, err = pw.set_target(n["id"], m["target"])
                changes.append(f"moved {m['node']} -> {m['target']}" + ("" if ok else f" FAILED {err}"))
            rec.append({"node": m["node"], "target": m["target"], "node_id": n["id"]})
        for old in self.state.get("moves") or []:
            if old["node"] not in want:
                n = g.node(old["node"])
                if n:
                    pw.clear_target(n["id"])
                    changes.append(f"released {old['node']} (back to its own output)")
        return changes, pending, rec

    # ------------------------------------------------------------------ plan / apply
    def plan(self, design, strict=True):
        with self.lock:
            try:
                new = self._compile(design, strict=strict)
            except CompileError as e:
                return {"ok": False, "errors": e.errors}
            cur = self.desired
            g = self.inv.graph
            diff = {"procs": {"start": [], "stop": [], "restart": [], "keep": []}, "links": {"add": [], "remove": [], "keep": []},
                    "patches": {"set": [], "clear": [], "keep": []}, "taps": {"start": [], "stop": []}}
            for bid, conf in new.procs.items():
                if bid in self.procs and self.procs[bid].running():
                    (diff["procs"]["keep"] if self.procs[bid].conf == self._full_conf(conf) else diff["procs"]["restart"]).append(bid)
                else:
                    diff["procs"]["start"].append(bid)
            diff["procs"]["stop"] = [b for b in self.procs if b not in new.procs]
            have = {(l["out_node"], l["out_port"], l["in_node"], l["in_port"]) for l in g.links}
            new_recs = []
            for l in new.links:
                pairs = self.ops.resolve(g, l) if g.node(l["src"]["node"]) and g.node(l["dst"]["node"]) else []
                if not pairs:
                    diff["links"]["add"].append(f"{l['src']['node']}{' (monitor)' if l['src'].get('monitor') else ''} -> {l['dst']['node']}"
                                                + ("" if g.node(l["src"]["node"]) and g.node(l["dst"]["node"]) else "  (once its nodes exist)"))
                    continue
                for sp, dp in pairs:
                    k = (l["src"]["node"], sp["name"], l["dst"]["node"], dp["name"])
                    new_recs.append(k)
                    (diff["links"]["keep"] if k in have else diff["links"]["add"]).append(f"{k[0]}:{k[1]} -> {k[2]}:{k[3]}")
            for r in self.state.get("links", []):
                k = (r["out_node"], r["out_port"], r["in_node"], r["in_port"])
                if k[2].startswith("owl-meter-"):
                    continue
                if k in have and k not in new_recs and not any(l["src"]["node"] == k[0] and l["dst"]["node"] == k[2] for l in new.links):
                    diff["links"]["remove"].append(f"{k[0]}:{k[1]} -> {k[2]}:{k[3]}")
            lab = lambda x: (self.inv.receivers.get(x) or self.inv.senders.get(x) or {}).get("label") or x[:8]
            for rid, sid in new.patches.items():
                (diff["patches"]["keep"] if self._sub(rid) == sid else diff["patches"]["set"]).append(f"{lab(rid)} <- {lab(sid)}")
            for rid in set(self.state.get("patches", {})) | set(self.state.get("adopted", [])):
                if rid not in new.patches and self._sub(rid):
                    diff["patches"]["clear"].append(f"{lab(rid)} (now <- {lab(self._sub(rid))})")
            taps = self._auto_taps(new)
            diff["taps"]["start"] = [n for n in taps if n not in self.taps.taps]
            diff["taps"]["stop"] = [n for n in self.taps.taps if n not in taps]
            diff["moves"] = {"set": [], "clear": [], "keep": []}
            old_moves = {m["node"]: m["target"] for m in self.state.get("moves") or []}
            for m in new.moves:
                n = g.node(m["node"])
                cur = pw.get_target(n["id"]) if n else None
                (diff["moves"]["keep"] if cur == m["target"] else diff["moves"]["set"]).append(f"{m['node']} -> {m['target']}" + ("" if n else "  (once the stream exists)"))
            for node, tgt in old_moves.items():
                if node not in {m["node"] for m in new.moves}:
                    diff["moves"]["clear"].append(f"{node} (was -> {tgt}; back to its own output)")
            changes = sum(len(v) for sec in diff.values() for k, v in sec.items() if k != "keep")
            return {"ok": True, "errors": [], "warnings": new.warnings, "diff": diff, "changes": changes,
                    "wires": new.wire_impl, "alloc": new.alloc, "tx_alloc": new.tx_alloc, "pending": new.pending,
                    "impact": self._impact(design, new)}

    def _impact(self, design, new):
        """What a deploy does to the room, in words the Deploy dialog leads with: does it replace the running design
        (another name, or most of its blocks gone), which blocks disappear (and their HA entities / panel controls), and
        what happens to the voice assistant's ears and mouth."""
        cur = self.design or {}
        cur_blocks = {b.get("id"): b for b in cur.get("blocks") or []}
        new_ids = {b.get("id") for b in design.get("blocks") or []}
        removed = [{"id": bid, "label": b.get("label") or b.get("type"), "type": b.get("type")}
                   for bid, b in cur_blocks.items() if bid not in new_ids]
        added = [b.get("id") for b in design.get("blocks") or [] if b.get("id") not in cur_blocks]
        replaces = bool(cur) and ((cur.get("name") or "") != (design.get("name") or "")
                                  or (len(cur_blocks) >= 3 and len(removed) * 2 > len(cur_blocks)))
        lost_controls = 0
        for r in removed:
            bt = self.types.get(r["type"])
            if bt:
                lost_controls += len(bt.params) + (1 if bt.engine == "meter" else len(bt.outputs))
        lost_panel = []
        for pnl in design.get("panels") or cur.get("panels") or []:
            for it in pnl.get("items") or []:
                if str(it.get("key", "")).split(".")[0] in {r["id"] for r in removed}:
                    lost_panel.append(f"{pnl.get('name')}: {it.get('label') or it.get('key')}")
        def ears(desired):
            if not desired:
                return None, None, None
            v = getattr(desired, "voice", None)
            if v:
                return v.get("mic"), v.get("reply"), "block"
            mic = next((n for ns in desired.block_nodes.values() for n in ns if n.endswith("-clean")), None)
            rep = next((n for ns in desired.block_nodes.values() for n in ns if n.endswith("-sc")), None)
            return mic, rep, "guess"
        cm, cr, _ = ears(self.desired)
        nm, nr, how = ears(new)
        voice = {"mic_before": cm, "mic_after": nm, "reply_before": cr, "reply_after": nr, "how": how, "warnings": []}
        if cm and not nm:
            voice["warnings"].append(f"The voice assistant loses its microphone (it listens to {cm} now; nothing after this deploy).")
        elif cm and nm and cm != nm:
            voice["warnings"].append(f"The voice assistant will listen to {nm} instead of {cm}.")
        if cr and not nr:
            voice["warnings"].append(f"Voice replies lose their output ({cr}): they would play on the default speaker without ducking.")
        if how == "guess" and nm:
            voice["warnings"].append("This design has no Voice assistant block: the assistant guesses its mic (the first *-clean output). Add the block and wire it to make that explicit.")
        return {"current_name": cur.get("name"), "new_name": design.get("name"), "replaces": replaces,
                "removed_blocks": removed, "added_blocks": added, "lost_controls": lost_controls, "lost_panel_items": lost_panel,
                "voice": voice}

    def apply(self, design, rollback=True, reason="deploy", strict=True, keep_prev=False):
        """keep_prev: do not move the current design into the rollback slot (the block tests deploy and restore)."""
        with self.lock:
            t0 = time.time()
            try:
                new = self._compile(design, strict=strict)
            except CompileError as e:
                raise DeployError("; ".join(e.errors))
            new._design = design
            owned_links_before = list(self.state.get("links", []))
            owned_patches_before = dict(self.state.get("patches", {}))
            adopted = list((design.get("meta") or {}).get("adopted") or []) + list(self.state.get("adopted", []))
            prev_design, prev_desired = self.design, self.desired
            try:
                self.ops.start_procs(new.procs)
                g, missing = self.ops.wait_nodes([n for bid in new.procs for n in new.block_nodes[bid]])
                if missing:
                    raise DeployError(f"block nodes did not appear: {', '.join(missing)}")
                shares = self._push_shares(new, strict)
                changes = self._apply_patches(new, owned_patches_before, adopted)
                self._sync_mediamtx(new, refresh_pins=(reason == "deploy"))
                changes += self._apply_raw_patches(new)
                changes += self._apply_latency(new)
                rx_nodes = [(self.inv.receivers[r].get("tags") or {}).get("urn:x-owl:pipewire-node", [None])[0]
                            for r in list(new.patches) + list(new.raw_patches)
                            if r in self.inv.receivers and r in {x["id"] for x in self.inv.hub_receivers}]
                g, missing = self.ops.wait_nodes([n for n in rx_nodes if n])
                if missing:
                    raise DeployError(f"hub receiver nodes did not appear after patching: {', '.join(missing)}")
                taps = self._auto_taps(new)
                self.taps.want(self.ops.tap_channels(g, taps))
                g, _ = self.ops.wait_nodes([f"owl-meter-{n}" for n in taps], timeout=4)
                owned = []
                missing, created, pending = self.ops.ensure_links(g, new.links + self.ops.tap_links(taps), owned)
                if missing:
                    raise DeployError("links could not be made: " + "; ".join(f"{l['src']['node']} -> {l['dst']['node']} {l.get('error', '')}" for l in missing))
                stale = [r for r in owned_links_before if r not in owned]
                removed = self.ops.remove_links(pw.dump(), stale)
                mv_changes, mv_pending, mv_rec = self._apply_moves(new, g)
                self.watcher.set(new.moves)
                self._device_setup(new, g)
                changes += mv_changes
                self.state = {"links": owned, "patches": dict(new.patches), "alloc": dict(new.alloc),
                              "tx_alloc": dict(new.tx_alloc), "procs": sorted(new.procs),
                              "rx_latency": dict(self.state.get("rx_latency") or {}),
                              "taps": sorted(taps), "adopted": [], "deployed_at": time.time(), "name": design.get("name"),
                              "moves": mv_rec, "aliases": self.state.get("aliases") or {}, "shares": shares,
                              "raw_patches": dict(getattr(self, "_raw_applied", {}) or {}),
                              "fingerprints": dict(self.state.get("fingerprints") or {})}
                self.desired = new
                if reason == "deploy" and prev_design is not None and prev_design != design and not keep_prev:
                    atomic_write(PREV, prev_design)
                if reason == "deploy":
                    self.state["aliases"] = control_aliases(prev_design, design, self.types)
                    atomic_write(STATE, self.state)
                self.design = copy.deepcopy(design)
                atomic_write(DESIGN, self.design)
                atomic_write(STATE, self.state)
                self.patch_bad_since = {}
                self.recompile_next = time.time() + 5
                self._notify(reason)
                self.event(f"{reason}: {design.get('name')!r} applied in {time.time() - t0:.1f} s: {len(new.procs)} block process(es), "
                           f"{created} link(s) made, {removed} removed, {len(changes)} patch change(s), {len(pending)} pending"
                           + (f", {len(new.pending)} wire(s) wait for a device" if new.pending else ""))
                return {"ok": True, "seconds": round(time.time() - t0, 1), "links_created": created, "links_removed": removed,
                        "patch_changes": changes, "pending": [l["wire"] for l in pending], "warnings": new.warnings,
                        "shares": shares, "shares_pending": dict(self.share_pending)}
            except Exception as e:
                self.event(f"{reason} FAILED: {e}")
                for bid in list(self.procs):            # processes this attempt started and nothing deployed needs
                    if not prev_desired or bid not in prev_desired.procs:
                        self.procs.pop(bid).stop()
                if rollback and prev_design is not None:
                    self.event("rolling back to the previous design")
                    try:
                        self.apply(prev_design, rollback=False, reason="rollback")
                    except Exception as e2:
                        self.event(f"rollback failed too: {e2}")
                raise DeployError(str(e))

    def rollback(self):
        with self.lock:
            prev = read_json(PREV, None)
            if not prev:
                raise DeployError("no previous design to roll back to")
            cur = self.design
            res = self.apply(prev, rollback=False, reason="rollback")
            if cur is not None:
                atomic_write(PREV, cur)         # rollback twice = redo
            return res

    def remove_all(self):
        """Tear down everything the deployer owns (an empty design)."""
        return self.apply({"version": 1, "name": "empty", "blocks": [], "wires": [], "layout": {}}, rollback=False, reason="clear")

    # ------------------------------------------------------------------ reconcile
    def reconcile(self):
        with self.lock:
            for p in self.procs.values():
                was = p.running()
                p.check()
                if not was and p.running():
                    self.event(f"block process {p.name} restarted")
            if not self.desired:
                if self.design and time.time() >= self.restore_next:
                    self._restore()
                if not self.desired:
                    return
            if self.desired.pending and time.time() >= getattr(self, "recompile_next", 0):
                self.recompile_next = time.time() + 5
                try:
                    again = self._compile(self.design, strict=False)
                    if set(again.pending) != set(self.desired.pending):
                        self.event(f"device(s) back for wire(s) {', '.join(sorted(set(self.desired.pending) - set(again.pending)))}: re-applying")
                        self.apply(self.design, rollback=False, reason="device back", strict=False)
                except Exception as e:
                    self.event(f"recompile with pending wires failed: {e}")
            self._reconcile_shares()
            sap = getattr(self.inv, "sap", None)
            if sap is not None and time.time() >= getattr(self, "sap_next", 0):
                self.sap_next = time.time() + 5
                moved = [rp["label"] for rp in (self.desired.raw_patches or {}).values()
                         if rp.get("sdp") and (sap.get(rp.get("stream")) or {}).get("sdp") not in (None, rp["sdp"])]
                if moved:                          # re-announced with another SDP (a Dante reconfiguration, ...)
                    self.event(f"AES67 stream(s) re-announced with a new SDP: {', '.join(moved)}: re-applying")
                    try:
                        self.inv.refresh(0)
                        self.apply(self.design, rollback=False, reason="AES67 stream re-announced", strict=False)
                    except Exception as e:
                        self.event(f"re-apply after a re-announcement failed: {e}")
            if (getattr(self.desired, "rtsp_video", None) or getattr(self.desired, "raw_patches", None)) \
                    and time.time() >= getattr(self, "mtx_next", 0):
                self.mtx_next = time.time() + 15               # RTSP relay: paths and receivers heal themselves
                try:
                    if self.desired.rtsp_video:
                        self._sync_mediamtx(self.desired)
                    if self.desired.raw_patches:
                        self.inv.refresh(0)
                        if any(not self._raw_ok(r) for r in self.desired.raw_patches):
                            ch = self._apply_raw_patches(self.desired, wait_s=0)
                            self.state["raw_patches"] = dict(self._raw_applied)
                            atomic_write(STATE, self.state)
                            if ch:
                                self.event("re-patched receiver(s) (camera / AES67 stream): " + "; ".join(ch))
                except Exception as e:
                    self.event(f"own-SDP patches (camera / AES67 stream): {type(e).__name__}: {e}")
            try:
                g = pw.dump()
                self.last_graph_error = None
            except Exception as e:
                self.last_graph_error = str(e)
                return
            taps = self._auto_taps(self.desired)
            self.taps.want(self.ops.tap_channels(g, taps))
            self._device_setup(self.desired, g)
            owned = []
            missing, created, pending = self.ops.ensure_links(g, self.desired.links + self.ops.tap_links(taps), owned)
            if created:
                self.repairs += created
                self.event(f"re-made {created} link(s)")
            if owned != self.state.get("links"):
                self.state["links"] = owned
                atomic_write(STATE, self.state)
            if self.desired.moves:
                rec = {m["node"]: m for m in self.state.get("moves") or []}
                changed = False
                for m in self.desired.moves:
                    n = g.node(m["node"])
                    if not n:
                        continue
                    if pw.get_target(n["id"]) != m["target"]:          # new stream, a restart of this role, or someone moved it
                        pw.set_target(n["id"], m["target"])
                        self.event(f"stream {m['node']}: moved into {m['target']}")
                    if m["node"] not in rec or rec[m["node"]].get("node_id") != n["id"]:
                        rec[m["node"]] = {"node": m["node"], "target": m["target"], "node_id": n["id"]}
                        changed = True
                if changed:
                    self.state["moves"] = list(rec.values())
                    atomic_write(STATE, self.state)
            # patches: give the far end PATCH_GRACE s to restore its own, then re-patch
            try:
                self.inv.refresh(1.5)
            except Exception:
                return
            now = time.time()
            if (self.desired.rx_latency or self.state.get("rx_latency")) and now >= getattr(self, "lat_next", 0):
                self.lat_next = now + 15
                want = self.desired.rx_latency or {}
                if any(self._rx_override(r) not in ("?", want.get(r)) for r in set(want) | set(self.state.get("rx_latency") or {})) \
                        or any(self.state.get("rx_latency", {}).get(r) != v for r, v in want.items()):
                    ch = self._apply_latency(self.desired)
                    atomic_write(STATE, self.state)
                    if ch:
                        self.event("receiver latency: " + "; ".join(ch))
            for rid, sid in self.desired.patches.items():
                if rid not in self.inv.receivers:
                    continue                     # the node is gone (satellite down); nothing to patch yet
                if self._sub(rid) == sid:
                    self.patch_bad_since.pop(rid, None)
                    continue
                since = self.patch_bad_since.setdefault(rid, now)
                if now - since > PATCH_GRACE:
                    code, _ = self.ctl.patch(rid, sid)
                    self.event(f"re-patched receiver {rid[:8]} <- {sid[:8]} -> HTTP {code}")
                    self.patch_bad_since[rid] = now + 30

    # ------------------------------------------------------------------ live controls
    def set_param(self, bid, name, value, persist=True, source="api", quiet=False):
        """persist=False: a fade step (the design file is written on the last step); quiet: no change notification."""
        with self.lock:
            if not self.design:
                raise DeployError("nothing deployed")
            blk = next((b for b in self.design.get("blocks", []) if b["id"] == bid), None)
            if not blk:
                raise DeployError(f"no block {bid} in the deployed design")
            bt = self.types.get(blk["type"])
            if not bt or name not in bt.params:
                raise DeployError(f"block {bid} has no param {name}")
            design = copy.deepcopy(self.design)
            b2 = next(b for b in design["blocks"] if b["id"] == bid)
            b2.setdefault("params", {})[name] = value
            if name in bt.live_params():
                ctx = bt.ctx(bid, blk.get("label"), b2["params"])
                host = self.desired.block_host.get(bid, self.cfg.name) if self.desired else self.cfg.name
                done = 0
                if host != self.cfg.name:
                    c = self.sats.get(host)
                    if not c or not c.ok:
                        raise DeployError(f"satellite {host} is unreachable")
                    conf = bt.render_conf(ctx)
                    ok, r = c.control([list(x) for x in bt.controls(ctx)], {bid: conf})
                    if not ok:
                        raise DeployError(f"satellite {host}: {r}")
                    done = (r or {}).get("controls_set", 0)
                    sh = self.desired.shares.get(host)
                    if sh:
                        sh["procs"][bid] = conf
                        new_ctl = {(n_, c_): v_ for n_, c_, v_ in bt.controls(ctx)}
                        sh["controls"] = [c_ for c_ in sh["controls"] if (c_[0], c_[1]) not in new_ctl] + [[n_, c_, v_] for (n_, c_), v_ in new_ctl.items()]
                        sh["digest"] = share_digest(sh)
                else:
                    g = pw.dump()
                    for node, ctl, val in bt.controls(ctx):
                        n = g.node(node)
                        if n:
                            ok, err = pw.set_control(n["id"], ctl, val)
                            done += ok
                self.design = design
                if persist:
                    atomic_write(DESIGN, design)
                if self.desired:
                    self.desired._design = design
                    if host == self.cfg.name:
                        self.desired.procs[bid] = bt.render_conf(ctx)
                p = self.procs.get(bid)
                if p:
                    # keep the process record at the live values: a later deploy of this design is then a no-op for
                    # the block (2026-11-10: the ducker and volume restarted on a deploy that changed nothing of theirs,
                    # because their records still held the values of the last apply), and a crash restart comes back
                    # with the current values instead of the old ones
                    p.conf = self._full_conf(bt.render_conf(ctx))
                if not quiet:
                    self._notify("param", block=bid, param=name, source=source)
                return {"ok": True, "live": True, "controls_set": done}
            r = self.apply(design, rollback=True, reason=f"param {bid}.{name}")
            self._notify("param", block=bid, param=name, source=source)
            return r

    def update_design(self, panels=None, presets=None):
        """Panels and presets live in the design but change no audio: edit the deployed design (and the draft, so
        the canvas keeps them) in place."""
        with self.lock:
            if not self.design:
                raise DeployError("nothing deployed")
            if panels is not None:
                self.design["panels"] = panels
            if presets is not None:
                self.design["presets"] = presets
            atomic_write(DESIGN, self.design)
            draft = read_json(DRAFT, None)
            if isinstance(draft, dict) and draft.get("wires") is not None:
                if panels is not None:
                    draft["panels"] = panels
                if presets is not None:
                    draft["presets"] = presets
                atomic_write(DRAFT, draft)
            if self.desired:
                self.desired._design = self.design
            self._notify("meta")
            return {"ok": True}

    # ------------------------------------------------------------------ status
    def status(self):
        with self.lock:
            try:
                self.inv.refresh(1.0)
            except Exception:
                pass
            g = self.inv.graph or pw.Graph([])
            have = g.link_set()
            wires = {}
            if self.desired:
                for l in self.desired.links:
                    w = wires.setdefault(l["wire"], {"state": "ok", "detail": []})
                    if not g.node(l["src"]["node"]) or not g.node(l["dst"]["node"]):
                        w["state"] = "pending"
                        w["detail"].append(f"waiting for {l['src']['node'] if not g.node(l['src']['node']) else l['dst']['node']}")
                        continue
                    pairs = self.ops.resolve(g, l)
                    bad = [1 for sp, dp in pairs if (sp["id"], dp["id"]) not in have]
                    if bad or not pairs:
                        w["state"] = "missing"
                        w["detail"].append(f"{len(bad)} of {len(pairs)} links missing")
                pat = {}
                lab = lambda x: (self.inv.receivers.get(x) or self.inv.senders.get(x) or {}).get("label") or (x or "")[:8]
                for rid, sid in self.desired.patches.items():
                    act = self._sub(rid)
                    pat[rid] = {"want": sid, "active": act, "ok": act == sid, "label": lab(rid), "sender_label": lab(sid),
                                "present": rid in self.inv.receivers}
                # a wire whose patch is wrong is not ok; a wire riding a hub receiver shows that receiver's wire stats
                hub_st = ((self.inv.nmos or {}).get("status") or {}).get(self.cfg.node_id) or {}
                for wid, rid in self.desired.wire_patch.items():
                    w = wires.setdefault(wid, {"state": "ok", "detail": []})
                    p = pat.get(rid)
                    if p and not p["ok"]:
                        w["state"] = "missing" if p["present"] else "pending"
                        w["detail"].append(f"patch {p['label']} is {'<- ' + p['sender_label'] if p['active'] else 'not active'}"
                                           if p["present"] else f"{p['label']} is not registered (node down?)")
                for wid, rid in self.desired.wire_rx.items():
                    rs = (hub_st.get("receivers") or {}).get(rid) or {}
                    wr = rs.get("wire") or {}
                    if wr:
                        wires.setdefault(wid, {"state": "ok", "detail": []})["rx"] = {
                            "pkts_per_s": wr.get("pkts_per_s"), "lost": wr.get("lost_total"), "rms_dbfs": wr.get("rms_dbfs")}
                        if wires[wid]["state"] == "ok" and not (wr.get("pkts_per_s") or 0) > 0:
                            wires[wid]["state"] = "quiet"
                            wires[wid]["detail"].append("patched, no packets arriving")
            else:
                pat = {}
            if self.desired:
                for rid, rp in (getattr(self.desired, "raw_patches", None) or {}).items():   # camera -> video receiver
                    w = wires.setdefault(rp["wire"], {"state": "ok", "detail": []})
                    if rid not in self.inv.receivers:
                        w["state"] = "pending"
                        w["detail"].append("the receiver is not registered (its node is down?)")
                    elif not self._raw_ok(rid):
                        w["state"] = "missing"
                        w["detail"].append("the receiver is not playing " + ("the stream" if rp.get("sdp") else "the camera")
                                           + " (re-patched within 15 s)")
                for wid, node in (getattr(self.desired, "voice_wires", None) or {}).items():   # the assistant's listen/reply
                    w = wires.setdefault(wid, {"state": "ok", "detail": []})
                    if not g.node(node):
                        w["state"] = "pending"
                        w["detail"].append(f"waiting for {node}")
                for wid, why in self.desired.pending.items():
                    wires[wid] = {"state": "pending", "detail": [why]}
                for wid, hosts in self.desired.wire_hosts.items():
                    w = wires.setdefault(wid, {"state": "ok", "detail": []})
                    for host in hosts:
                        c = self.sats.get(host)
                        if not c or not c.ok:
                            w["state"] = "pending"
                            w["detail"].append(f"satellite {host} unreachable")
                        elif host in self.share_pending:
                            w["state"] = "pending"
                            w["detail"].append(self.share_pending[host])
                        else:
                            sw = ((c.status or {}).get("wires") or {}).get(wid)
                            if sw and sw.get("state") != "ok":
                                w["state"] = sw["state"] if w["state"] == "ok" else w["state"]
                                w["detail"] += sw.get("detail") or []
                tstats = self.taps.stats()
                for name, t in self.desired.taps.items():          # Meter blocks: the tap runs and is linked
                    wid = next((w for w, impl in self.desired.wire_impl.items() if any(f"meter tap on {t['node']}" == s_ for s_ in impl)), None)
                    if not wid:
                        continue
                    w = wires.setdefault(wid, {"state": "ok", "detail": []})
                    st_ = tstats.get(name) or {}
                    mn = g.node(f"owl-meter-{name}")
                    linked = mn and any(l["in_node"] == f"owl-meter-{name}" for l in g.links)
                    if not st_.get("running") or not mn:
                        w["state"], w["detail"] = "pending", ["meter tap starting"]
                    elif not linked:
                        w["state"], w["detail"] = "missing", ["meter tap not linked"]
            procs = {bid: dict(p.info(), nodes={n: bool(g.node(n)) for n in (self.desired.block_nodes.get(bid, []) if self.desired else [])})
                     for bid, p in self.procs.items()}
            if self.desired:
                for bid, dv in (self.desired.devices or {}).items():          # device blocks: present or not
                    procs[bid] = {"device": True, "running": bool(dv["node"] and (g.node(dv["node"]) if dv["host"] == self.cfg.name else True)),
                                  "node": dv["node"], "host": dv["host"], "match": dv["match"].get("node.name"), "restarts": 0,
                                  "nodes": {dv["node"]: True} if dv["node"] else {}, "setup_ran": self._setup_done.get(bid) is not None}
                for host, sh in self.desired.shares.items():       # satellite blocks: what their executor reports
                    c = self.sats.get(host)
                    sp = ((c.status or {}).get("procs") or {}) if c and c.ok else {}
                    for bid in sh.get("procs") or {}:
                        procs[bid] = dict(sp.get(bid) or {"running": False, "restarts": 0, "nodes": {}}, host=host,
                                          reachable=bool(c and c.ok))
            taps = self.tap_stats()
            tapmap = {}
            if self.desired:
                for name, t in self._auto_taps(self.desired).items():
                    tapmap[name] = dict(t, stats=taps.get(name))
                for host, sh in self.desired.shares.items():
                    for name, t in (sh.get("taps") or {}).items():
                        tapmap[name] = dict(t, stats=taps.get(name), host=host)
            owned = {(r["out_node"], r["out_port"], r["in_node"], r["in_port"]) for r in self.state.get("links", [])}
            external = []
            for l in g.links:
                k = (l["out_node"], l["out_port"], l["in_node"], l["in_port"])
                if k in owned or not l["out_node"] or not l["in_node"]:
                    continue
                if l["in_node"].startswith(("owl-meter-", "owl-probe-")):
                    continue
                external.append({"out_node": l["out_node"], "out_port": l["out_port"], "in_node": l["in_node"], "in_port": l["in_port"]})
            return {"time": time.time(), "deployed": bool(self.design), "name": (self.design or {}).get("name"),
                    "deployed_at": self.state.get("deployed_at"), "has_prev": os.path.exists(PREV),
                    "prev_name": (read_json(PREV, {}) or {}).get("name"), "wires": wires, "patches": pat, "procs": procs,
                    "taps": tapmap, "external_links": external, "repairs": self.repairs, "events": list(self.events),
                    "pw_error": self.last_graph_error, "inventory_error": self.inv.error,
                    "stream_watcher": {"running": self.watcher.running(), "watching": sorted(self.watcher.want), "moves_made": self.watcher.hits},
                    "satellites": {h: dict(c.public(), share_pending=self.share_pending.get(h),
                                           in_design=bool(self.desired and h in self.desired.shares)) for h, c in self.sats.items()},
                    "design_error": self.design_error if (self.design and not self.desired) else None}

    def baseline_design(self):
        """When nothing is deployed: the running patches as wires, so the canvas starts from what is."""
        inv = self.inv.refresh()
        ports = {}
        for b in inv.blocks:
            for p in b["inputs"] + b["outputs"]:
                if "nmos" in p:
                    ports[p["nmos"].get("receiver") or p["nmos"].get("sender")] = f"{b['id']}|{p['id']}"
        wires, adopted = [], []
        for rid, r in inv.receivers.items():
            sub = r.get("subscription") or {}
            if sub.get("active") and sub.get("sender_id") in ports and rid in ports:
                wires.append({"id": f"w-{rid[:8]}", "from": ports[sub["sender_id"]], "to": ports[rid]})
                adopted.append(rid)
        return {"version": 1, "name": "Living room", "blocks": [], "wires": wires, "layout": {},
                "meta": {"adopted": adopted, "baseline": True}}

    def stop(self):
        with self.lock:
            self.watcher.stop()
            for p in self.procs.values():
                p.stop()
            self.taps.stop()
