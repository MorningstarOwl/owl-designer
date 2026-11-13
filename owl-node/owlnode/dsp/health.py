"""What needs attention, from what the hub already knows (each owl-node's /x-owl/v1/status, the deployer, the
satellites): one list of issues for the designer, Home Assistant (a "problem" binary sensor whose attributes list
them) and the API; and the same facts as Prometheus metrics (GET /metrics on the hub's dsp role).

Issues:
  ptp       a node with a PTP hardware clock is not locked (no data, stale, faulty, or more than PTP_TOLERANCE_NS off)
  loss      an active AES67 receiver lost packets in the last LOSS_WINDOW_S
  silent    an active AES67 receiver gets no packets at all
  restart   a block process restarted in the last RESTART_WINDOW_S
  node      a node does not answer (its status, or its dsp role)
  design    the design does not apply / the registry does not answer
"""
import time

PTP_TOLERANCE_NS = 10_000
LOSS_WINDOW_S = 60
RESTART_WINDOW_S = 600


def _ptp_issue(name, st):
    clock = st.get("clock") or {}
    if clock.get("ref_type") != "ptp":
        return None                                   # a system-clock (test) node: nothing to lock to
    p = st.get("ptp") or {}
    state = p.get("port_state")
    if not p or p.get("error"):
        return f"PTP on {name}: no data ({p.get('error') or 'owl-node-ptpstat not running?'})"
    if p.get("stale"):
        return f"PTP on {name}: status is stale"
    if state == "MASTER":
        return None
    if state != "SLAVE":
        return f"PTP on {name}: {state or 'unknown'} (not following a grandmaster)"
    off = p.get("offset_ns")
    if off is None or abs(off) > PTP_TOLERANCE_NS:
        return f"PTP on {name}: {off} ns off the grandmaster"
    return None


class Health:
    def __init__(self, dep, inv):
        self.dep, self.inv = dep, inv
        self.loss = {}                 # receiver id -> [(time, lost_total)]
        self.restarts = {}             # block id -> [(time, total_restarts)]

    def _nodes(self):
        nmos = self.inv.nmos or {}
        labels = {n["id"]: n.get("label") or n["id"][:8] for n in nmos.get("nodes") or []}
        for nid, st in (nmos.get("status") or {}).items():
            yield nid, (st or {}).get("label") or labels.get(nid) or nid[:8], st

    def check(self, now=None):
        now = time.time() if now is None else now
        issues = []

        def add(kind, where, text):
            issues.append({"kind": kind, "where": where, "text": text})
        if getattr(self.dep, "design_error", None):
            add("design", "hub", f"the design does not apply: {self.dep.design_error}")
        if getattr(self.inv, "error", None):
            add("design", "hub", f"registry / graph: {self.inv.error}")
        for nid, name, st in self._nodes():
            if st is None:
                continue                              # not an owl-node (no status service)
            if st.get("error"):
                add("node", name, f"{name} does not answer ({st['error']})")
                continue
            msg = _ptp_issue(name, st)
            if msg:
                add("ptp", name, msg)
            for rid, r in (st.get("receivers") or {}).items():
                if not r.get("master_enable"):
                    self.loss.pop(rid, None)
                    continue
                w = r.get("wire") or {}
                label = f"{name} {r.get('key') or rid[:8]}"
                if w.get("pkts_per_s") == 0:
                    add("silent", name, f"{label}: patched but no packets arrive")
                hist = self.loss.setdefault(rid, [])
                if w.get("lost_total") is not None:
                    hist.append((now, w["lost_total"]))
                hist[:] = [x for x in hist if now - x[0] <= LOSS_WINDOW_S]
                if len(hist) >= 2 and hist[-1][1] > hist[0][1]:
                    add("loss", name, f"{label}: {hist[-1][1] - hist[0][1]} packet(s) lost in the last {LOSS_WINDOW_S} s")
        for bid, p in list(getattr(self.dep, "procs", {}).items()):
            hist = self.restarts.setdefault(bid, [])
            hist.append((now, getattr(p, "total_restarts", 0)))
            hist[:] = [x for x in hist if now - x[0] <= RESTART_WINDOW_S]
            if hist[-1][1] > hist[0][1]:
                add("restart", "hub", f"block {bid} restarted {hist[-1][1] - hist[0][1]} time(s) in the last {RESTART_WINDOW_S // 60} min")
        for host, c in list((getattr(self.inv, "sats", None) or {}).items()):
            if not getattr(c, "ok", True):
                add("node", host, f"{host}'s dsp role does not answer")
        return {"ok": not issues, "issues": issues, "time": now}


