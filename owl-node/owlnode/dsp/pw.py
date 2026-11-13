"""The hub's PipeWire graph through the command-line tools (pw-dump, pw-link, pw-cli): nodes, ports and links as
plain dicts, and the three operations the deployer needs (link, unlink, set a filter control)."""
import json, subprocess, threading, time
from collections import deque


class Graph:
    def __init__(self, objs):
        self.time = time.time()
        self.nodes, self.ports, self.links = {}, {}, []       # id -> node, node id -> [port], [link]
        self.by_name = {}
        for o in objs:
            if o.get("type") == "PipeWire:Interface:Node":
                p = (o.get("info") or {}).get("props") or {}
                n = {"id": o["id"], "name": p.get("node.name"), "props": p, "class": p.get("media.class"),
                     "state": (o.get("info") or {}).get("state")}
                self.nodes[o["id"]] = n
                if n["name"]:
                    self.by_name.setdefault(n["name"], n)
        for o in objs:
            if o.get("type") == "PipeWire:Interface:Port":
                p = (o.get("info") or {}).get("props") or {}
                nid = p.get("node.id")
                self.ports.setdefault(nid, []).append({
                    "id": o["id"], "node_id": nid, "name": p.get("port.name"), "dir": p.get("port.direction"),
                    "idx": int(p.get("port.id", 0)), "monitor": bool(p.get("port.monitor")),
                    "channel": p.get("audio.channel")})
        pid = {p["id"]: p for ps in self.ports.values() for p in ps}
        for o in objs:
            if o.get("type") == "PipeWire:Interface:Link":
                i = o.get("info") or {}
                op, ip = pid.get(i.get("output-port-id")), pid.get(i.get("input-port-id"))
                on, inn = self.nodes.get(i.get("output-node-id")), self.nodes.get(i.get("input-node-id"))
                self.links.append({"id": o["id"], "out_node": on["name"] if on else None, "out_port": op["name"] if op else None,
                                   "out_port_id": i.get("output-port-id"), "in_node": inn["name"] if inn else None,
                                   "in_port": ip["name"] if ip else None, "in_port_id": i.get("input-port-id"),
                                   "state": i.get("state")})

    def node(self, name):
        return self.by_name.get(name)

    def node_ports(self, name, direction, monitor=False, idx=None):
        """A node's ports in channel order: inputs, main outputs or monitor outputs; `idx` picks channel indexes."""
        n = self.node(name)
        if not n:
            return []
        want = "in" if direction == "in" else "out"
        ps = [p for p in self.ports.get(n["id"], []) if p["dir"] == want and (want == "in" or p["monitor"] == monitor)]
        ps.sort(key=lambda p: p["idx"])
        if idx is not None:
            ps = [p for k, p in enumerate(ps) if k in idx]
        return ps

    def link_set(self):
        return {(l["out_port_id"], l["in_port_id"]): l for l in self.links}


def dump(timeout=10):
    out = subprocess.run(["pw-dump", "--no-colors"], capture_output=True, timeout=timeout, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"pw-dump: {out.stderr.strip()[:200]}")
    return Graph(json.loads(out.stdout or "[]"))


def link(out_port_id, in_port_id):
    r = subprocess.run(["pw-link", str(out_port_id), str(in_port_id)], capture_output=True, text=True, timeout=5)
    return r.returncode == 0 or "exists" in (r.stderr or ""), (r.stderr or "").strip()


def unlink(link_id):
    r = subprocess.run(["pw-link", "-d", str(link_id)], capture_output=True, text=True, timeout=5)
    return r.returncode == 0, (r.stderr or "").strip()


def set_control(node_id, control, value):
    """A filter-chain control (`pw-cli set-param <node> Props { params = [ name value ] }`)."""
    v = f"{value:.6g}" if isinstance(value, float) else str(value)
    r = subprocess.run(["pw-cli", "set-param", str(node_id), "Props", f'{{ params = [ "{control}" {v} ] }}'],
                       capture_output=True, text=True, timeout=5)
    return r.returncode == 0, (r.stderr or r.stdout or "").strip()


def set_target(node_id, target):
    """Move a stream: `target.object` metadata (what pavucontrol sets). WirePlumber re-links the stream to the named
    node and remembers the choice for that stream (stream restore), so when the target later disappears the stream
    falls back to its own target.object property / the default sink, and when it reappears it goes back."""
    # a bare node name or an object serial; a JSON-quoted name is ignored by WirePlumber 0.5 (tested 2026-11-09)
    r = subprocess.run(["pw-metadata", str(node_id), "target.object", str(target), "Spa:String:JSON"],
                       capture_output=True, text=True, timeout=5)
    return r.returncode == 0, (r.stderr or r.stdout or "").strip()


