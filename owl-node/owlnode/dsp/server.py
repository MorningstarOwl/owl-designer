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
D3, satellites (a node without the hub role runs this server in satellite mode: the PipeWire executor of its share,
see satellite.py; the hub's dsp finds it on :8362 through the registry):
  GET  /api/dsp/graph               this host's PipeWire graph for the hub's inventory
  GET  /api/dsp/share               PUT /api/dsp/share {share}      POST /api/dsp/control {set, procs}   POST /api/dsp/clear
  GET  /api/dsp/status              {mode: satellite, share, procs, wires, taps, events}
"""
import json, os, signal, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

from .. import VERSION, config
from ..config import atomic_write, read_json
from ..controller import Controller
from . import blocks as B
from .controls import Controls
from .deploy import DRAFT, DeployError, Deployer
from .inventory import Inventory
from .meters import Taps
from .mqtt import Bridge
from .satellite import ShareDeployer, graph_objects
from .voice import Voice
from .ws import Session
from .nodes import NodeBook, NodeError
from . import probe as P
from . import sap as SAP


def log(*a):
    print(time.strftime("%H:%M:%S"), "dsp:", *a, flush=True)


class SatHandler(BaseHTTPRequestHandler):
    """Satellite mode: the executor's API (no designer, no controls, no MQTT)."""
    sd = None
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
            if p == "/api/dsp/clear":
                return self._send(200, self.sd.clear())
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
        except DeployError as e:
            return self._send(409, {"ok": False, "error": str(e)})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        p = urlsplit(self.path).path
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
    try:
        srv.serve_forever()
    finally:
        if voice:
            voice.stop()
        mqtt.stop()
        controls.stop()
        dep.stop()
        log("stopped")


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
