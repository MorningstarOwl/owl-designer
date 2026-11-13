"""The `dsp` role's HTTP API (the controller proxies /api/dsp/* to it and serves the designer page).

  GET  /api/dsp/inventory   device blocks (hub PipeWire + every NMOS node) with live status
  GET  /api/dsp/palette     block types from dsp/blocks/*.yaml
  GET  /api/dsp/design      the deployed design (or, when none, a baseline built from the running patches)
  GET  /api/dsp/draft       PUT /api/dsp/draft   the canvas's work in progress (shared between browsers)
  POST /api/dsp/plan        {design} -> validation + diff against what runs (nothing changes)
  POST /api/dsp/deploy      {design, keep_prev?, allow_missing?} -> apply with verification; a failure rolls back to the
                            previous design. allow_missing: absent devices (an app stream that only exists while it
                            plays) leave their wires pending instead of failing; the reconcile wires them when they appear
  POST /api/dsp/rollback    re-apply the previous design
  POST /api/dsp/clear       tear down everything the deployer owns
  POST /api/dsp/control     {block, param, value}: live control (filter-chain) or a restart of that block (D1 form)
  GET  /api/dsp/status      wires ok/pending/missing, processes, patches, meters, events, mqtt, websocket clients
D2, named controls:
  GET  /api/dsp/controls            every control (key, name, type, range, value), meters with values, presets, panels
  GET  /api/dsp/controls/<ref>      one control by key, name or alias (meters include their level)
  POST /api/dsp/controls/set        {key|name, value, fade_s?}   PUT /api/dsp/controls/<ref> {value, fade_s?}
  GET  /api/dsp/ws                  WebSocket: hello + every change + meters at 4 Hz; set/recall/get messages (ws.py)
  GET  /api/dsp/probe?block=&port=  WebSocket: live PCM of that point of the hub's graph for the designer's probe (probe.py)
  GET/PUT /api/dsp/panels           the deployed design's panels (pages of controls); PUT changes no audio
  GET  /api/dsp/presets             POST /api/dsp/presets {name, keys|values, fade_s, id?}   DELETE /api/dsp/presets/<id>
  POST /api/dsp/presets/recall      {preset, fade_s?}
  GET  /api/dsp/mqtt                the HA bridge's state (broker, entities, errors; never the password)
  GET  /api/dsp/previews            which MediaMTX preview paths are live (for the HA add-on's designer)
  GET  /api/dsp/sap                 AES67 streams announced over SAP (Dante, ...), with their SDPs (OWL_SAP=off: none)
  POST /api/dsp/inject              {block, port, kind, level_db, freq_hz, duration_s}: a test signal into an input
                                    (inject.py; on a satellite's port, that satellite plays it); POST .../<id> {level_db};
                                    DELETE .../<id>; GET /api/dsp/inject: what plays now
  GET  /api/dsp/sounds              the sound library (sounds.py); POST /api/dsp/sounds?name=<file> {the file} adds one;
                                    GET .../<name>/file; DELETE .../<name>. A Sound player block plays one on <block>.play
  GET  /api/dsp/health              what needs attention (PTP, packet loss, restarts, nodes; health.py)
  GET  /metrics                     Prometheus metrics of the whole system (health.py)
  GET  /api/dsp/history             the last deployed designs; GET /api/dsp/history/<id> one of them
  GET  /api/dsp/backup              this hub's backup bundle (backup.py); it is also pushed to every satellite
  POST /api/dsp/restore             {backup}: restore a bundle on this hub (re-pairs the nodes, re-applies the design)
D3, satellites (a node without the hub role runs this server in satellite mode: the PipeWire executor of its share,
see satellite.py; the hub's dsp finds it on :8362 through the registry):
  GET  /api/dsp/graph               this host's PipeWire graph for the hub's inventory
  GET  /api/dsp/share               PUT /api/dsp/share {share}      POST /api/dsp/control {set, procs}   POST /api/dsp/clear
  GET  /api/dsp/status              {mode: satellite, share, procs, wires, taps, events}
  GET  /api/dsp/hub-backup          PUT /api/dsp/hub-backup {bundle}: the latest backup of the hub (for a replacement hub)
"""
import json, os, signal, sys, threading, time, urllib.error
from urllib.parse import parse_qs
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

