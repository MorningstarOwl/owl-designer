"""Every port in the house as blocks with ports, from two sources, refreshed live:
  the hub's PipeWire graph  -> one block per device or app node (ALSA in/out, HDMI, squeezelite, ...); the hub's own
                               AES67 nodes are hidden (they are transport the compiler allocates)
  the NMOS registry         -> one block per node with its senders as outputs and receivers as inputs (every
                               satellite; the hub's video senders too, its audio ones being transport)
Block ids are stable across refreshes: pw:<host>:<node.name> and nmos:<node name>; port ids: in/out/monitor for
PipeWire nodes, tx:<sender uuid> / rx:<receiver uuid> for NMOS."""
import re, time

from . import pw
from .satellite import SatClient
from ..config import slug

DSP_PORT = 8362

SHOW_CLASSES = {"Audio/Sink": "sink", "Audio/Source": "source", "Audio/Duplex": "duplex",
                "Stream/Output/Audio": "app-out", "Stream/Input/Audio": "app-in", "Audio/Source/Virtual": "source",
                "Audio/Sink/Virtual": "sink"}
AUDIO, VIDEO, MUX = "urn:x-nmos:format:audio", "urn:x-nmos:format:video", "urn:x-nmos:format:mux"


def hidden(n, satellite=False):
    """The hub's AES67 nodes are transport the compiler allocates; a satellite's are shown (D3): its receiver is a
    source its blocks read from and its sender a sink they feed, and the compiler uses the same nodes for the
    automatic transport of cross-host wires."""
    p, name = n["props"], n["name"] or ""
    aes = name.startswith("aes67-") or p.get("device.api") == "aes67"
    return (name.startswith("owl-") or (aes and not satellite)
            or p.get("owl.dsp.block") or p.get("owl.dsp.meter") or p.get("node.passive") in (True, "true")
            or p.get("application.name") in ("pw-dump", "pw-cli", "pw-link", "pw-record", "pw-cat", "pw-play"))


def pw_blocks(graph, host, satellite=False, live=True):
    out = []
    for n in graph.nodes.values():
        kind = SHOW_CLASSES.get(n["class"] or "")
        if not kind or not n["name"] or hidden(n, satellite):
            continue
        p = n["props"]
        ins = graph.node_ports(n["name"], "in")
        outs = graph.node_ports(n["name"], "out", monitor=False)
        mons = graph.node_ports(n["name"], "out", monitor=True)
        inputs, outputs = [], []
        if ins:
            inputs.append({"id": "in", "label": "in", "media": "audio", "channels": len(ins),
                           "pw": {"node": n["name"], "dir": "in"}})
        if outs:
            outputs.append({"id": "out", "label": "out" if kind != "source" else "capture", "media": "audio",
                            "channels": len(outs), "pw": {"node": n["name"], "dir": "out", "monitor": False}})
        if mons:
            outputs.append({"id": "monitor", "label": "monitor", "media": "audio", "channels": len(mons),
                            "pw": {"node": n["name"], "dir": "out", "monitor": True}})
        if not inputs and not outputs:
            continue
        label = p.get("node.description") or p.get("node.nick") or n["name"]
        sub = p.get("application.name") or p.get("device.api") or (n["class"] or "").split("/")[0]
        if p.get("device.api") == "alsa":
            sub = "ALSA " + ("input" if kind == "source" else "output" if kind == "sink" else "duplex")
        if p.get("device.api") == "aes67" or n["name"].startswith("aes67-"):
            sub = "AES67 " + ("receiver" if kind == "source" else "sender")
        out.append({"id": f"pw:{host}:{n['name']}", "kind": "device", "source": "pipewire", "host": host,
                    "label": label, "sub": sub, "class": kind, "node": n["name"], "inputs": inputs, "outputs": outputs,
                    "live": live, "driver": p.get("priority.driver")})
    out.sort(key=lambda b: (b["class"], b["label"]))
    return out


