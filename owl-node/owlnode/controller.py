"""The `controller` role (hub only): the designer and Routing pages and a thin NMOS controller behind them.

  GET  /                 the designer (D1): canvas of every device's ports + processing blocks, Design/Run/Panels
                         modes and the Routing grid as a view; static app, React Flow vendored under /vendor/
  GET  /routing          the Routing page (senders x receivers grid), also embedded by the designer
  /api/dsp/*             proxied to the `dsp` role (OWL_DSP_API, default http://127.0.0.1:8362): inventory, palette,
                         plan, deploy, rollback, status, named controls, panels, presets; /api/dsp/ws is tunnelled
                         at the TCP level (WebSocket upgrade) so the designer's live values stay same-origin (D2)
  GET  /api/graph        nodes, senders, receivers from the registry's IS-04 Query API, plus each owl-node's
                         /x-owl/v1/status (PTP lock, packets/s, loss, level)
  POST /api/patch        {receiver_id, sender_id|null}: IS-05 PATCH of the receiver's staged endpoint with the
                         sender's SDP and activate_immediate (null = disconnect). Audio (RTP) and video (SRT) alike.
                         Unicast RTP senders (N4 JPEG XS, urn:x-nmos:transport:rtp.ucast) are pointed at the receiver
                         first (IS-05 PATCH of the sender: destination_ip/port = the receiver's interface and port),
                         so the transport file the receiver gets names it; a receiver that loses or leaves a unicast
                         sender is disconnected, and a sender left without a receiver is disabled.
  POST /api/whep/<path>  WHEP signalling for a sender's browser preview, relayed to MediaMTX (same origin as the page;
                         the media itself goes browser <-> MediaMTX directly over WebRTC, UDP 8189)
  previews               which MediaMTX paths are live (the video senders' browser previews, N3), from MediaMTX's
                         API on the hub (OWL_MEDIAMTX_API, default http://127.0.0.1:9997); the add-on asks the hub's
                         dsp engine (/api/dsp/previews), since that API only listens on the hub's loopback

The registry stays the source of truth; this process keeps no state. Works with any IS-04/IS-05 node, not only
owl-nodes (Sony's nmos-cpp node is the reference it is tested against).
"""
import json, os, sys, threading, time, urllib.error, urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from . import VERSION

HERE = os.path.dirname(os.path.abspath(__file__))
MEDIAMTX_API = os.environ.get("OWL_MEDIAMTX_API", "http://127.0.0.1:9997")
MEDIAMTX_WEBRTC = os.environ.get("OWL_MEDIAMTX_WEBRTC", "http://127.0.0.1:8889")
DSP_API = os.environ.get("OWL_DSP_API", "http://127.0.0.1:8362").rstrip("/")
ADDON = os.environ.get("OWL_ADDON") or None     # F1: "remote" when this runs as the Home Assistant add-on
STATIC = {".js": "application/javascript", ".css": "text/css", ".html": "text/html; charset=utf-8", ".svg": "image/svg+xml",
          ".json": "application/json", ".png": "image/png"}
POOL = ThreadPoolExecutor(8)


def log(*a):
    print(time.strftime("%H:%M:%S"), "controller:", *a, flush=True)


def http(method, url, body=None, timeout=3, raw=False):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            b = r.read()
            return r.status, (b.decode() if raw else json.loads(b or b"null"))
    except urllib.error.HTTPError as e:
        b = e.read()
        try:
            return e.code, json.loads(b)
        except ValueError:
            return e.code, b.decode(errors="replace")