from .. import VERSION, config
from ..config import atomic_write, read_json
from ..controller import Controller
from . import blocks as B
from .controls import Controls
from .deploy import DESIGN, DRAFT, DeployError, Deployer
from .inventory import Inventory
from .meters import Taps
from .mqtt import Bridge
from .satellite import ShareDeployer, graph_objects
from .voice import Voice
from .ws import Session
from .nodes import NodeBook, NodeError
from . import probe as P
from . import sap as SAP
from . import backup as BK
from . import health as HL
from . import inject as INJ
from . import sounds as SND
from .satellite import http as sat_http


def log(*a):
    print(time.strftime("%H:%M:%S"), "dsp:", *a, flush=True)


class SatHandler(BaseHTTPRequestHandler):
    """Satellite mode: the executor's API (no designer, no controls, no MQTT)."""
    sd = None
    injector = INJ.Injector(log=lambda *a: log(*a))
    server_version = "owl-node-dsp/" + VERSION
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        p = urlsplit(self.path).path
        try:
            if p == "/healthz":
                return self._send(200, {"ok": True, "version": VERSION, "mode": "satellite"})
            if p == "/api/dsp/status":
                return self._send(200, dict(self.sd.status(), **self._identity()))
            if p == "/api/dsp/graph":
                return self._send(200, {"host": self.sd.cfg.name, "time": time.time(), "objects": graph_objects()})
            if p == "/api/dsp/share":
                return self._send(200, {"share": self.sd.share, "applied_at": self.sd.applied_at, "error": self.sd.error})
            if p == "/api/dsp/palette":
                return self._send(200, {"types": sorted(B.load())})
            if p == "/api/dsp/inject":
                return self._send(200, {"injections": self.injector.active()})
            if p == "/api/dsp/hub-backup":
                b = BK.stored()
                return self._send(200, b) if b else self._send(404, {"error": "no hub backup stored here yet"})
        except Exception as e:
            return self._send(500, {"error": f"{type(e).__name__}: {e}"})
        self._send(404, {"error": "not found (satellite dsp role)"})

    def _identity(self):
        """Who this node is and which hub it is paired with (E1: a hub adds nodes by address)."""
        c = self.sd.cfg
        return {"label": getattr(c, "label", None) or c.name, "area": getattr(c, "area", None), "roles": list(c.roles),
                "paired": read_json(config.HUB_FILE, None), "registries": list(c.registries)}

    def do_DELETE(self):
        p = urlsplit(self.path).path
        if p.startswith("/api/dsp/inject/"):
            try:
                return self._send(200, self.injector.stop(p[len("/api/dsp/inject/"):]))
            except INJ.InjectError as e:
                return self._send(404, {"error": str(e)})
        if p == "/api/dsp/pair":
            try:
                os.unlink(config.HUB_FILE)
            except FileNotFoundError:
                pass
            log("unpaired: back to node.yaml's registry / mDNS")
            return self._send(200, {"ok": True, "host": self.sd.cfg.name})
        self._send(404, {"error": "not found"})

    def do_PUT(self):
        p = urlsplit(self.path).path
        try:
            b = self._body()
            if p == "/api/dsp/pair":
                reg = str(b.get("registry") or "").rstrip("/")
                if not reg.startswith(("http://", "https://")):
                    raise ValueError("registry must be the hub's registry URL (http://<hub>:8010)")
                rec = {"registry": reg, "hub": b.get("hub"), "hub_name": b.get("hub_name"), "paired_at": time.time()}
                atomic_write(config.HUB_FILE, rec)
                log(f"paired with hub {rec['hub_name'] or rec['hub']} (registry {reg})")
                return self._send(200, dict({"ok": True, "host": self.sd.cfg.name, "version": VERSION}, **self._identity()))
            if p == "/api/dsp/share":
                return self._send(200, self.sd.apply(b.get("share") if "share" in b else b))
            if p == "/api/dsp/hub-backup":
                return self._send(200, BK.store(b))
        except (KeyError, ValueError, TypeError) as e:
            return self._send(400, {"ok": False, "error": f"bad request: {e}"})
        except Exception as e:
            return self._send(409, {"ok": False, "error": f"{type(e).__name__}: {e}"})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        p = urlsplit(self.path).path
        try:
            b = self._body()
            if p == "/api/dsp/control":
                return self._send(200, self.sd.control(b.get("set") or [], b.get("procs") or {}))
            if p == "/api/dsp/inject":                  # the hub resolved the canvas port to this node's PipeWire node
                return self._send(200, self.injector.start(b["node"], b.get("ports"), b.get("label"), b.get("kind") or "white",
                                                           b.get("level_db", INJ.DEFAULT_DB), b.get("freq_hz") or 1000, b.get("duration_s") or 30))
            if p.startswith("/api/dsp/inject/"):
                return self._send(200, self.injector.set_level(p[len("/api/dsp/inject/"):], b["level_db"]))
            if p == "/api/dsp/clear":
                return self._send(200, self.sd.clear())
        except INJ.InjectError as e:
            return self._send(400, {"ok": False, "error": str(e)})
        except Exception as e:
            return self._send(409, {"ok": False, "error": f"{type(e).__name__}: {e}"})
        self._send(404, {"error": "not found"})