def clear_target(node_id):
    r = subprocess.run(["pw-metadata", "-d", str(node_id), "target.object"], capture_output=True, text=True, timeout=5)
    return r.returncode == 0, (r.stderr or r.stdout or "").strip()


def get_target(node_id):
    """The stream's current target.object metadata (None when unset)."""
    r = subprocess.run(["pw-metadata", str(node_id), "target.object"], capture_output=True, text=True, timeout=5)
    for line in r.stdout.splitlines():
        if "target.object" in line and "value:" in line:
            v = line.split("value:", 1)[1].split("type:")[0].strip().strip("'").strip('"')
            return v
    return None


def pair_ports(src_ports, dst_ports):
    """Channel mapping for a wire: equal counts pair up; one source feeds every destination channel; several sources
    into one destination channel mix (PipeWire sums links into a port)."""
    if not src_ports or not dst_ports:
        return []
    if len(src_ports) == len(dst_ports):
        return list(zip(src_ports, dst_ports))
    if len(src_ports) == 1:
        return [(src_ports[0], d) for d in dst_ports]
    if len(dst_ports) == 1:
        return [(s, dst_ports[0]) for s in src_ports]
    return list(zip(src_ports, dst_ports))


def _fmt(v):
    return f"{v:.6g}" if isinstance(v, float) else str(v)


class PwCtl:
    """Live filter-chain controls through ONE long-lived `pw-cli` fed `set-param` lines on stdin, instead of a
    `pw-cli` process per control (~4 ms each, plus a full pw-dump to find the node: ~10 ms on an idle graph, far more on
    a busy hub). A fader move or a fade step is a line written to a pipe. pw-cli answers each command by printing the
    node's Props; a reader thread drains that and keeps the errors it reports (asynchronous, not tied to one command).
    If pw-cli cannot run, every set falls back to a one-shot `pw-cli set-param`."""
    NOISE = ("unsupported type",)                 # what pw-cli prints at start-up for object types it does not know

    def __init__(self, argv=("pw-cli",)):
        self.argv, self.p, self.lock = list(argv), None, threading.Lock()
        self.errors, self.error_seq, self.sent, self.restarts, self.fallback = deque(maxlen=20), 0, 0, 0, False

    def _start(self):
        self.p = subprocess.Popen(self.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  text=True, bufsize=1, start_new_session=True)
        threading.Thread(target=self._drain, args=(self.p,), daemon=True, name="pw-cli").start()

    def _drain(self, p):
        try:
            for line in p.stdout:
                if "rror" in line and not any(n in line for n in self.NOISE):
                    self.errors.append((time.time(), line.strip()[:200]))
                    self.error_seq += 1
        except (OSError, ValueError):
            pass

    def running(self):
        return self.p is not None and self.p.poll() is None

    def set(self, node_id, pairs):
        """pairs: [(control name, value)] of one node, sent as one set-param. -> True when sent."""
        if not pairs:
            return True
        body = " ".join(f'"{c}" {_fmt(v)}' for c, v in pairs)
        line = f"set-param {int(node_id)} Props {{ params = [ {body} ] }}\n"
        with self.lock:
            for attempt in (0, 1):
                if not self.fallback and not self.running():
                    try:
                        if self.p is not None:
                            self.restarts += 1
                        self._start()
                    except OSError:
                        self.fallback = True
                if self.fallback:
                    break
                try:
                    self.p.stdin.write(line)
                    self.p.stdin.flush()
                    self.sent += 1
                    return True
                except (OSError, ValueError):
                    self.p = None                  # it died between the check and the write: start a new one once
        r = subprocess.run(["pw-cli", "set-param", str(int(node_id)), "Props", f"{{ params = [ {body} ] }}"],
                           capture_output=True, text=True, timeout=5)
        self.sent += r.returncode == 0
        return r.returncode == 0

    def status(self):
        return {"running": self.running(), "sent": self.sent, "restarts": self.restarts, "fallback": self.fallback,
                "errors": [e for _, e in list(self.errors)[-3:]]}

    def stop(self):
        with self.lock:
            if self.running():
                try:
                    self.p.stdin.close()
                except OSError:
                    pass
                self.p.terminate()
            self.p = None


class Debounced:
    """Run fn at most once per `delay` s: the first call of a burst schedules it `delay` later, the calls until then
    ride along (a fader drag writes its file once a second, not per step); flush() runs a pending call now."""

    def __init__(self, fn, delay=1.0):
        self.fn, self.delay, self.timer, self.lock = fn, delay, None, threading.Lock()

    def __call__(self):
        with self.lock:
            if self.timer is None:
                self.timer = threading.Timer(self.delay, self._run)
                self.timer.daemon = True
                self.timer.start()

    def _run(self):
        with self.lock:
            self.timer = None
        self.fn()

    def flush(self):
        with self.lock:
            t, self.timer = self.timer, None
        if t is not None:
            t.cancel()
            self.fn()