class Controller:
    def __init__(self, registry):
        self.registry = registry.rstrip("/")
        self.q = f"{self.registry}/x-nmos/query/v1.3"

    def graph(self):
        out = {"registry": self.registry, "time": time.time(), "error": None}
        futs = {k: POOL.submit(http, "GET", f"{self.q}/{k}?paging.limit=1000") for k in
                ("nodes", "devices", "senders", "receivers", "flows")}
        for k, f in futs.items():
            try:
                code, body = f.result()
                out[k] = body if code == 200 and isinstance(body, list) else []
                if code != 200:
                    out["error"] = f"query {k}: HTTP {code}"
            except Exception as e:
                out[k], out["error"] = [], f"registry unreachable: {e}"
        # owl status per node
        def st(node):
            for s in node.get("services", []):
                if s.get("type") == "urn:x-owl:service:status/v1":
                    try:
                        code, body = http("GET", s["href"], timeout=1.5)
                        return node["id"], body if code == 200 else {"error": f"HTTP {code}"}
                    except Exception as e:
                        return node["id"], {"error": str(e)}
            return node["id"], None
        out["status"] = dict(f.result() for f in [POOL.submit(st, n) for n in out["nodes"]])
        out["previews"] = self.previews()
        return out

    def previews(self):
        """{path: {ready, readers, bytes}} of MediaMTX (None when it is not running). MediaMTX's API listens on the
        hub's loopback only, so the Home Assistant add-on (another machine) asks the hub's dsp engine for it."""
        if ADDON:
            try:
                code, body = http("GET", f"{DSP_API}/api/dsp/previews", timeout=2)
                return body.get("previews") if code == 200 and isinstance(body, dict) else None
            except Exception:
                return None
        try:
            code, body = http("GET", f"{MEDIAMTX_API}/v3/paths/list?itemsPerPage=500", timeout=1)
            if code != 200:
                return None
            return {p["name"]: {"ready": p.get("ready"), "readers": len(p.get("readers") or []),
                                "tracks": p.get("tracks"), "bytes": p.get("bytesReceived")}
                    for p in body.get("items", [])}
        except Exception:
            return None

    def _get(self, kind, rid):
        code, body = http("GET", f"{self.q}/{kind}/{rid}")
        if code != 200:
            raise LookupError(f"{kind[:-1]} {rid} not in the registry (HTTP {code})")
        return body

    def _ctl(self, res):
        dev = self._get("devices", res["device_id"])
        ctl = next((c["href"] for c in dev.get("controls", []) if c.get("type", "").startswith("urn:x-nmos:control:sr-ctrl/v1.1")), None) \
            or next((c["href"] for c in dev.get("controls", []) if c.get("type", "").startswith("urn:x-nmos:control:sr-ctrl/")), None)
        if not ctl:
            raise LookupError(f"device {dev['id']} has no IS-05 control endpoint")
        return ctl.rstrip("/")

    def _ucast(self, tx):
        return tx.get("transport") == "urn:x-nmos:transport:rtp.ucast"

    def _park(self, sender_id, receiver_id):
        """Disable a unicast sender if it is still sending to receiver_id (nobody else can use its stream)."""
        try:
            tx = self._get("senders", sender_id)
        except LookupError:
            return
        if self._ucast(tx) and (tx.get("subscription") or {}).get("receiver_id") == receiver_id:
            code, _ = http("PATCH", f"{self._ctl(tx)}/single/senders/{sender_id}/staged",
                           {"receiver_id": None, "master_enable": False, "activation": {"mode": "activate_immediate"}})
            log(f"parked unicast sender {sender_id} (was sending to {receiver_id}) -> {code}")

    def _point(self, tx, rx, rx_ctl):
        """Aim a unicast sender at a receiver: its interface address and destination port, from the receiver's IS-05
        active parameters (an un-patched owl-node receiver has already resolved them to its own)."""
        code, act = http("GET", f"{rx_ctl}/single/receivers/{rx['id']}/active")
        tp = ((act or {}).get("transport_params") or [{}])[0] if code == 200 and isinstance(act, dict) else {}
        ip, port = tp.get("interface_ip"), tp.get("destination_port")
        if not ip or ip == "auto":
            node = self._get("nodes", self._get("devices", rx["device_id"])["node_id"])
            ip = ((node.get("api") or {}).get("endpoints") or [{}])[0].get("host")
        tp_tx = {"destination_ip": ip}
        if isinstance(port, int) and port > 0:
            tp_tx["destination_port"] = port
        code, resp = http("PATCH", f"{self._ctl(tx)}/single/senders/{tx['id']}/staged",
                          {"receiver_id": rx["id"], "master_enable": True, "transport_params": [tp_tx],
                           "activation": {"mode": "activate_immediate"}}, timeout=5)
        log(f"PATCH sender {tx['id']} -> {ip}:{tp_tx.get('destination_port', 'its port')} -> {code}")
        if code not in (200, 202):
            raise LookupError(f"unicast sender {tx['id']} refused destination {ip}: HTTP {code} {resp}")
        # one stream, one receiver: whoever else was patched to this sender has just lost it
        code, others = http("GET", f"{self.q}/receivers?subscription.sender_id={tx['id']}")
        for o in (others if code == 200 and isinstance(others, list) else []):
            if o["id"] != rx["id"] and (o.get("subscription") or {}).get("active"):
                c, _ = http("PATCH", f"{self._ctl(o)}/single/receivers/{o['id']}/staged",
                            {"sender_id": None, "master_enable": False, "activation": {"mode": "activate_immediate"}})
                log(f"disconnected receiver {o['id']} (its unicast sender now sends to {rx['id']}) -> {c}")

    def patch(self, receiver_id, sender_id):
        rx = self._get("receivers", receiver_id)
        ctl = self._ctl(rx)
        url = f"{ctl}/single/receivers/{receiver_id}/staged"
        old = (rx.get("subscription") or {}).get("sender_id")
        if sender_id is None:
            body = {"sender_id": None, "master_enable": False, "activation": {"mode": "activate_immediate"}}
        else:
            tx = self._get("senders", sender_id)
            if self._ucast(tx):
                self._point(tx, rx, ctl)
            code, sdp = http("GET", tx["manifest_href"], raw=True)       # (a unicast one now names this receiver)
            if code != 200:
                raise LookupError(f"sender {sender_id} transport file: HTTP {code}")
            # one transport_params entry per leg: ST 2022-7 receivers (nmos-cpp's) have two; our SDPs carry one
            code, cons = http("GET", f"{ctl.rstrip('/')}/single/receivers/{receiver_id}/constraints")
            legs = len(cons) if code == 200 and isinstance(cons, list) and cons else 1
            if rx.get("transport", "").startswith("urn:x-nmos:transport:rtp"):
                tps = [{"rtp_enabled": True}] + [{"rtp_enabled": False}] * (legs - 1)
            elif rx.get("transport", "").startswith("urn:x-matrox:transport:srt"):
                # the SDP carries the address; stream_id is reset (an RTSP camera relay may have set one)
                tps = [{"stream_id": None}] + [{}] * (legs - 1)
            else:       # others: the SDP carries the address; empty legs keep the receiver's own params
                tps = [{}] * legs
            body = {"sender_id": sender_id, "master_enable": True, "activation": {"mode": "activate_immediate"},
                    "transport_file": {"data": sdp, "type": "application/sdp"}, "transport_params": tps}
        t0 = time.time()
        code, resp = http("PATCH", url, body, timeout=5)
        log(f"PATCH {url} sender={sender_id} -> {code} in {1000 * (time.time() - t0):.0f} ms")
        if old and old != sender_id and code in (200, 202):
            self._park(old, receiver_id)
        return code, resp

    def set_receiver_latency(self, receiver_id, latency_ms):
        """owl-node extension: an AES67 receiver's playout latency (ms; None = the node's own default), on the agent
        of the node the receiver belongs to (x-owl/v1/receivers/<id>). -> (HTTP code, body)."""
        rx = self._get("receivers", receiver_id)
        node = self._get("nodes", self._get("devices", rx["device_id"])["node_id"])
        ep = ((node.get("api") or {}).get("endpoints") or [{}])[0]
        if not ep.get("host"):
            raise LookupError(f"node {node['id']} has no API endpoint")
        return http("PATCH", f"http://{ep['host']}:{ep.get('port') or 8360}/x-owl/v1/receivers/{receiver_id}",
                    {"latency_ms": latency_ms}, timeout=5)

    def patch_sdp(self, receiver_id, sdp, transport_params=None):
        """IS-05 PATCH of a receiver with a transport file of our own and no NMOS sender behind it (an RTSP camera
        relayed by the hub's MediaMTX): sender_id null, master_enable true."""
        rx = self._get("receivers", receiver_id)
        ctl = self._ctl(rx)
        body = {"sender_id": None, "master_enable": True, "activation": {"mode": "activate_immediate"},
                "transport_file": {"data": sdp, "type": "application/sdp"}, "transport_params": transport_params or [{}]}
        code, resp = http("PATCH", f"{ctl}/single/receivers/{receiver_id}/staged", body, timeout=5)
        log(f"PATCH receiver {receiver_id} <- own transport file ({sdp.splitlines()[2] if sdp.count(chr(10)) > 2 else ''}) -> {code}")
        return code, resp