class Handler(BaseHTTPRequestHandler):
    book = None
    registry = None
    dep = None
    inv = None
    types = None
    ctl = None
    mqtt = None
    sap = None
    injector = INJ.Injector(log=lambda *a: log(*a))
    sounds = None                 # sounds.Library (hub)
    player = None                 # sounds.Player (hub)
    inject_remote = {}            # hub id "<host>:<id>" -> (host, the satellite's id)
    ws_clients = 0
    server_version = "owl-node-dsp/" + VERSION
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        p = urlsplit(self.path).path
        try:
            if p == "/healthz":
                return self._send(200, {"ok": True, "version": VERSION})
            if p == "/api/dsp/ws":
                return self._ws()
            if p == "/api/dsp/probe":
                if not P.ProbeSession(self, self.dep, log).run():
                    return self._send(400, {"error": "expected a WebSocket upgrade"})
                return
            if p == "/api/dsp/inventory":
                return self._send(200, self.inv.refresh().public())
            if p == "/api/dsp/palette":
                return self._send(200, {"types": [t.palette() for t in self.types.values()]})
            if p == "/api/dsp/design":
                if self.dep.design:
                    return self._send(200, {"deployed": True, "design": self.dep.design})
                return self._send(200, {"deployed": False, "design": self.dep.baseline_design()})
            if p == "/api/dsp/draft":
                return self._send(200, {"draft": read_json(DRAFT, None)})
            if p == "/api/dsp/status":
                st = self.dep.status()
                st["mqtt"] = self.mqtt.status() if self.mqtt else {"enabled": False}
                st["voice"] = self.ctl.voice.status() if self.ctl.voice else None
                st["probes"] = P.active()
                st["sap"] = self.sap.status() if self.sap else {"enabled": False}
                st["backup"] = {"pushed": BACKUP_PUSHED}
                st["ws_clients"] = Handler.ws_clients
                st["controls_version"] = self.ctl.version
                return self._send(200, st)
            if p == "/api/dsp/controls":
                return self._send(200, self.ctl.listing())
            if p.startswith("/api/dsp/controls/"):
                try:
                    return self._send(200, self.ctl.get(unquote(p[len("/api/dsp/controls/"):])))
                except DeployError as e:
                    return self._send(404, {"error": str(e)})
            if p == "/api/dsp/panels":
                return self._send(200, {"panels": (self.dep.design or {}).get("panels") or [], "deployed": bool(self.dep.design)})
            if p == "/api/dsp/presets":
                return self._send(200, {"presets": (self.dep.design or {}).get("presets") or [], "last": self.ctl.last_preset})
            if p == "/api/dsp/mqtt":
                return self._send(200, self.mqtt.status() if self.mqtt else {"enabled": False})
            if p == "/api/dsp/nodes":
                return self._send(200, self._nodes())
            if p == "/api/dsp/history":
                return self._send(200, {"history": self.dep.history()})
            if p.startswith("/api/dsp/history/"):
                try:
                    return self._send(200, self.dep.history_item(unquote(p[len("/api/dsp/history/"):])))
                except DeployError as e:
                    return self._send(404, {"error": str(e)})
            if p == "/api/dsp/inject":
                return self._send(200, {"injections": self._injections()})
            if p == "/api/dsp/sounds":
                return self._send(200, {"sounds": self.sounds.list(), "playing": self.player.status(),
                                        "limits": {"file_mb": SND.MAX_FILE // 2**20, "total_mb": SND.MAX_TOTAL // 2**20}})
            if p.startswith("/api/dsp/sounds/") and p.endswith("/file"):
                return self._sound_file(unquote(p[len("/api/dsp/sounds/"):-len("/file")]))
            if p == "/api/dsp/health":
                return self._send(200, self.ctl.health.check() if self.ctl.health else {"ok": True, "issues": []})
            if p == "/metrics":
                data = HL.metrics(self.dep, self.inv, self.dep.taps, self.ctl.health, VERSION).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                return self.wfile.write(data)
            if p == "/api/dsp/backup":
                return self._send(200, BK.make(self.dep.cfg.name))
            if p == "/api/dsp/sap":
                if not self.sap:
                    return self._send(200, {"enabled": False, "streams": []})
                return self._send(200, dict(self.sap.status(), enabled=True, streams=self.sap.dir.list()))
            if p == "/api/dsp/previews":
                # MediaMTX's API is on the hub's loopback: the remote designer (HA add-on) reads preview state here
                return self._send(200, {"previews": self.dep.ctl.previews()})
            if p == "/api/dsp/voice":
                return self._send(200, self.ctl.voice.status() if self.ctl.voice else {"enabled": False})
        except Exception as e:
            return self._send(500, {"error": f"{type(e).__name__}: {e}"})
        self._send(404, {"error": "not found"})

    def _ws(self):
        Handler.ws_clients += 1
        try:
            s = Session(self, self.ctl, log)
            if not s.run():
                self._send(400, {"error": "expected a WebSocket upgrade"})
        finally:
            Handler.ws_clients -= 1

    def do_PUT(self):
        p = urlsplit(self.path).path
        try:
            b = self._body()
            if p == "/api/dsp/draft":
                atomic_write(DRAFT, b.get("draft") if "draft" in b else b)
                return self._send(200, {"ok": True})
            if p == "/api/dsp/panels":
                return self._send(200, self.ctl.set_panels(b.get("panels") if "panels" in b else b))
            if p.startswith("/api/dsp/controls/"):
                return self._send(200, self.ctl.set(unquote(p[len("/api/dsp/controls/"):]), b.get("value"), fade_s=float(b.get("fade_s") or 0),
                                                    source=b.get("source") or "api"))
        except DeployError as e:
            return self._send(409, {"ok": False, "error": str(e)})
        except (KeyError, ValueError, TypeError) as e:
            return self._send(400, {"error": f"bad request: {e}"})
        except Exception as e:
            return self._send(500, {"error": f"{type(e).__name__}: {e}"})
        self._send(404, {"error": "not found"})

    def _registry_url(self):
        """The hub's registry as a node on the LAN reaches it (a loopback URL is rewritten to the address the browser
        used for this hub)."""
        reg = (Handler.registry or "").rstrip("/")
        u = urlsplit(reg)
        if u.hostname in ("127.0.0.1", "localhost", "::1", None):
            host = self._public_host().rsplit(":", 1)[0] or self.dep.cfg.ip
            reg = f"http://{host}:{u.port or 8010}"
        return reg

    def _public_host(self):
        """host[:port] the browser used for the designer (the controller forwards it; a direct call has Host)."""
        return self.headers.get("X-Forwarded-Host") or self.headers.get("Host") or ""

    def _hub_url(self):
        h = self._public_host()
        if not h or h.startswith(("127.", "localhost")):
            h = f"{self.dep.cfg.ip}:8361"
        return f"http://{h}"

    def _nodes(self):
        """Every node the designer knows: added by address and/or found in the registry, with what the hub sees."""
        inv = self.inv
        added = {e["host"]: e for e in self.book.entries()}
        nst = (inv.nmos or {}).get("status") or {}
        registered = {(nst.get(n["id"]) or {}).get("name") for n in (inv.nmos or {}).get("nodes") or []}
        design_hosts = {}
        for b in (self.dep.design or {}).get("blocks") or []:
            if b.get("host"):
                design_hosts[b["host"]] = design_hosts.get(b["host"], 0) + 1
        out = []
        for host in sorted(set(added) | set(inv._probes)):
            c = inv._probes.get(host)
            st = (c.status if c else None) or {}
            pw_n = sum(1 for b in inv.blocks if b.get("host") == host and b.get("source") == "pipewire")
            out.append({"host": host, "label": st.get("label") or (added.get(host) or {}).get("label") or host,
                        "url": (c.url if c else (added.get(host) or {}).get("url")), "added": host in added, "from_registry": host in registered,
                        "ok": bool(c and c.ok), "seen": bool(c and c.seen), "error": c.error if c else None,
                        "version": c.version if c else None, "paired": st.get("paired"), "area": st.get("area"),
                        "devices": pw_n, "design_blocks": design_hosts.get(host, 0),
                        "transport": {k: len(v) for k, v in (inv.node_transport.get(host) or {}).items()}})
        return {"hub": self.dep.cfg.name, "registry": self._registry_url(), "nodes": out, "removed": sorted(self.book.removed)}

    # ---- the sound library (sounds.py)
    def _sounds_changed(self, what):
        log(f"sounds: {what}")
        self.ctl.refresh(force=True)                     # a Sound player's select lists the library
        self.ctl.bus.publish({"type": "design", "name": (self.dep.design or {}).get("name"), "kind": "sounds",
                              "version": self.ctl.version})

    def _sound_upload(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > SND.MAX_FILE:
            self.close_connection = True                 # the body is not read
            return self._send(413, {"ok": False, "error": f"at most {SND.MAX_FILE // 2**20} MB per sound"})
        data = self.rfile.read(n) if n else b""
        name = (parse_qs(urlsplit(self.path).query).get("name") or ["sound"])[0]
        try:
            r = self.sounds.add(name, data)
        except SND.SoundError as e:
            return self._send(400, {"ok": False, "error": str(e)})
        self._sounds_changed(f"added {r['name']} ({r['size']} bytes, {r['format']})")
        return self._send(200, dict(r, ok=True))

    def _sound_file(self, name):
        try:
            path = self.sounds.path(name)
            fmt = self.sounds.info(name)["format"]
        except SND.SoundError as e:
            return self._send(404, {"error": str(e)})
        with open(path, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", {"wav": "audio/wav", "flac": "audio/flac", "ogg": "audio/ogg", "mp3": "audio/mpeg"}.get(fmt, "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    # ---- the injector (inject.py): canvas port -> this hub's PipeWire, or the satellite that has the port
    def _injections(self):
        out = [dict(i, host=self.dep.cfg.name) for i in self.injector.active()]
        for host in sorted({h for h, _ in Handler.inject_remote.values()}):
            c = self.inv.sats.get(host)
            try:
                code, r = sat_http("GET", f"{c.url}/api/dsp/inject", timeout=2) if c else (0, None)
                live = {i["id"]: i for i in (r or {}).get("injections") or []} if code == 200 else {}
            except Exception:
                live = {}
            for hid, (h, rid) in list(Handler.inject_remote.items()):
                if h == host:
                    if rid in live:
                        out.append(dict(live[rid], id=hid, host=host))
                    else:
                        Handler.inject_remote.pop(hid, None)           # ended there
        return out

    def _inject_start(self, b):
        try:
            t = INJ.resolve_input(self.dep, b.get("block"), b.get("port"))
            args = dict(kind=b.get("kind") or "white", level_db=b.get("level_db", INJ.DEFAULT_DB),
                        freq_hz=b.get("freq_hz") or 1000, duration_s=b.get("duration_s") or 30)
            if t["host"] == self.dep.cfg.name:
                return self._send(200, dict(self.injector.start(t["node"], t["ports"], t["label"], **args), host=t["host"]))
            c = self.inv.sats.get(t["host"])
            if not c or not c.ok:
                raise INJ.InjectError(f"{t['label']} is on {t['host']}, which does not answer now")
            try:
                code, r = sat_http("POST", f"{c.url}/api/dsp/inject", dict(args, node=t["node"], ports=t["ports"], label=t["label"]), timeout=10)
            except urllib.error.HTTPError as e:
                raise INJ.InjectError(f"{t['host']}: {json.loads(e.read() or b'{}').get('error') or e}")
            except Exception as e:
                raise INJ.InjectError(f"{t['host']}: {e} (owl-node there older than 0.11?)")
            hid = f"{t['host']}:{r['id']}"
            Handler.inject_remote[hid] = (t["host"], r["id"])
            return self._send(200, dict(r, id=hid, host=t["host"]))
        except INJ.InjectError as e:
            return self._send(400, {"ok": False, "error": str(e)})

    def _inject_remote_call(self, hid, method, body=None):
        host, rid = Handler.inject_remote[hid]
        c = self.inv.sats.get(host)
        try:
            code, r = sat_http(method, f"{c.url}/api/dsp/inject/{rid}", body, timeout=5)
        except urllib.error.HTTPError as e:
            raise INJ.InjectError(json.loads(e.read() or b"{}").get("error") or str(e))
        except Exception as e:
            raise INJ.InjectError(f"{host}: {e}")
        if method == "DELETE":
            Handler.inject_remote.pop(hid, None)
        return dict(r, id=hid, host=host)

    def _inject_level(self, iid, level_db):
        try:
            if level_db is None:
                raise INJ.InjectError("give level_db")
            if iid in Handler.inject_remote:
                return self._send(200, self._inject_remote_call(iid, "POST", {"level_db": level_db}))
            return self._send(200, self.injector.set_level(iid, level_db))
        except INJ.InjectError as e:
            return self._send(400, {"ok": False, "error": str(e)})

    def _inject_stop(self, iid):
        try:
            if iid in Handler.inject_remote:
                return self._send(200, self._inject_remote_call(iid, "DELETE"))
            return self._send(200, self.injector.stop(iid))
        except INJ.InjectError as e:
            return self._send(404, {"ok": False, "error": str(e)})

    def _restore(self, bundle):
        """A hub backup -> this hub: files, nodes re-paired with this hub's registry, design re-applied."""
        hub = self.dep.cfg.name
        summary = BK.restore(bundle, hub)
        book = NodeBook()
        Handler.book = self.inv.book = book
        me = {self.dep.cfg.ip, self._public_host().rsplit(":", 1)[0], "127.0.0.1", "localhost", hub}
        paired = {}
        for e in book.entries():
            try:
                r = book.add(e["url"], hub, self._registry_url(), self._hub_url(), me)
                paired[e["host"]] = "paired" if r.get("paired") else f"added, not paired: {r.get('pair_error')}"
            except NodeError as err:
                paired[e["host"]] = f"not reachable now: {err}"
        summary["nodes"] = paired
        design = read_json(DESIGN, None)
        if design:
            self.inv.time = 0
            try:
                self.inv.refresh(0)
                r = self.dep.apply(design, rollback=False, reason="restore", strict=False)
                summary["deploy"] = {"ok": True, "pending": sorted((r or {}).get("pending") or [])}
            except DeployError as err:
                summary["deploy"] = {"ok": False, "error": str(err)}
        log(f"restored the backup of hub {summary['from_hub']!r}: {', '.join(summary['restored'])}; nodes {paired}")
        return dict(summary, ok=True)

    def do_DELETE(self):
        p = urlsplit(self.path).path
        try:
            if p.startswith("/api/dsp/nodes/"):
                host = unquote(p[len("/api/dsp/nodes/"):])
                r = self.book.remove(host)
                self.inv.drop_satellite(host)
                n = sum(1 for b in (self.dep.design or {}).get("blocks") or [] if b.get("host") == host)
                if n:
                    r["warning"] = f"the deployed design still places {n} block(s) on {host}; they stay pending until it is added again"
                log(f"node {host} removed from the designer")
                return self._send(200, r)
            if p.startswith("/api/dsp/presets/"):
                return self._send(200, self.ctl.delete_preset(unquote(p[len("/api/dsp/presets/"):])))
            if p.startswith("/api/dsp/inject/"):
                return self._inject_stop(p[len("/api/dsp/inject/"):])
            if p.startswith("/api/dsp/sounds/"):
                try:
                    name = unquote(p[len("/api/dsp/sounds/"):])
                    self.player.stop_sound(name)
                    r = self.sounds.delete(name)
                except SND.SoundError as e:
                    return self._send(404, {"ok": False, "error": str(e)})
                self._sounds_changed(f"deleted {name}")
                return self._send(200, r)
        except DeployError as e:
            return self._send(409, {"ok": False, "error": str(e)})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        p = urlsplit(self.path).path
        if p == "/api/dsp/sounds":
            return self._sound_upload()
        try:
            b = self._body()
            if p == "/api/dsp/nodes":
                try:
                    me = {self.dep.cfg.ip, self._public_host().rsplit(":", 1)[0], "127.0.0.1", "localhost", self.dep.cfg.name}
                    r = self.book.add(b.get("address") or b.get("url"), self.dep.cfg.name, self._registry_url(), self._hub_url(), me)
                except NodeError as e:
                    return self._send(400, {"ok": False, "error": str(e)})
                self.inv.time = 0
                log(f"node {r['host']} added by address ({r['url']}, owl-node {r.get('version')}, paired: {r['paired']})")
                return self._send(200, r)
            if p == "/api/dsp/inject":
                return self._inject_start(b)
            if p.startswith("/api/dsp/inject/"):
                return self._inject_level(p[len("/api/dsp/inject/"):], b.get("level_db"))
            if p == "/api/dsp/restore":
                return self._send(200, self._restore(b.get("backup") if "backup" in b else b))
            if p == "/api/dsp/plan":
                design = b.get("design") or b
                r = self.dep.plan(design, strict=not b.get("allow_missing"))
                v = self.ctl.validate(design)
                r["warnings"] = (r.get("warnings") or []) + v["warnings"]
                r["renames"] = v["renames"]
                return self._send(200, r)
            if p == "/api/dsp/deploy":
                try:
                    return self._send(200, self.dep.apply(b.get("design") or b, keep_prev=bool(b.get("keep_prev")), strict=not b.get("allow_missing")))
                except DeployError as e:
                    return self._send(409, {"ok": False, "error": str(e), "rolled_back": self.dep.design is not None})
            if p == "/api/dsp/rollback":
                try:
                    return self._send(200, self.dep.rollback())
                except DeployError as e:
                    return self._send(409, {"ok": False, "error": str(e)})
            if p == "/api/dsp/clear":
                return self._send(200, self.dep.remove_all())
            if p == "/api/dsp/control":
                try:
                    return self._send(200, self.dep.set_param(b["block"], b["param"], b["value"], source=b.get("source") or "api"))
                except DeployError as e:
                    return self._send(409, {"ok": False, "error": str(e)})
            if p == "/api/dsp/controls/set":
                try:
                    return self._send(200, self.ctl.set(b.get("key") or b.get("name"), b.get("value"), fade_s=float(b.get("fade_s") or 0),
                                                        source=b.get("source") or "api"))
                except DeployError as e:
                    return self._send(409, {"ok": False, "error": str(e)})
            if p == "/api/dsp/presets":
                try:
                    return self._send(200, self.ctl.save_preset(b.get("name"), b.get("keys"), b.get("fade_s") or 0, b.get("id"), b.get("values")))
                except DeployError as e:
                    return self._send(409, {"ok": False, "error": str(e)})
            if p == "/api/dsp/presets/recall":
                try:
                    return self._send(200, self.ctl.recall(b.get("preset") or b.get("name") or b.get("id"), fade_s=b.get("fade_s"), source=b.get("source") or "api"))
                except DeployError as e:
                    return self._send(409, {"ok": False, "error": str(e)})
        except (KeyError, ValueError, TypeError) as e:
            return self._send(400, {"error": f"bad request: {e}"})
        except Exception as e:
            return self._send(500, {"error": f"{type(e).__name__}: {e}"})
        self._send(404, {"error": "not found"})


def main():
    cfg = config.Node()
    mode = os.environ.get("OWL_DSP_MODE") or ("hub" if "hub" in cfg.roles else "satellite")
    if mode == "satellite":
        return satellite_main(cfg)
    reg = os.environ.get("OWL_REGISTRY") or (cfg.registries or [None])[0]
    if not reg:
        sys.exit("dsp needs registry: in node.yaml (the hub's own) or OWL_REGISTRY")
    port = int(os.environ.get("OWL_DSP_PORT") or (cfg.raw.get("dsp") or {}).get("port", 8362))
    types = B.load()
    ctl = Controller(reg)
    inv = Inventory(cfg, ctl)
    inv.book = NodeBook()
    sap = None
    if os.environ.get("OWL_SAP", "on") not in ("off", "0", "no"):
        sap = SAP.Listener(cfg.ip, log=log)            # third-party AES67 streams (Dante, ...) announced over SAP
        inv.sap = sap.dir
    taps = Taps()
    dep = Deployer(cfg, inv, types, taps, ctl)
    controls = Controls(dep, types, inv, cfg)
    controls.health = HL.Health(dep, inv)
    library = SND.Library()

    def sound_changed(bid, playing, source):
        controls.bus.publish({"type": "sound", "block": bid, "playing": playing, "source": source})
    player = SND.Player(dep, library, log=log, on_change=sound_changed)
    controls.sounds, controls.player = library, player
    Handler.sounds, Handler.player = library, player
    mqtt = Bridge(controls, cfg, controller_port=int(os.environ.get("OWL_CONTROLLER_PORT", "8361")))
    voice = None
    if os.environ.get("OWL_VOICE", "on") not in ("off", "0", "no"):
        voice = Voice(cfg, dep, controls.bus)
        controls.voice = voice
        controls.refresh(force=True)
    Handler.dep, Handler.inv, Handler.types, Handler.ctl, Handler.mqtt = dep, inv, types, controls, mqtt
    Handler.book, Handler.registry = inv.book, reg
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    srv.daemon_threads = True
    stopping = []

    def stop(*_):
        stopping.append(1)
        srv.shutdown()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    log(f"owl-node {VERSION} dsp on :{port}, hub {cfg.name}, registry {reg}, {len(types)} block types "
        f"({', '.join(sorted(types))}), blocks dir {next((d for d in B.BLOCK_DIRS if os.path.isdir(d)), '?')}, "
        f"mqtt {'-> ' + mqtt.status()['broker'] if mqtt.conf else 'off (no OWL_MQTT_URL / mqtt.json)'}")
    mqtt.start()
    if voice:
        voice.start()
    if sap:
        sap.start()
    Handler.sap = sap

    def loop():
        while not stopping:
            try:
                dep.reconcile()
            except Exception as e:
                log(f"reconcile: {type(e).__name__}: {e}")
            time.sleep(2)
    threading.Thread(target=loop, daemon=True).start()
    threading.Thread(target=push_backups, args=(cfg, inv, stopping), daemon=True).start()
    try:
        srv.serve_forever()
    finally:
        Handler.injector.stop_all()
        player.stop_all()
        if voice:
            voice.stop()
        mqtt.stop()
        controls.stop()
        dep.stop()
        log("stopped")


BACKUP_PUSHED = {}       # satellite host -> {digest, at, error}: GET /api/dsp/status shows it


def push_backups(cfg, inv, stopping, every=30):
    """Keep the hub's backup on every satellite dsp role it talks to (sent when it changed or the satellite lacks it)."""
    while not stopping:
        try:
            bundle = BK.make(cfg.name)
            dig = BK.digest(bundle)
            for host, c in list(inv.sats.items()):
                if not c.ok or (BACKUP_PUSHED.get(host) or {}).get("digest") == dig:
                    continue
                try:
                    code, r = sat_http("PUT", f"{c.url}/api/dsp/hub-backup", bundle, timeout=5)
                    BACKUP_PUSHED[host] = {"digest": dig if code == 200 else None, "at": time.time(),
                                           "error": None if code == 200 else f"HTTP {code} (owl-node there older than 0.8?)"}
                except Exception as e:
                    BACKUP_PUSHED[host] = {"digest": None, "at": time.time(), "error": f"{type(e).__name__}: {e}"}
        except Exception as e:
            log(f"backup push: {type(e).__name__}: {e}")
        for _ in range(every):
            if stopping:
                return
            time.sleep(1)


def satellite_main(cfg):
    port = int(os.environ.get("OWL_DSP_PORT") or (cfg.raw.get("dsp") or {}).get("port", 8362))
    taps = Taps()
    sd = ShareDeployer(cfg, taps)
    SatHandler.sd = sd
    srv = ThreadingHTTPServer(("0.0.0.0", port), SatHandler)
    srv.daemon_threads = True
    stopping = []

    def stop(*_):
        stopping.append(1)
        srv.shutdown()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    log(f"owl-node {VERSION} dsp (satellite mode) on :{port}, node {cfg.name}: share {sd.share.get('digest')} "
        f"of {sd.share.get('design')!r} ({len(sd.share.get('procs') or {})} block process(es), {len(sd.share.get('links') or [])} link(s))")

    def loop():
        while not stopping:
            try:
                sd.reconcile()
            except Exception as e:
                log(f"reconcile: {type(e).__name__}: {e}")
            time.sleep(2)
    threading.Thread(target=loop, daemon=True).start()
    try:
        srv.serve_forever()
    finally:
        sd.stop()
        log("stopped")
