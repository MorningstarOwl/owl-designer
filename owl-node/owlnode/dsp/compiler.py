"""A wire is a wire. The compiler turns a design (blocks + wires) into what must exist:

  procs      {block id: SPA-JSON config}           one `pipewire -c` process per processing block on the hub
  links      [{src: {node, monitor, ports}, dst: {node, ports}, wire}]   PipeWire links on the hub, node-level;
                                                   the deployer pairs the channels when both nodes exist
  patches    {receiver id: sender id}              IS-05 patches (satellite -> satellite, and the hub's transport)
  alloc      {sender id: hub receiver id}          which hub AES67 receiver carries which satellite sender
  tx_alloc   {"<host>|<source>": {sender, offset, channels}}  which channels of which of a host's AES67 senders
                                                   carry which local source
  taps       {name: {node, monitor, ports, channels, block}}   meter taps on the hub (Meter blocks)
  controls   [(node, control, value)]              live filter-chain controls at their design values
  shares     {host: share}                         D3: the same four things for every satellite with a dsp role
                                                   (blocks placed there with "host": "<satellite>"), pushed to it

Cross-host rules (a block or device port lives on one host; the hub and every dsp satellite have PipeWire graphs):
  "a host's AES67 sender"            = channels of one: a sender of N channels carries N/2 stereo sources side by side
                                       (Dante-style flows). Each distinct source gets its own channels (a source that
                                       crosses to several places uses them once; multicast fans it out), kept across
                                       deploys; a receiving machine takes the whole stream and links only those
                                       channels. Only when no channels (or no receiver at the far end) are left do
                                       sources share channels, which mixes them (warned)
  satellite output -> hub input      = a link into that satellite's AES67 sender (its share) + an IS-05 patch of a hub
                                       receiver (reused when it already carries that sender, else a free one) + a link
                                       from the receiver's node on the hub
  hub output -> satellite input      = a link into the hub's AES67 sender + a patch of that satellite's receiver (one
                                       already carrying the hub, else a free one) + a link from the receiver's node in
                                       the satellite's share
  satellite A output -> satellite B  = A's sender -> B's receiver patch, with the links on both sides in their shares
  NMOS sender/receiver ports         = patches, as before (video wires are always patches, unicast senders aimed)
Never a transport choice for the user. Pure function of (design, inventory, block types, previous allocation): no
side effects."""
from . import blocks as B

NO_WIRE = object()


class CompileError(Exception):
    def __init__(self, errors):
        super().__init__("; ".join(errors))
        self.errors = errors


def new_share(host, design):
    return {"design": design, "host": host, "blocks": {}, "procs": {}, "block_nodes": {}, "links": [], "taps": {}, "controls": []}


class Desired:
    def __init__(self):
        self.procs, self.links, self.patches, self.alloc, self.taps, self.controls = {}, [], {}, {}, {}, []
        self.block_nodes, self.warnings, self.errors, self.wire_impl = {}, [], [], {}
        self.block_outputs = {}      # block id -> [(port id, node, ports, monitor)] for Run-mode meters
        self.block_host = {}         # block id -> host (the hub's name or a satellite's)
        self.shares = {}             # host -> share (satellite dsp roles)
        self.wire_hosts = {}         # wire id -> [satellite hosts whose share carries links of it]
        self.devices = {}            # device blocks (engine device): block id -> {host, match, node|None, label, setup}
        self.wire_rx, self.wire_patch = {}, {}   # wire id -> hub receiver it rides on / receiver it patches
        self.pending = {}            # wire id -> why it is not compiled yet (a device that is absent; non-strict only)
        self.moves = []              # [{node, target, wire}]: app streams re-targeted into a block (wire "move": true)
        self.tx_alloc = {}           # "<host>|<source key>" -> {sender, offset, channels} (stable across deploys; pick_slot)
        self.voice = None            # the Voice assistant block (engine voice): {block, label, mic, reply, mic_wire, reply_wire}
        self.voice_wires = {}        # wire id -> the hub node it names (the assistant's listen source / reply target)
        self.rtsp_video = {}         # RTSP blocks with video: block id -> {url, path, latency_ms, codec, label} (MediaMTX)
        self.raw_patches = {}        # receiver id -> {block, path, label, latency_ms, wire}: IS-05 patch with our own SDP

    def share(self, host, design_name):
        return self.shares.setdefault(host, new_share(host, design_name))

    def summary(self):
        return {"procs": sorted(self.procs), "links": self.links, "patches": self.patches, "alloc": self.alloc, "tx_alloc": self.tx_alloc,
                "taps": self.taps, "warnings": self.warnings, "wires": self.wire_impl, "moves": self.moves,
                "shares": {h: {"blocks": sorted(s["blocks"]), "links": len(s["links"]), "taps": sorted(s["taps"])} for h, s in self.shares.items()}}


def parse_ep(ref):
    """'<block id>|<port id>' (block ids themselves contain ':' for pw:host:node and nmos:host)."""
    if not isinstance(ref, str) or "|" not in ref:
        return None, None
    b, p = ref.rsplit("|", 1)
    return b, p