def nmos_blocks(g, hub_node_id, hub_name):
    """g = the controller's graph() (registry nodes/devices/senders/receivers/flows + owl status)."""
    flows = {f["id"]: f for f in g.get("flows", [])}
    dev_node = {d["id"]: d["node_id"] for d in g.get("devices", [])}
    blocks = {}
    for n in g.get("nodes", []):
        st = (g.get("status") or {}).get(n["id"])
        if not st and not any(dev_node.get(d) == n["id"] for d in dev_node):
            continue                                   # the registry's own node: no devices
        name = (st or {}).get("name") or slug(n.get("label") or n.get("hostname") or n["id"])
        if n["id"] == hub_node_id:
            name = hub_name
        host = ((n.get("api") or {}).get("endpoints") or [{}])[0].get("host")
        blocks[n["id"]] = {"id": f"nmos:{name}", "kind": "device", "source": "nmos", "host": name,
                           "label": n.get("label") or name, "sub": host or "", "class": "node", "node_id": n["id"],
                           "inputs": [], "outputs": [], "live": True, "is_hub": n["id"] == hub_node_id,
                           "status": st, "area": (n.get("tags") or {}).get("urn:x-owl:area", [None])[0]}
    def media_of(fmt):
        return "audio" if fmt == AUDIO else "video" if fmt in (VIDEO, MUX) else "other"
    for s in g.get("senders", []):
        b = blocks.get(dev_node.get(s["device_id"]))
        if not b:
            continue
        f = flows.get(s["flow_id"]) or {}
        media = media_of(f.get("format"))
        if b["is_hub"] and media == "audio":
            continue                                   # transport: allocated by the compiler
        mt = f.get("media_type")
        if f.get("format") == MUX:
            mt = next((flows[p].get("media_type") for p in f.get("parents", []) if p in flows), mt)
        b["outputs"].append({"id": f"tx:{s['id']}", "label": s.get("label") or "sender", "media": media,
                             "channels": len(f.get("channels") or []) or None, "media_type": mt,
                             "nmos": {"sender": s["id"], "transport": s.get("transport"),
                                      "pw_node": (s.get("tags") or {}).get("urn:x-owl:pipewire-node", [None])[0],
                                      "preview": (s.get("tags") or {}).get("urn:x-owl:preview-page", [None])[0],
                                      "subscription": s.get("subscription")}})
    for r in g.get("receivers", []):
        b = blocks.get(dev_node.get(r["device_id"]))
        if not b:
            continue
        media = media_of(r.get("format"))
        if b["is_hub"] and media == "audio":
            continue
        b["inputs"].append({"id": f"rx:{r['id']}", "label": r.get("label") or "receiver", "media": media,
                            "channels": None, "media_types": (r.get("caps") or {}).get("media_types"),
                            "nmos": {"receiver": r["id"], "transport": r.get("transport"),
                                     "pw_node": (r.get("tags") or {}).get("urn:x-owl:pipewire-node", [None])[0],
                                     "subscription": r.get("subscription")}})
    out = [b for b in blocks.values() if b["inputs"] or b["outputs"] or not b["is_hub"]]
    out.sort(key=lambda b: (not b["is_hub"], b["label"]))
    return out


