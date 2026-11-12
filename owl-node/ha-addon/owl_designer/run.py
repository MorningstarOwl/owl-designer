"""Owl Designer add-on entry point: the add-on's options -> the controller's environment, then the controller.

engine_url: the owl-node hub install's dsp API (port 8362 when none is given); the registry (8010) and MediaMTX
(9997 API, 8889 WebRTC) are on the same machine unless registry_url says otherwise. The page is served on 8361, which
is both Home Assistant's Ingress port and the optional LAN port."""
import json, os, sys, urllib.request
from urllib.parse import urlsplit

def log(msg):
    print(f"owl-designer: {msg}", flush=True)

try:
    with open("/data/options.json") as f:
        opts = json.load(f)
except (OSError, ValueError):
    opts = {}

def norm(url, port):
    url = (url or "").strip().rstrip("/")
    if not url:
        return ""
    if "://" not in url:
        url = "http://" + url
    u = urlsplit(url)
    if not u.hostname:
        return ""
    return f"{u.scheme}://{u.hostname}:{u.port or port}"

engine = norm(opts.get("engine_url"), 8362)
host = urlsplit(engine).hostname if engine else None
registry = norm(opts.get("registry_url"), 8010) or (f"http://{host}:8010" if host else "")
env = dict(os.environ,
           OWL_ADDON="remote",
           OWL_CONTROLLER_PORT="8361",
           OWL_DSP_API=engine or "http://engine-not-configured.invalid:8362",
           OWL_REGISTRY=registry or "http://registry-not-configured.invalid:8010",
           OWL_MEDIAMTX_API=f"http://{host}:9997" if host else "http://127.0.0.1:9",
           OWL_MEDIAMTX_WEBRTC=f"http://{host}:8889" if host else "http://127.0.0.1:9")
if not engine:
    log("engine_url is not set: the designer will open and say so. Set it in the add-on's Configuration tab "
        "(the machine with the owl-node hub install, e.g. 192.0.2.50), then restart the add-on.")
else:
    try:
        with urllib.request.urlopen(f"{engine}/healthz", timeout=4) as r:
            v = json.loads(r.read() or b"{}").get("version")
        log(f"engine {engine} answers (owl-node {v}); registry {registry}")
    except Exception as e:
        log(f"engine {engine} does not answer yet ({type(e).__name__}: {e}); the designer will keep trying")
os.execvpe(sys.executable, [sys.executable, "-u", "-m", "owlnode", "controller"], env)