class Slot:
    """Channels [offset, offset + width) of an AES67 sender (tx: the inventory's sender dict, with pw_node)."""

    def __init__(self, tx, offset, width):
        self.tx, self.offset, self.width = tx, offset, width

    def ports(self):
        return list(range(self.offset, self.offset + self.width))

    def span(self):
        return f"{self.offset + 1}" if self.width == 1 else f"{self.offset + 1}-{self.offset + self.width}"

    def __eq__(self, o):
        return isinstance(o, Slot) and (self.tx["id"], self.offset, self.width) == (o.tx["id"], o.offset, o.width)

    def __hash__(self):
        return hash((self.tx["id"], self.offset, self.width))


def sender_channels(tx):
    """An AES67 sender's channel count: the owl-node tag (urn:x-owl:channels), else 2 (other NMOS nodes, owl-node
    before 0.8, which only had stereo senders)."""
    try:
        return max(1, int(((tx.get("tags") or {}).get("urn:x-owl:channels") or [2])[0]))
    except (TypeError, ValueError):
        return 2


def source_key(src):
    """A local source as the sender allocation sees it: the node, monitor or not, and the channel subset."""
    ports = src.get("ports")
    return f"{src['node']}{'#mon' if src.get('monitor') else ''}" + ("" if ports is None else "[" + ",".join(map(str, ports)) + "]")