class Inventory:
    """Refreshed on demand (cached ~1.5 s). hub_receivers/hub_senders: the hub's audio transport for the compiler."""

    def __init__(self, cfg, controller):
        self.cfg, self.ctl = cfg, controller
        self.graph, self.nmos, self.time = None, None, 0
        self.blocks, self.hub_receivers, self.hub_senders = [], [], []
        self.senders, self.receivers, self.flows = {}, {}, {}
        self.error = None
        self._probes = {}
        self.sats = {}                  # host -> SatClient (D3: every non-hub owl-node is probed for a dsp role)
        self.book = None                # E1: NodeBook (nodes added by address in the designer), set by the server
        self.node_transport = {}        # host -> {senders: [...], receivers: [...]} (audio, with PipeWire node tags)

    def refresh(self, max_age=1.5):
        if time.time() - self.time < max_age and self.graph is not None:
            return self
        errs = []
        try:
            self.graph = pw.dump()
        except Exception as e:
            errs.append(f"pw-dump: {e}")
            if self.graph is None:
                self.graph = pw.Graph([])
        try:
            self.nmos = self.ctl.graph()
            if self.nmos.get("error"):
                errs.append(self.nmos["error"])
        except Exception as e:
            errs.append(f"registry: {e}")
            self.nmos = self.nmos or {"nodes": [], "devices": [], "senders": [], "receivers": [], "flows": [], "status": {}}
        self.error = "; ".join(errs) or None
        self.senders = {s["id"]: s for s in self.nmos.get("senders", [])}
        self.receivers = {r["id"]: r for r in self.nmos.get("receivers", [])}
        self.flows = {f["id"]: f for f in self.nmos.get("flows", [])}
        hub_devs = {d["id"] for d in self.nmos.get("devices", []) if d["node_id"] == self.cfg.node_id}
        self.hub_receivers = [r for r in self.receivers.values() if r["device_id"] in hub_devs and r.get("format") == AUDIO
                              and (r.get("tags") or {}).get("urn:x-owl:pipewire-node")]
        self.hub_senders = [s for s in self.senders.values() if s["device_id"] in hub_devs
                            and (self.flows.get(s["flow_id"]) or {}).get("format") == AUDIO
                            and (s.get("tags") or {}).get("urn:x-owl:pipewire-node")]
        self._find_satellites()
        self.node_transport = self._transport()
        self.blocks = pw_blocks(self.graph, self.cfg.name) + nmos_blocks(self.nmos, self.cfg.node_id, self.cfg.name)
        for host, c in self.sats.items():
            if c.graph is not None:
                self.blocks += pw_blocks(c.graph, host, satellite=True, live=c.ok)
            self.blocks += self._transport_placeholders(host, c.graph)
        self.time = time.time()
        return self

    def _transport_placeholders(self, host, graph):
        """A satellite's AES67 receiver node exists only while the receiver is patched (its sender's while enabled), and
        a design may both patch it and read from it: the registry knows the node names (urn:x-owl:pipewire-node), so
        a receiver or sender whose node is absent from the satellite's graph is still a device block (live: false;
        its links stay pending on the satellite until the node appears). Found 2026-11-10: a block test design
        unpatched pinode RX 1 and the strict restore of the Living room design then had no pw:pinode:aes67-rx-pinode-1."""
        out = []
        tr = self.node_transport.get(host) or {}
        for r in tr.get("receivers", []):
            n = r["pw_node"]
            if graph is not None and graph.node(n):
                continue
            out.append({"id": f"pw:{host}:{n}", "kind": "device", "source": "pipewire", "host": host, "label": f"AES67 RX {r.get('label')}",
                        "sub": "AES67 receiver (not patched: no node yet)", "class": "source", "node": n, "inputs": [],
                        "outputs": [{"id": "out", "label": "capture", "media": "audio", "channels": 2, "pw": {"node": n, "dir": "out", "monitor": False}}],
                        "live": False, "driver": None})
        for s_ in tr.get("senders", []):
            n = s_["pw_node"]
            if graph is not None and graph.node(n):
                continue
            out.append({"id": f"pw:{host}:{n}", "kind": "device", "source": "pipewire", "host": host, "label": f"AES67 TX: {s_.get('label')}",
                        "sub": "AES67 sender (disabled: no node yet)", "class": "sink", "node": n,
                        "inputs": [{"id": "in", "label": "in", "media": "audio", "channels": 2, "pw": {"node": n, "dir": "in"}}], "outputs": [],
                        "live": False, "driver": None})
        return out

    def _find_satellites(self):
        """One SatClient per registered owl-node that is not the hub: it probes :8362 (dsp role) and keeps polling.
        `self.sats` holds only the ones that have answered as a satellite dsp at least once. E1: plus every node added
        by address (the book); a node removed in the designer is ignored here even while it stays registered."""
        book = self.book
        if book is not None:
            for e in book.entries():
                c = self._probes.get(e["host"])
                if c is not None and c.url != e["url"]:            # re-added at another address
                    self.drop_satellite(e["host"])
                    c = None
                if c is None:
                    c = self._probes[e["host"]] = SatClient(e["host"], e["url"])
                    c.start()
        for n in self.nmos.get("nodes", []):
            st = (self.nmos.get("status") or {}).get(n["id"])
            if not st or n["id"] == self.cfg.node_id:
                continue
            name = st.get("name")
            host = ((n.get("api") or {}).get("endpoints") or [{}])[0].get("host")
            if not name or not host or name in self._probes or (book is not None and book.is_removed(name)):
                continue
            port = (st.get("dsp") or {}).get("port") or DSP_PORT
            c = self._probes[name] = SatClient(name, f"http://{host}:{port}")
            c.start()
        for name, c in self._probes.items():
            if c.seen and name not in self.sats:
                self.sats[name] = c
                print(time.strftime("%H:%M:%S"), f"dsp: satellite dsp role found on {name} ({c.url}, owl-node {c.version})", flush=True)

    def drop_satellite(self, host):
        """Forget a satellite (removed in the designer): stop polling it; its devices leave the canvas."""
        c = self._probes.pop(host, None)
        self.sats.pop(host, None)
        if c is not None:
            c.stop()
        self.time = 0

    def _transport(self):
        """Per owl-node: its audio senders and receivers that have PipeWire nodes (the compiler's cross-host transport)."""
        node_name = {}
        for n in self.nmos.get("nodes", []):
            st = (self.nmos.get("status") or {}).get(n["id"])
            if st and st.get("name"):
                node_name[n["id"]] = st["name"]
        dev_node = {d["id"]: d["node_id"] for d in self.nmos.get("devices", [])}
        out = {}
        for s in self.senders.values():
            host = node_name.get(dev_node.get(s["device_id"]))
            pwn = (s.get("tags") or {}).get("urn:x-owl:pipewire-node", [None])[0]
            if host and pwn and (self.flows.get(s["flow_id"]) or {}).get("format") == AUDIO:
                out.setdefault(host, {"senders": [], "receivers": []})["senders"].append(dict(s, pw_node=pwn))
        for r in self.receivers.values():
            host = node_name.get(dev_node.get(r["device_id"]))
            pwn = (r.get("tags") or {}).get("urn:x-owl:pipewire-node", [None])[0]
            if host and pwn and r.get("format") == AUDIO:
                out.setdefault(host, {"senders": [], "receivers": []})["receivers"].append(dict(r, pw_node=pwn))
        for h in out.values():
            h["senders"].sort(key=lambda x: x.get("label") or "")
            h["receivers"].sort(key=lambda x: x.get("label") or "")
        return out

    def block(self, bid):
        return next((b for b in self.blocks if b["id"] == bid), None)

    def match_node(self, host, match):
        """D3 device blocks: the PipeWire device block on `host` whose node name matches the glob (and class), or None."""
        import fnmatch
        pat, cls = match.get("node.name"), match.get("media.class")
        kind = SHOW_CLASSES.get(cls or "") if cls else None
        for b in self.blocks:
            if b.get("source") != "pipewire" or b.get("host") != host or not b.get("node"):
                continue
            if fnmatch.fnmatch(b["node"], pat) and (kind is None or b.get("class") == kind):
                g = self.graph if host == self.cfg.name else (self.sats.get(host).graph if host in self.sats else None)
                n = g.node(b["node"]) if g else None
                return dict(b, card=(n or {}).get("props", {}).get("alsa.card") if n else None)
        return None

    def port(self, bid, pid):
        b = self.block(bid)
        if not b:
            return None, None
        for p in b["inputs"]:
            if p["id"] == pid:
                return b, dict(p, dir="in")
        for p in b["outputs"]:
            if p["id"] == pid:
                return b, dict(p, dir="out")
        return b, None

    def sender_media(self, sid):
        f = self.flows.get((self.senders.get(sid) or {}).get("flow_id")) or {}
        return "audio" if f.get("format") == AUDIO else "video" if f.get("format") in (VIDEO, MUX) else "other"

    def receiver_media(self, rid):
        fmt = (self.receivers.get(rid) or {}).get("format")
        return "audio" if fmt == AUDIO else "video" if fmt in (VIDEO, MUX) else "other"

    def public(self):
        """What the canvas gets."""
        return {"time": self.time, "error": self.error, "hub": self.cfg.name,
                "satellites": {h: c.public() for h, c in self.sats.items()},
                "blocks": [{k: v for k, v in b.items() if k != "status"} | {"status": summarize_status(b.get("status"))}
                           for b in self.blocks]}


