"""Per-receiver wire statistics, unprivileged: join the receiver's multicast group on its own UDP socket and count
what arrives (packets/s, lost packets by RTP sequence, L/R level of L24/L16). Independent of PipeWire's view, so
"patched" in the UI means packets are really reaching this host."""
import math, select, socket, struct, threading, time


class Watch:
    def __init__(self, group, port, iface_ip, source_ip=None):
        self.group, self.port, self.source_ip = group, port, source_ip
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.setsockopt(socket.SOL_IP, 49, 0)         # IP_MULTICAST_ALL off: only this group
        except OSError:
            pass
        s.bind((group, port))
        s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                     socket.inet_aton(group) + socket.inet_aton(iface_ip or "0.0.0.0"))
        s.setblocking(False)
        self.sock = s
        self.last_seq = None
        self.win = self._new()
        self.stats = {"pkts_per_s": 0, "lost": 0, "lost_total": 0, "rms_dbfs": [None, None], "source": None,
                      "since": time.time(), "last_packet": None}

    def _new(self):
        return {"t0": time.time(), "pkts": 0, "lost": 0, "sq": [0.0, 0.0], "n": 0, "src": None}

    def feed(self, data, addr):
        if self.source_ip and addr[0] != self.source_ip:
            return
        if len(data) < 12 or data[0] >> 6 != 2:
            return
        cc = data[0] & 0x0F
        seq = struct.unpack("!H", data[2:4])[0]
        ssrc = data[8:12]
        w = self.win
        if ssrc != getattr(self, "ssrc", None):      # a (re)started sender: new SSRC, new sequence; not loss
            self.ssrc, self.last_seq = ssrc, None
            self.stats["sender_restarts"] = self.stats.get("sender_restarts", -1) + 1
        if self.last_seq is not None:
            gap = (seq - self.last_seq) & 0xFFFF
            if 1 < gap < 30000:
                w["lost"] += gap - 1
        self.last_seq = seq
        w["pkts"] += 1
        w["src"] = addr[0]
        pl = data[12 + 4 * cc:]
        if w["n"] < 48000 and len(pl) >= 12:          # sample the level from the first second's worth
            for i in range(0, len(pl) - 5, 6):         # assume L24 stereo (what our senders send)
                for c in (0, 1):
                    b = pl[i + 3 * c: i + 3 * c + 3]
                    v = int.from_bytes(b, "big", signed=True) / 8388608.0
                    w["sq"][c] += v * v
                w["n"] += 1
        self.stats["last_packet"] = time.time()

    def roll(self):
        w, now = self.win, time.time()
        dt = max(now - w["t0"], 1e-3)
        lvl = [None, None]
        if w["n"]:
            for c in (0, 1):
                ms = w["sq"][c] / w["n"]
                lvl[c] = round(10 * math.log10(ms), 1) if ms > 1e-12 else None
        self.stats.update(pkts_per_s=round(w["pkts"] / dt), lost=w["lost"], rms_dbfs=lvl, source=w["src"])
        self.stats["lost_total"] += w["lost"]
        self.win = self._new()

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


class Monitor(threading.Thread):
    """watch(key, group, port, iface_ip) / unwatch(key); stats(key) -> dict, rolled every `interval` s."""

    def __init__(self, interval=2.0):
        super().__init__(daemon=True, name="rtpstat")
        self.interval, self.watches, self.lock = interval, {}, threading.Lock()

    def watch(self, key, group, port, iface_ip, source_ip=None):
        with self.lock:
            old = self.watches.get(key)
            if old and (old.group, old.port, old.source_ip) == (group, port, source_ip):
                return
            if old:
                old.close()
            try:
                self.watches[key] = Watch(group, port, iface_ip, source_ip)
            except OSError as e:
                self.watches.pop(key, None)
                print(f"rtpstat: cannot watch {group}:{port}: {e}", flush=True)

    def unwatch(self, key):
        with self.lock:
            w = self.watches.pop(key, None)
            if w:
                w.close()

    def stats(self, key):
        with self.lock:
            w = self.watches.get(key)
            return dict(w.stats, group=w.group) if w else None

    def run(self):
        last = time.time()
        while True:
            with self.lock:
                socks = {w.sock: w for w in self.watches.values()}
            if socks:
                r, _, _ = select.select(list(socks), [], [], 0.2)
                for s in r:
                    w = socks[s]
                    for _ in range(64):
                        try:
                            data, addr = s.recvfrom(2048)
                        except (BlockingIOError, OSError):
                            break
                        w.feed(data, addr)
            else:
                time.sleep(0.2)
            if time.time() - last >= self.interval:
                last = time.time()
                with self.lock:
                    for w in self.watches.values():
                        w.roll()