class Handler(BaseHTTPRequestHandler):
    ctl = None
    server_version = "owl-node-controller/" + VERSION

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _static(self, rel, cache=False):
        path = os.path.normpath(os.path.join(HERE, "web", rel))
        if not path.startswith(os.path.join(HERE, "web")) or not os.path.isfile(path):
            return self._send(404, {"error": "not found"})
        with open(path, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", STATIC.get(os.path.splitext(path)[1], "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=86400" if cache else "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _proxy(self, method):
        """/api/dsp/* -> the dsp role (same host). Bodies and status codes pass through."""
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else None
        req = urllib.request.Request(DSP_API + self.path, data=body, method=method,
                                     headers={"Content-Type": self.headers.get("Content-Type") or "application/json",
                                              # behind Home Assistant's Ingress the Host is HA's internal address: say
                                              # nothing then (the engine falls back to its own LAN address)
                                              "X-Forwarded-Host": "" if self.headers.get("X-Ingress-Path") else (self.headers.get("Host") or "")})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return self._send(r.status, r.read(), r.headers.get("Content-Type") or "application/json")
        except urllib.error.HTTPError as e:
            return self._send(e.code, e.read(), e.headers.get("Content-Type") or "application/json")
        except Exception as e:
            return self._send(502, {"error": f"dsp role unreachable at {DSP_API}: {e}"})

    def do_PUT(self):
        if urlsplit(self.path).path.startswith("/api/dsp/"):
            return self._proxy("PUT")
        self._send(404, {"error": "not found"})

    def do_DELETE(self):
        if urlsplit(self.path).path.startswith("/api/dsp/"):
            return self._proxy("DELETE")
        self._send(404, {"error": "not found"})

    def _tunnel(self):
        """/api/dsp/ws: hand the TCP connection to the dsp role (WebSocket upgrade; the page stays same-origin)."""
        import socket
        u = urlsplit(DSP_API)
        try:
            up = socket.create_connection((u.hostname, u.port or 80), timeout=10)
        except OSError as e:
            return self._send(502, {"error": f"dsp role unreachable at {DSP_API}: {e}"})
        up.settimeout(None)
        head = self.raw_requestline + "".join(f"{k}: {v}\r\n" for k, v in self.headers.items()).encode() + b"\r\n"
        up.sendall(head)
        self.close_connection = True
        client = self.connection
        client.settimeout(None)

        def pump(a, b):
            try:
                while True:
                    d = a.recv(65536)
                    if not d:
                        break
                    b.sendall(d)
            except OSError:
                pass
            finally:
                for s_ in (a, b):
                    try:
                        s_.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
        t = threading.Thread(target=pump, args=(up, client), daemon=True)
        t.start()
        pump(client, up)
        t.join(2)

    def do_GET(self):
        p = urlsplit(self.path).path
        if p in ("/api/dsp/ws", "/api/dsp/probe") and "websocket" in (self.headers.get("Upgrade") or "").lower():
            return self._tunnel()
        if p == "/api/info":
            return self._send(200, {"version": VERSION, "dsp_api": DSP_API, "registry": self.ctl.registry if self.ctl else None,
                                    "addon": ADDON, "ingress": bool(self.headers.get("X-Ingress-Path"))})
        if p in ("/", "/index.html", "/designer"):
            return self._static("designer.html")
        if p == "/routing":
            return self._static("routing.html")
        if p.startswith("/vendor/"):
            return self._static(p[1:], cache=True)
        if p in ("/designer.js", "/designer.css"):          # the designer page's code and styles (not cached: they change)
            return self._static(p[1:])
        if p.startswith("/api/dsp/"):
            return self._proxy("GET")
        if p == "/api/graph":
            return self._send(200, self.ctl.graph())
        if p == "/healthz":
            return self._send(200, {"ok": True})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        p = urlsplit(self.path).path
        if p.startswith("/api/whep/"):
            return self.whep(p[len("/api/whep/"):].strip("/"))
        if p.startswith("/api/dsp/"):
            return self._proxy("POST")
        if p != "/api/patch":
            return self._send(404, {"error": "not found"})
        try:
            b = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            code, resp = self.ctl.patch(b["receiver_id"], b.get("sender_id"))
            self._send(200 if code in (200, 202) else 502, {"code": code, "response": resp})
        except (LookupError, KeyError, ValueError) as e:
            self._send(400, {"error": str(e)})
        except Exception as e:
            self._send(502, {"error": f"{type(e).__name__}: {e}"})


def _whep(self, path):
    import re
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", path):
        return self._send(400, {"error": "bad preview path"})
    offer = self.rfile.read(int(self.headers.get("Content-Length") or 0))
    req = urllib.request.Request(f"{MEDIAMTX_WEBRTC}/{path}/whep", data=offer, method="POST",
                                 headers={"Content-Type": "application/sdp"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return self._send(r.status, r.read(), "application/sdp")
    except urllib.error.HTTPError as e:
        return self._send(e.code, e.read(), e.headers.get("Content-Type") or "text/plain")
    except Exception as e:
        return self._send(502, {"error": f"MediaMTX: {e}"})


Handler.whep = _whep


def main():
    reg = os.environ.get("OWL_REGISTRY")
    port = int(os.environ.get("OWL_CONTROLLER_PORT", "8361"))
    if not reg:
        try:
            from . import config
            n = config.Node()
            reg = (n.registries or [None])[0]
            port = int((n.raw.get("controller") or {}).get("port", port))
        except Exception:
            pass
    if not reg:
        sys.exit("controller needs OWL_REGISTRY or registry: in node.yaml")
    Handler.ctl = Controller(reg)
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    srv.daemon_threads = True
    log(f"owl-node {VERSION} designer + Routing page on :{port}, registry {reg}, dsp API {DSP_API}")
    srv.serve_forever()