def summarize_status(st):
    if not st:
        return None
    p = st.get("ptp") or {}
    rx, tx = {}, {}
    for rid, r in (st.get("receivers") or {}).items():
        w = r.get("wire") or {}
        rx[rid] = {"active": r.get("master_enable"), "pkts_per_s": w.get("pkts_per_s"), "lost": w.get("lost_total"),
                   "rms_dbfs": w.get("rms_dbfs")}
    vid = st.get("video") or {}
    for rid, r in (vid.get("receivers") or {}).items():
        pr = r.get("process") or {}
        rx[rid] = {"active": r.get("master_enable"), "fps": pr.get("fps"), "running": pr.get("running"),
                   "lost": ((pr.get("rtp") or {}).get("lost") if pr.get("rtp") else (pr.get("srt") or {}).get("packets-received-lost")),
                   "latency_ms": (((pr.get("verify") or {}).get("latency_ms") or {}).get("recent") or {}).get("median")}
    for sid, s_ in (vid.get("senders") or {}).items():
        pr = s_.get("process") or {}
        tx[sid] = {"active": s_.get("master_enable"), "fps": pr.get("fps"), "running": pr.get("running"),
                   "callers": len(((pr.get("srt") or {}).get("callers") or [])), "codec": s_.get("codec")}
    return {"version": st.get("version"), "registered": st.get("registered"), "roles": st.get("roles"),
            "ptp": {"state": p.get("port_state"), "offset_ns": p.get("offset_ns"), "stale": p.get("stale")},
            "clock": (st.get("clock") or {}).get("ref_type"), "receivers": rx, "senders": tx}
