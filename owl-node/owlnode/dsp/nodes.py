"""E1: nodes added by address. The household types a machine's address (192.0.2.20 or 192.0.2.20:8362) in the
designer; the hub checks that an owl-node dsp role answers there, pairs it (the node's agent then registers with this
hub's registry, which is what makes cross-host wires possible: AES67 senders/receivers are found there) and keeps
polling it, so its PipeWire inputs and outputs appear on the canvas. Kept in /var/lib/owl-node/dsp-nodes.json:

  {"nodes": [{"host", "url", "label", "added_at"}], "removed": ["<host>", ...]}

`removed` hides a node from registry discovery too (it may stay registered through its own node.yaml), until it is
added again. Nodes found through the registry (D3) keep working without an entry here."""
import os, re, threading, time
from urllib.parse import urlsplit

from ..config import STATE_DIR, atomic_write, read_json
from .satellite import http

NODES_FILE = os.path.join(STATE_DIR, "dsp-nodes.json")
DSP_PORT = 8362


class NodeError(Exception):
    pass


def parse_address(addr):
    """'192.0.2.20', '192.0.2.20:8362', 'http://pinode.local:8362/' -> (host, port|None)."""
    a = str(addr or "").strip()
    if not a:
        raise NodeError("type the node's address, e.g. 192.0.2.20 or 192.0.2.20:8362")
    if "://" not in a:
        a = "http://" + a
    u = urlsplit(a)
    if not u.hostname or not re.match(r"^[A-Za-z0-9.\-]+$", u.hostname):
        raise NodeError(f"{addr!r} is not an address (an IP like 192.0.2.20, optionally :port)")
    try:
        port = u.port
    except ValueError:
        raise NodeError(f"{addr!r}: the port must be a number")
    return u.hostname, port


class NodeBook:
    def __init__(self):
        self.lock = threading.Lock()
        d = read_json(NODES_FILE, None) or {}
        self.nodes = {n["host"]: n for n in d.get("nodes") or [] if n.get("host") and n.get("url")}
        self.removed = set(d.get("removed") or [])

    def _save(self):
        atomic_write(NODES_FILE, {"nodes": sorted(self.nodes.values(), key=lambda n: n["host"]), "removed": sorted(self.removed)})

    def entries(self):
        with self.lock:
            return [dict(n) for n in self.nodes.values()]

    def is_removed(self, host):
        return host in self.removed

    def probe(self, addr, hub_name, hub_addrs=()):
        """-> (url, status) of the owl-node dsp satellite at addr, or NodeError saying what answered instead."""
        host, port = parse_address(addr)
        ports = [port] if port else [DSP_PORT]
        if port and port != DSP_PORT:
            ports.append(DSP_PORT)                 # someone typed the agent's 8360 or the controller's 8361
        why = []
        for p in ports:
            url = f"http://{host}:{p}"
            try:
                code, st = http("GET", f"{url}/api/dsp/status", timeout=3)
            except Exception as e:
                es = f"{type(e).__name__} {e}"
                why.append(f"nothing listens on :{p}" if "refused" in es.lower() else
                           f"no answer on :{p} (machine off, wrong address or a firewall?)" if "timed out" in es.lower() else
                           f"something answers on :{p}, but it is not an owl-node dsp role" if "HTTP Error" in es else
                           f":{p} {es[:80]}")
                continue
            if code == 200 and isinstance(st, dict) and st.get("mode") == "satellite":
                if st.get("host") == hub_name:
                    raise NodeError(f"{host}:{p} is this hub itself")
                return url, st
            if code == 200 and isinstance(st, dict) and "deployed" in st and host in hub_addrs:
                raise NodeError(f"{host} is this hub itself: its own devices are on the canvas already")
            if code == 200 and isinstance(st, dict) and "deployed" in st:
                raise NodeError(f"{host}:{p} is another hub (its own designer runs there), not a node")
            why.append(f":{p} answered HTTP {code} but is not an owl-node dsp role")
        hint = ("Install owl-node there with the dsp role (install.sh --roles agent,audio,dsp) and start it; "
                f"the hub talks to its dsp role on port {DSP_PORT}.")
        raise NodeError(f"No owl-node at {host}: {'; '.join(why)}. {hint}")

    def add(self, addr, hub_name, registry, hub_url, hub_addrs=()):
        url, st = self.probe(addr, hub_name, hub_addrs)
        host = st["host"]
        paired, pair_error = False, None
        try:
            code, r = http("PUT", f"{url}/api/dsp/pair", {"registry": registry, "hub": hub_url, "hub_name": hub_name}, timeout=5)
            paired = code == 200 and bool((r or {}).get("ok"))
            if not paired:
                pair_error = (r or {}).get("error") if isinstance(r, dict) else f"HTTP {code}"
        except Exception as e:
            pair_error = f"{type(e).__name__}: {str(e)[:80]}"
        if pair_error and ("404" in str(pair_error) or "not found" in str(pair_error)):
            pair_error = f"owl-node {st.get('version')} there cannot be paired yet (update it); it keeps the registry of its own node.yaml"
        with self.lock:
            self.nodes[host] = {"host": host, "url": url, "label": st.get("label") or host, "added_at": time.time()}
            self.removed.discard(host)
            self._save()
        return {"ok": True, "host": host, "url": url, "label": st.get("label") or host, "version": st.get("version"),
                "paired": paired, "pair_error": pair_error, "roles": st.get("roles")}

    def remove(self, host):
        with self.lock:
            n = self.nodes.pop(host, None)
            self.removed.add(host)
            self._save()
        if n:
            try:
                http("DELETE", f"{n['url']}/api/dsp/pair", timeout=3)
            except Exception:
                pass
        return {"ok": True, "host": host, "was_added": bool(n)}