def _esc(v):
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def metrics(dep, inv, taps=None, health=None, version=""):
    """Prometheus text exposition (version 0.0.4) of the hub's view of the system."""
    out, seen = [], set()

    def m(name, value, help_, kind="gauge", **labels):
        if value is None:
            return
        if name not in seen:
            seen.add(name)
            out.append(f"# HELP {name} {help_}")
            out.append(f"# TYPE {name} {kind}")
        lab = ",".join(f'{k}="{_esc(v)}"' for k, v in labels.items())
        out.append(f"{name}{{{lab}}} {float(value):g}" if lab else f"{name} {float(value):g}")
    m("owl_info", 1, "owl-node version of the hub", version=version)
    m("owl_design_ok", 0 if getattr(dep, "design_error", None) else 1 if getattr(dep, "design", None) else 0, "1 when a design is deployed and applies")
    m("owl_deploy_repairs_total", getattr(dep, "repairs", 0), "links the reconcile loop re-made", "counter")
    nmos = inv.nmos or {}
    labels = {n["id"]: n.get("label") or n["id"] for n in nmos.get("nodes") or []}
    for nid, st in (nmos.get("status") or {}).items():
        if st is None:
            continue
        node = (st or {}).get("name") or labels.get(nid, nid)
        m("owl_node_up", 0 if st.get("error") else 1, "1 when the node's status answers", node=node)
        if st.get("error"):
            continue
        m("owl_node_registered", 1 if st.get("registered") else 0, "1 when registered with the NMOS registry", node=node)
        p = st.get("ptp") or {}
        if (st.get("clock") or {}).get("ref_type") == "ptp":
            m("owl_ptp_locked", 1 if _ptp_issue(node, st) is None else 0, "1 when PTP is locked (or the node is the grandmaster)", node=node)
            m("owl_ptp_offset_ns", p.get("offset_ns"), "PTP offset from the grandmaster", node=node)
        for rid, r in (st.get("receivers") or {}).items():
            w = r.get("wire") or {}
            lab = dict(node=node, receiver=r.get("key") or rid)
            m("owl_rx_active", 1 if r.get("master_enable") else 0, "1 when the AES67 receiver is patched", **lab)
            m("owl_rx_packets_per_second", w.get("pkts_per_s"), "RTP packets per second arriving", **lab)
            m("owl_rx_lost_packets_total", w.get("lost_total"), "RTP packets lost (sequence gaps)", "counter", **lab)
            m("owl_rx_latency_ms", r.get("latency_ms"), "receiver playout latency", **lab)
    for bid, p in list(getattr(dep, "procs", {}).items()):
        m("owl_block_running", 1 if p.running() else 0, "1 when the block process runs", block=bid)
        m("owl_block_restarts_total", getattr(p, "total_restarts", 0), "block process restarts", "counter", block=bid)
    for host, c in list((getattr(inv, "sats", None) or {}).items()):
        m("owl_satellite_up", 1 if getattr(c, "ok", False) else 0, "1 when the satellite's dsp role answers", host=host)
    ops = getattr(dep, "ops", None)
    if ops is not None and getattr(ops, "pwctl", None) is not None:
        st = ops.pwctl.status()
        m("owl_live_controls_sent_total", st["sent"], "live control changes sent to PipeWire", "counter")
        m("owl_live_control_errors_total", ops.pwctl.error_seq, "errors pw-cli reported", "counter")
    for name, s in ((taps.stats() if taps is not None else {}) or {}).items():
        lv = [v for v in (s.get("rms_dbfs") or []) if v is not None]
        m("owl_meter_rms_dbfs", max(lv) if lv else -120, "loudest channel of a meter tap (RMS, last second)", tap=name)
    if health is not None:
        h = health.check()
        m("owl_health_ok", 1 if h["ok"] else 0, "1 when nothing needs attention")
        kinds = {}
        for i in h["issues"]:
            kinds[i["kind"]] = kinds.get(i["kind"], 0) + 1
        for k in ("ptp", "loss", "silent", "restart", "node", "design"):
            m("owl_health_issues", kinds.get(k, 0), "issues by kind", kind=k)
    return "\n".join(out) + "\n"