def compile_design(design, inv, types, prev_alloc=None, hub=None, strict=True, prev_tx_alloc=None):
    """strict: a device that is absent is an error (a user's Deploy). Non-strict (restore after a reboot, reconcile): its
    wires are left pending and everything else compiles; the deployer recompiles as devices appear.
    prev_alloc / prev_tx_alloc: the last deploy's hub receiver and sender allocations, kept where still possible so a
    re-deploy does not re-patch streams that did not change."""
    d = Desired()
    absent = []
    hub = hub or inv.cfg.name
    dname = design.get("name")
    prev_alloc = dict(prev_alloc or {})
    prev_tx_alloc = dict(prev_tx_alloc or {})
    sat_hosts = set(getattr(inv, "sats", {}) or {})
    blocks = {}
    # ---- processing blocks
    for b in design.get("blocks") or []:
        bid = b.get("id")
        if not bid or not B.ID_RE.match(bid):
            d.errors.append(f"block id {bid!r} must match {B.ID_RE.pattern}")
            continue
        if bid in blocks:
            d.errors.append(f"duplicate block id {bid}")
            continue
        bt = types.get(b.get("type"))
        if not bt:
            d.errors.append(f"block {bid}: unknown type {b.get('type')!r}")
            continue
        host = b.get("host") or hub
        if host != hub and host not in sat_hosts:
            if strict:
                d.errors.append(f"block {bid}: no satellite dsp role on {host!r} (it is down, or owl-node there predates D3)")
                continue
            absent.append(f"satellite {host} has no dsp role now")
        ctx = bt.ctx(bid, b.get("label"), b.get("params"))
        nm = bt.node_map(ctx)
        if bt.engine == "voice":
            if host != hub:
                d.errors.append(f"block {bid}: the voice assistant runs on the hub only")
                continue
            if d.voice:
                d.errors.append(f"block {bid}: a design has one voice assistant ({d.voice['block']} is already one)")
                continue
            d.voice = {"block": bid, "label": ctx.label, "mic": None, "reply": None, "mic_wire": None, "reply_wire": None}
        blocks[bid] = {"type": bt, "ctx": ctx, "nodes": nm, "ports": bt.ports(ctx.params), "host": host}
        d.block_host[bid] = host
        if bt.engine == "voice":
            continue
        if bt.engine == "device":
            m = inv.match_node(host, bt.match) if hasattr(inv, "match_node") else None
            d.devices[bid] = {"host": host, "match": bt.match, "node": m["node"] if m else None, "label": ctx.label,
                              "card": (m or {}).get("card"), "setup": bt.setup_cmds(ctx, (m or {}).get("card")), "params": ctx.params}
            blocks[bid]["device"] = d.devices[bid]
            continue
        if bt.engine == "rtsp":
            if host != hub:
                d.errors.append(f"block {bid}: an RTSP source runs on the hub only")
                continue
            url = str(ctx.params.get("url") or "").strip()
            if not url:
                d.warnings.append(f"block {bid} ({ctx.label}): no RTSP URL yet")
            elif not url.startswith(("rtsp://", "rtsps://")):
                d.errors.append(f"block {bid} ({ctx.label}): the URL must start with rtsp:// or rtsps://")
                continue
            conf = bt.render_conf(ctx)
            if conf:
                d.procs[bid] = conf
            d.block_nodes[bid] = bt.node_names(ctx)
            if url and ctx.params.get("video"):
                d.rtsp_video[bid] = {"url": url, "path": f"owl-cam-{bid}", "latency_ms": int(ctx.params.get("latency_ms") or 200),
                                     "codec": ctx.params.get("video_codec") or "auto", "label": ctx.label}
            d.block_outputs[bid] = [(pid, nm[pid]["node"], nm[pid].get("ports"), False) for pid in nm if conf]
            continue
        if host == hub:
            if bt.engine == "pipewire":
                d.procs[bid] = bt.render_conf(ctx)
                d.controls += bt.controls(ctx)
                for l in bt.internal(ctx):
                    d.links.append(dict(l, wire=f"{bid}:internal"))
            d.block_nodes[bid] = bt.node_names(ctx)
        elif host in sat_hosts:
            sh = d.share(host, dname)
            sh["blocks"][bid] = {"type": bt.type, "label": ctx.label, "params": ctx.params}
            if bt.engine == "pipewire":
                sh["procs"][bid] = bt.render_conf(ctx)
                sh["controls"] += [list(c) for c in bt.controls(ctx)]
                for l in bt.internal(ctx):
                    sh["links"].append(dict(l, wire=f"{bid}:internal"))
            sh["block_nodes"][bid] = bt.node_names(ctx)
        d.block_outputs[bid] = [(pid, nm[pid]["node"], nm[pid].get("ports"), bool(nm[pid].get("monitor"))) for pid in nm if nm[pid]["dir"] == "out"]

    def endpoint(ref, role):
        """-> dict(kind=pw|nmos_tx|nmos_rx|meter, host, ...), or None with an error recorded."""
        bid, pid = parse_ep(ref)
        if bid is None:
            d.errors.append(f"wire {role} {ref!r}: expected '<block id>|<port id>'")
            return None
        if bid in blocks:
            bk = blocks[bid]
            ports = bk["ports"]["outputs" if role == "from" else "inputs"]
            port = next((p for p in ports if p["id"] == pid), None)
            if not port:
                d.errors.append(f"wire {role} {ref}: block {bid} has no {'output' if role == 'from' else 'input'} {pid!r}")
                return None
            if bk["host"] != hub and bk["host"] not in sat_hosts:
                absent.append(f"satellite {bk['host']} (block {bid}) has no dsp role now")
                return None
            if bk["type"].engine == "device":
                dv = bk["device"]
                if not dv["node"]:
                    absent.append(f"device {dv['label']} ({bk['type'].label}) is not plugged in on {bk['host']} (no node matching {dv['match'].get('node.name')})")
                    return None
                return {"kind": "pw", "node": dv["node"], "monitor": False, "ports": None, "media": "audio", "channels": port["channels"],
                        "device": dv["label"], "host": bk["host"], "block": None}
            if bk["type"].engine == "voice":
                return {"kind": "voice", "port": pid, "block": bid, "media": "audio", "channels": port["channels"], "host": hub}
            if bk["type"].engine == "rtsp":
                p_ = bk["ctx"].params
                if port.get("media") == "video":
                    if not p_.get("video"):
                        d.errors.append(f"wire {role} {ref}: {bk['ctx'].label} has its video switched off")
                        return None
                    return {"kind": "rtsp_video", "block": bid, "media": "video", "host": hub, "label": bk["ctx"].label}
                if not (p_.get("audio") and str(p_.get("url") or "").strip()):
                    d.errors.append(f"wire {role} {ref}: {bk['ctx'].label} pulls no audio (audio off or no URL)")
                    return None
            n = bk["nodes"][pid]
            if bk["type"].engine == "meter":
                return {"kind": "meter", "block": bid, "node": n["node"], "channels": port["channels"], "media": "audio", "host": bk["host"]}
            return {"kind": "pw", "node": n["node"], "monitor": bool(n.get("monitor")), "ports": n.get("ports"), "media": "audio",
                    "channels": port["channels"], "block": bid, "host": bk["host"]}
        if bid.startswith("pw:") and bid.split(":")[1] not in sat_hosts | {hub} and not inv.block(bid):
            msg = f"no device {bid} (satellite {bid.split(':')[1]} has no dsp role now, or is down)"
            if strict:
                d.errors.append(f"wire {role} {ref}: {msg}")
            else:
                absent.append(msg)
            return None
        blk, port = inv.port(bid, pid)
        if not blk:
            msg = f"no device {bid} in the inventory (unplugged, an app not running, or a satellite that is down)"
            if strict:
                d.errors.append(f"wire {role} {ref}: {msg}")
            else:
                absent.append(msg)
            return None
        if not port or port["dir"] != ("out" if role == "from" else "in"):
            d.errors.append(f"wire {role} {ref}: device {blk['label']} has no {'output' if role == 'from' else 'input'} {pid!r}")
            return None
        if "pw" in port:
            return {"kind": "pw", "node": port["pw"]["node"], "monitor": bool(port["pw"].get("monitor")), "ports": None,
                    "media": "audio", "channels": port["channels"], "device": blk["label"], "app": blk.get("class") == "app-out",
                    "host": blk["host"]}
        if "sap" in port:                      # a third-party AES67 stream announced over SAP (sap.py)
            sp = port["sap"]
            return {"kind": "sap_tx", "sender": f"sap:{sp['stream']}", "sdp": sp["sdp"], "ports": sp.get("ports"),
                    "media": "audio", "channels": port["channels"], "label": f"{blk['label']}" + (
                        f" ({port['label']})" if sp.get("ports") is not None else ""), "host": None}
        if "nmos" in port:
            if role == "from":
                return {"kind": "nmos_tx", "sender": port["nmos"]["sender"], "media": port["media"], "host": blk["host"],
                        "label": port["label"], "media_type": port.get("media_type"), "is_hub": blk.get("is_hub"),
                        "pw_node": port["nmos"].get("pw_node")}
            return {"kind": "nmos_rx", "receiver": port["nmos"]["receiver"], "media": port["media"], "host": blk["host"],
                    "label": port["label"], "media_types": port.get("media_types"), "is_hub": blk.get("is_hub"),
                    "pw_node": port["nmos"].get("pw_node")}
        d.errors.append(f"wire {role} {ref}: port has no transport")
        return None

    hub_rx_pool = {r["id"]: r for r in inv.hub_receivers}
    used_rx = set()
    sap_sdp = {}                 # "sap:<stream>" -> {sdp, label}: receivers patched with an announced SDP, no NMOS sender

    def set_patch(rid, sender_id, wid):
        """A receiver's source: an NMOS sender (d.patches) or an announced stream (d.raw_patches, our own SDP)."""
        if sender_id in sap_sdp:
            d.raw_patches[rid] = dict(sap_sdp[sender_id], wire=wid)
        else:
            d.patches[rid] = sender_id
    transport = getattr(inv, "node_transport", {}) or {}

    def alloc_receiver(sender_id, wire_id):
        if sender_id in d.alloc:
            return d.alloc[sender_id]
        cands = []
        if prev_alloc.get(sender_id) in hub_rx_pool and prev_alloc[sender_id] not in used_rx:
            cands.append(prev_alloc[sender_id])                                   # keep what the last deploy used
        for r in hub_rx_pool.values():
            if r["id"] in used_rx:
                continue
            sub = r.get("subscription") or {}
            if sub.get("active") and sub.get("sender_id") == sender_id:
                cands.append(r["id"])                                              # already carries it: adopt
        held = {rid for sid, rid in prev_alloc.items() if sid != sender_id}   # the last deploy's, for other senders
        for r in sorted(hub_rx_pool.values(), key=lambda r: r["id"] in held):
            sub = r.get("subscription") or {}
            if r["id"] not in used_rx and not sub.get("active"):
                cands.append(r["id"])                                              # free (others' previous last)
        busy = []
        for r in hub_rx_pool.values():
            sub = r.get("subscription") or {}
            if r["id"] not in used_rx and sub.get("active") and sub.get("sender_id") not in d.alloc:
                busy.append(r["id"])                                               # carrying something not in the design
        if not cands and not busy:
            d.errors.append(f"wire {wire_id}: no free AES67 receiver on the hub for this sender (add one: install.sh --rx)")
            return None
        if not cands:
            cands = busy
            r = hub_rx_pool[busy[0]]
            d.warnings.append(f"wire {wire_id}: hub receiver {r.get('label')} is re-targeted from "
                              f"{(r.get('subscription') or {}).get('sender_id')}")
        rid = cands[0]
        d.alloc[sender_id] = rid
        used_rx.add(rid)
        set_patch(rid, sender_id, wire_id)
        return rid

    rx_sources = {}
    # AES67 sender nodes a wire of the design feeds directly (a satellite's sender shown as a device block): the
    # allocation leaves those alone, or a compiled wire would be mixed into a stream the user wired by hand
    explicit_tx = set()
    for w in design.get("wires") or []:
        b_, _ = parse_ep(w.get("to"))
        if b_ and b_.startswith("pw:") and b_.count(":") >= 2:
            explicit_tx.add(b_.split(":", 2)[2])
    tx_by_src = {}               # host -> {source key: Slot}
    tx_chan = {}                 # host -> {sender id: [source key or None per channel]}

    def sender_pool(host):
        """The host's AES67 senders (with their PipeWire node as pw_node and channel count), in a stable order."""
        if host == hub:
            txs = [dict(t, pw_node=(t.get("tags") or {}).get("urn:x-owl:pipewire-node", [None])[0]) for t in inv.hub_senders]
        else:
            txs = list((transport.get(host) or {}).get("senders") or [])
        txs = [dict(t, channels=sender_channels(t)) for t in txs if t.get("pw_node")]
        return sorted(txs, key=lambda t: (t.get("label") or "", t["id"]))

    def rx_free(dst_host):
        """Whether dst_host still has an AES67 receiver for one more sender (None: no receiver needed, e.g. a wire
        straight into an NMOS receiver)."""
        if dst_host is None:
            return True
        if dst_host == hub:
            return any(r not in used_rx for r in hub_rx_pool)
        return any(r["id"] not in rx_sources for r in (transport.get(dst_host) or {}).get("receivers") or [])

    def rx_has(dst_host, sender_id):
        if dst_host is None:
            return False
        if dst_host == hub:
            return sender_id in d.alloc
        return any(rx_sources.get(r["id"]) == sender_id for r in (transport.get(dst_host) or {}).get("receivers") or [])

    def prev_slot(host, key):
        v = prev_tx_alloc.get(f"{host}|{key}")
        if isinstance(v, str):                       # 0.7.3 stored a bare sender id (a whole stereo sender)
            return {"sender": v, "offset": 0, "channels": None}
        return v if isinstance(v, dict) else None

    def pick_slot(host, src, wid, dst_host=None, whole=False):
        """The channels of one of `host`'s AES67 senders that carry `src` (a local source) off the machine -> Slot, or
        None with an error. A sender of N channels carries N/2 stereo sources side by side (a receiver takes the whole
        stream and links only that source's channels), like a Dante flow. In order of preference: this source's slot
        from the last deploy (no re-patch), channels in a sender `dst_host` already receives (no new receiver), channels
        in a sender that is in use (packing), a free sender. Channels shared with another source (they mix) only when
        nothing fits. whole: the far end is an NMOS receiver that plays the stream from its first channel (third-party
        gear), so the source must start at channel 1."""
        pool = [t for t in sender_pool(host) if t["pw_node"] not in explicit_tx]
        if not pool:
            d.errors.append(f"wire {wid}: {host} has no AES67 sender to carry this (install.sh --tx there)")
            return None
        key = source_key(src)
        mine, chans = tx_by_src.setdefault(host, {}), tx_chan.setdefault(host, {})
        if key in mine:
            sl = mine[key]
            if whole and sl.offset:
                d.warnings.append(f"wire {wid}: {key} is on channels {sl.span()} of AES67 sender {sl.tx.get('label')}; "
                                  f"{dst_host or 'the receiver'} gets the whole stream and may play channels 1-2 instead")
            return sl
        for t in pool:
            chans.setdefault(t["id"], [None] * t["channels"])
        w = max(1, int(src.get("channels") or 2))
        mine_before = prev_slot(host, key)
        held = {}                                   # sender id -> channels the last deploy gave other sources
        for k, v in prev_tx_alloc.items():
            if k.startswith(host + "|") and k != f"{host}|{key}":
                pv = prev_slot(host, k.split("|", 1)[1])
                if pv and pv.get("sender"):
                    held.setdefault(pv["sender"], set()).update(range(pv.get("offset") or 0, (pv.get("offset") or 0) + (pv.get("channels") or 2)))
        cands = []
        for order, t in enumerate(pool):
            occ, cap = chans[t["id"]], t["channels"]
            width = min(w, cap)
            has = rx_has(dst_host, t["id"])
            if not has and not rx_free(dst_host):
                continue                            # the far end could not receive one more sender
            offs = [0] if whole else range(0, cap - width + 1, width)
            for off in offs:
                if any(occ[c] is not None for c in range(off, off + width)):
                    continue
                own = bool(mine_before and mine_before.get("sender") == t["id"] and (mine_before.get("offset") or 0) == off)
                taken = bool(held.get(t["id"], set()) & set(range(off, off + width)))
                used = any(x is not None for x in occ)
                cands.append(((not own, taken, not has, not used, order, off), t, off, width))
        if cands:
            _, tx, off, width = min(cands, key=lambda c: c[0])
            if width < w:
                d.warnings.append(f"wire {wid}: {key} has {w} channels but AES67 sender {tx.get('label')} carries {width}: "
                                  f"only the first {width} cross to the other machine")
            sl = Slot(tx, off, width)
            for c in sl.ports():
                chans[tx["id"]][c] = key
        else:
            # nothing fits: share the channels of a source already on a sender (prefer one dst_host receives); they mix
            others = [o for o in mine.values()]
            if not others:
                tx = pool[0]
                sl = Slot(tx, 0, min(w, tx["channels"]))
                d.warnings.append(f"wire {wid}: {dst_host} has no free AES67 receiver for {host}'s sender "
                                  f"(install.sh --rx there adds one); a receiver there is re-targeted")
            else:
                sl = next((o for o in others if rx_has(dst_host, o.tx["id"])), others[0])
                full = all(all(x is not None for x in chans[t["id"]]) for t in pool)
                why = (f"every channel of {host}'s AES67 senders is in use (more channels: --tx '<label>|channels=8', or "
                       f"another --tx)") if full else f"{dst_host} has no free AES67 receiver (install.sh --rx there adds one)"
                d.warnings.append(f"wire {wid}: {key} shares channels {sl.span()} of AES67 sender {sl.tx.get('label')} with "
                                  f"{', '.join(sorted({k for k, o in mine.items() if o == sl}))}: {why}; whoever receives "
                                  f"them hears the sum")
        mine[key] = sl
        d.tx_alloc[f"{host}|{key}"] = {"sender": sl.tx["id"], "offset": sl.offset, "channels": sl.width}
        return sl

    def sat_receiver(host, sender_id, wid):
        """A receiver on `host` for sender_id: one already carrying it (in this design or live), else a free one."""
        rxs = (transport.get(host) or {}).get("receivers") or []
        for r in rxs:
            if rx_sources.get(r["id"]) == sender_id:
                return r
        for r in rxs:
            if r["id"] not in rx_sources and ((r.get("subscription") or {}).get("sender_id") == sender_id and (r.get("subscription") or {}).get("active")):
                return r
        for r in rxs:
            if r["id"] not in rx_sources and not (r.get("subscription") or {}).get("active"):
                return r
        for r in rxs:
            if r["id"] not in rx_sources:
                d.warnings.append(f"wire {wid}: receiver {r.get('label')} on {host} is re-targeted")
                return r
        d.errors.append(f"wire {wid}: no free AES67 receiver on {host} (install.sh --rx there)")
        return None

    def local_link(host, src, dst, wid):
        """A PipeWire link (or meter tap) on one host: the hub's lists or the host's share."""
        if dst["kind"] == "meter":
            tap = {"node": src["node"], "monitor": src.get("monitor", False), "ports": src.get("ports"),
                   "channels": src.get("channels") or 2, "block": dst["block"], "tap_node": dst["node"]}
            if host == hub:
                d.taps[dst["block"]] = tap
            else:
                d.share(host, dname)["taps"][dst["block"]] = tap
                d.wire_hosts.setdefault(wid, []).append(host)
            return f"meter tap on {src['node']}" + ("" if host == hub else f" ({host})")
        link = {"src": {"node": src["node"], "monitor": src.get("monitor", False), "ports": src.get("ports")},
                "dst": {"node": dst["node"], "ports": dst.get("ports")}, "wire": wid}
        if host == hub:
            d.links.append(link)
        else:
            d.share(host, dname)["links"].append(link)
            d.wire_hosts.setdefault(wid, []).append(host)
        return f"PipeWire link {src['node']} -> {dst['node']}" + ("" if host == hub else f" ({host})")

    def into_sender(host, src, wid, dst_host=None, whole=False):
        """src (on `host`) -> its channels of one of that host's AES67 senders -> (Slot, implementation) or (None, None)."""
        sl = pick_slot(host, src, wid, dst_host, whole)
        if not sl:
            return None, None
        impl = local_link(host, src, {"kind": "pw", "node": sl.tx["pw_node"], "ports": sl.ports()}, wid)
        return sl, impl + f" ({'hub ' if host == hub else ''}AES67 sender {sl.tx.get('label')}, channel{'' if sl.width == 1 else 's'} {sl.span()})"

    def from_hub_receiver(sender_id, dst, wid, src_label, src_host, ports=None):
        """ports: the sender's channels this wire carries (its slot); None = the whole stream."""
        rid = alloc_receiver(sender_id, wid)
        if not rid:
            return None
        rxn = (hub_rx_pool[rid].get("tags") or {}).get("urn:x-owl:pipewire-node", [None])[0]
        d.wire_rx[wid] = rid
        d.wire_patch[wid] = rid
        impl = [f"IS-05 patch hub receiver {hub_rx_pool[rid].get('label')} <- {src_label} ({src_host})"]
        impl.append(local_link(hub, {"kind": "pw", "node": rxn, "monitor": False, "ports": ports}, dst, wid))
        return impl

    def from_sat_receiver(host, sender_id, dst, wid, src_label, src_host, ports=None):
        rx = sat_receiver(host, sender_id, wid)
        if not rx:
            return None
        rx_sources[rx["id"]] = sender_id
        set_patch(rx["id"], sender_id, wid)
        d.wire_patch[wid] = rx["id"]
        impl = [f"IS-05 patch {rx.get('label')} ({host}) <- {src_label} ({src_host})"]
        impl.append(local_link(host, {"kind": "pw", "node": rx["pw_node"], "monitor": False, "ports": ports}, dst, wid))
        return impl

    def voice_wire(wid, src, dst):
        """A wire into the assistant's listen input or out of its reply output: no link of ours; it names the node the
        satellite records from (pw-record --target) or plays its answers into (pw-play --target)."""
        v = d.voice
        if dst["kind"] == "voice":
            if src["kind"] != "pw" or src.get("host") != hub:
                raise CompileError([f"wire {wid}: the voice assistant listens to a PipeWire source on the hub (a mic, the AEC's clean mic)"])
            if src.get("monitor"):
                raise CompileError([f"wire {wid}: the voice assistant cannot listen to a speaker's monitor; wire a mic or the AEC's clean mic"])
            if v["mic"]:
                raise CompileError([f"wire {wid}: the voice assistant already listens to {v['mic']} (wire {v['mic_wire']}); it has one listen wire"])
            v["mic"], v["mic_wire"] = src["node"], wid
            d.voice_wires[wid] = src["node"]
            return f"voice assistant listens to {src['node']} (pw-record while the mic is live)"
        if dst["kind"] != "pw" or dst.get("host") != hub:
            raise CompileError([f"wire {wid}: the voice assistant's reply goes to a PipeWire input on the hub (the ducker's sidechain, a speaker)"])
        if v["reply"]:
            raise CompileError([f"wire {wid}: the voice assistant already replies into {v['reply']} (wire {v['reply_wire']}); it has one reply wire"])
        v["reply"], v["reply_wire"] = dst["node"], wid
        d.voice_wires[wid] = dst["node"]
        return f"voice assistant replies play into {dst['node']}"

    for w in design.get("wires") or []:
        wid = w.get("id") or f"{w.get('from')}->{w.get('to')}"
        del absent[:]
        src, dst = endpoint(w.get("from"), "from"), endpoint(w.get("to"), "to")
        if not src or not dst:
            if absent and not strict:
                d.pending[wid] = "; ".join(absent)
            elif absent:
                d.errors.append(f"wire {wid}: " + "; ".join(absent))
            continue
        if src["media"] != dst["media"] and src["media"] != "other" and dst["media"] != "other":
            d.errors.append(f"wire {wid}: {src['media']} into {dst['media']}")
            continue
        sh, dh = src.get("host"), dst.get("host")
        impl = []
        sc, dc = src.get("channels"), dst.get("channels")
        bk = blocks.get(dst.get("block")) or {}
        if (bk.get("type") is not None and bk["type"].type == "aec" and parse_ep(w.get("to"))[1] == "mic"
                and sc and dc and sc != dc and sc in (1, 2)):
            d.warnings.append(f"wire {wid}: {src.get('device') or src['node']} is a {sc}-channel mic but {bk['ctx'].label}'s "
                              f"Mic channels is {dc}: set it to {sc}")
        try:
            if src["kind"] == "rtsp_video":
                if dst["kind"] != "nmos_rx" or dst["media"] != "video":
                    raise CompileError([f"wire {wid}: a camera's video goes to a video receiver (an SRT receiver of a node)"])
                if dst["receiver"] in rx_sources or dst["receiver"] in d.raw_patches:
                    raise CompileError([f"wire {wid}: receiver {dst['label']} already has another source"])
                v = d.rtsp_video.get(src["block"])
                if not v:
                    raise CompileError([f"wire {wid}: {src['label']} has no RTSP URL yet"])
                d.raw_patches[dst["receiver"]] = dict(v, block=src["block"], wire=wid)
                d.wire_patch[wid] = dst["receiver"]
                impl.append(f"MediaMTX pulls {src['label']} (path {v['path']}); IS-05 patch {dst['label']} ({dst['host']}) "
                            f"<- SRT read:{v['path']} on the hub (MPEG-TS)")
                d.wire_impl[wid] = impl
                continue
            if src["kind"] == "sap_tx":
                sap_sdp[src["sender"]] = {"sdp": src["sdp"], "label": src["label"], "stream": src["sender"][4:]}
                if dst["kind"] in ("pw", "meter"):
                    dh_ = dst.get("host")
                    if dh_ == hub:
                        i2 = from_hub_receiver(src["sender"], dst, wid, src["label"], "AES67/SAP", src.get("ports"))
                    else:
                        i2 = from_sat_receiver(dh_, src["sender"], dst, wid, src["label"], "AES67/SAP", src.get("ports"))
                    if i2 is None:
                        continue
                    impl += i2
                elif dst["kind"] == "nmos_rx" and dst["media"] == "audio":
                    if rx_sources.get(dst["receiver"], src["sender"]) != src["sender"]:
                        raise CompileError([f"wire {wid}: receiver {dst['label']} already has another source"])
                    if src.get("ports") is not None:
                        d.warnings.append(f"wire {wid}: {dst['label']} gets the whole stream {src['label']}, not only "
                                          f"those channels (an NMOS receiver plays a stream as it is)")
                    rx_sources[dst["receiver"]] = src["sender"]
                    set_patch(dst["receiver"], src["sender"], wid)
                    d.wire_patch[wid] = dst["receiver"]
                    impl.append(f"IS-05 patch {dst['label']} ({dst['host']}) <- {src['label']} (AES67/SAP, announced SDP)")
                else:
                    raise CompileError([f"wire {wid}: an AES67 stream goes into an audio input or an AES67 receiver"])
                d.wire_impl[wid] = impl
                continue
            if dst["kind"] == "voice" or src["kind"] == "voice":
                impl.append(voice_wire(wid, src, dst))
                d.wire_impl[wid] = impl
                continue
            if src["kind"] == "pw" and dst["kind"] in ("pw", "meter"):
                if sh == dh:                                                     # one host: a link or a tap
                    impl.append(local_link(sh, src, dst, wid))
                    if w.get("move"):
                        if sh != hub:
                            raise CompileError([f"wire {wid}: stream moves work on the hub only (satellites have no WirePlumber hook yet)"])
                        if not src.get("app"):
                            raise CompileError([f"wire {wid}: only an application's stream can be moved (the source is {src.get('device') or src['node']})"])
                        if dst.get("block") is None:
                            raise CompileError([f"wire {wid}: a stream can only be moved into a processing block"])
                        d.links[-1]["wp"] = True          # WirePlumber makes these links when it moves the stream
                        d.moves.append({"node": src["node"], "target": dst["node"], "wire": wid})
                        impl.append(f"move the stream: target.object = {dst['node']} (WirePlumber takes {src['node']} off its current output; it falls back when the block is gone)")
                elif sh != hub and dh == hub:                                    # satellite -> hub
                    sl, i1 = into_sender(sh, src, wid, hub)
                    if not sl:
                        continue
                    i2 = from_hub_receiver(sl.tx["id"], dst, wid, src.get("device") or src["node"], sh, sl.ports())
                    if i2 is None:
                        continue
                    impl += [i1] + i2
                elif sh == hub and dh != hub:                                    # hub -> satellite
                    sl, i1 = into_sender(hub, src, wid, dh)
                    if not sl:
                        continue
                    impl.append(i1)
                    i2 = from_sat_receiver(dh, sl.tx["id"], dst, wid, sl.tx.get("label"), hub, sl.ports())
                    if i2 is None:
                        continue
                    impl += i2
                else:                                                            # satellite A -> satellite B
                    sl, i1 = into_sender(sh, src, wid, dh)
                    if not sl:
                        continue
                    i2 = from_sat_receiver(dh, sl.tx["id"], dst, wid, src.get("device") or src["node"], sh, sl.ports())
                    if i2 is None:
                        continue
                    impl += [i1] + i2
            elif src["kind"] == "nmos_tx" and dst["kind"] in ("pw", "meter"):
                if src["media"] != "audio":
                    raise CompileError([f"wire {wid}: a video sender cannot feed an audio input"])
                if dh == hub:
                    i2 = from_hub_receiver(src["sender"], dst, wid, src["label"], src["host"])
                    if i2 is None:
                        continue
                    impl += i2
                elif dh == src["host"]:
                    raise CompileError([f"wire {wid}: {src['label']} is {dh}'s own sender; wire from its PipeWire ports instead (the block {dst.get('block') or dst['node']} runs there)"])
                else:
                    i2 = from_sat_receiver(dh, src["sender"], dst, wid, src["label"], src["host"])
                    if i2 is None:
                        continue
                    impl += i2
            elif src["kind"] == "pw" and dst["kind"] == "nmos_rx":
                if dst["media"] != "audio":
                    raise CompileError([f"wire {wid}: an audio output cannot feed a video receiver"])
                if sh == dst["host"]:
                    raise CompileError([f"wire {wid}: {dst['label']} is {sh}'s own receiver (a source there); wire into its PipeWire ports instead"])
                sl, i1 = into_sender(sh, src, wid, None, whole=True)
                if not sl:
                    continue
                impl.append(i1)
                sid = sl.tx["id"]
                slabel = sl.tx.get("label") if sh == hub else (src.get("device") or src["node"])
                if rx_sources.get(dst["receiver"], sid) != sid:
                    raise CompileError([f"wire {wid}: receiver {dst['label']} already has another source"])
                rx_sources[dst["receiver"]] = sid
                d.patches[dst["receiver"]] = sid
                d.wire_patch[wid] = dst["receiver"]
                impl.append(f"IS-05 patch {dst['label']} ({dst['host']}) <- {slabel}")
            elif src["kind"] == "nmos_tx" and dst["kind"] == "nmos_rx":
                if dst["receiver"] in rx_sources and rx_sources[dst["receiver"]] != src["sender"]:
                    raise CompileError([f"wire {wid}: receiver {dst['label']} already has another source"])
                mt, mts = src.get("media_type"), dst.get("media_types")
                if mt and mts and mt not in mts and mt != "application/mp2t":
                    raise CompileError([f"wire {wid}: {dst['label']} cannot decode {mt} (accepts {', '.join(mts)})"])
                rx_sources[dst["receiver"]] = src["sender"]
                d.patches[dst["receiver"]] = src["sender"]
                d.wire_patch[wid] = dst["receiver"]
                impl.append(f"IS-05 patch {dst['label']} ({dst['host']}) <- {src['label']} ({src['host']})")
            else:
                raise CompileError([f"wire {wid}: cannot connect {src['kind']} to {dst['kind']}"])
        except CompileError as e:
            d.errors += e.errors
            continue
        d.wire_impl[wid] = impl
    # sources on the same channels of a sender mix (pick_slot warned per wire); a sender also wired by hand counts
    def mixed(links, where):
        feeds = {}
        for l in links:
            n_ = l["dst"]["node"]
            if n_ and n_.startswith("aes67-tx-"):
                src_id = (l["src"]["node"], bool(l["src"].get("monitor")), str(l["src"].get("ports")))
                feeds.setdefault(n_, []).append((src_id, None if l["dst"].get("ports") is None else set(l["dst"]["ports"])))
        for n_, fs in sorted(feeds.items()):
            if any(a != b_ and (pa is None or pb is None or pa & pb) for i, (a, pa) in enumerate(fs) for b_, pb in fs[i + 1:]):
                d.warnings.append(f"sources share channels of AES67 sender {n_}{where}: whoever receives them hears the sum")
    mixed(d.links, " on the hub")
    for host, sh_ in d.shares.items():
        mixed(sh_["links"], f" on {host}")
    if d.voice and not d.voice["mic"]:
        d.warnings.append(f"the voice assistant ({d.voice['label']}) has nothing wired into 'listen': it cannot hear anything")
    if d.voice and not d.voice["reply"]:
        d.warnings.append(f"the voice assistant ({d.voice['label']}) has no 'reply' wire: its answers play on the default speaker without ducking")
    if d.errors:
        raise CompileError(d.errors)
    return d
