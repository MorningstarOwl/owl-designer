"""SAP (RFC 2974) discovery of AES67 streams that are not owl-nodes: Dante devices in AES67 mode, Ravenna/Livewire+
gear, other PipeWire or ffmpeg senders. They announce their SDP on 239.255.255.255:9875; the hub's dsp engine listens,
keeps the playable audio streams it hears, and the inventory shows each as a network device the designer can wire into
any input (the compiler patches an AES67 receiver with the announced SDP, like the hub's RTSP camera relay).

owl-nodes announce their own senders too (the audio role's module-rtp-sap); those are already in the designer through
NMOS, so streams whose session name is an owl-node sender's (IS-04 tag urn:x-owl:sap-session) or whose origin is an
owl-node's address are left out by the inventory.

A stream is forgotten when its sender deletes it (SAP deletion) or stops announcing for EXPIRE_S (RFC 2974 suggests
ten announcement intervals; Dante and PipeWire announce every few seconds to every 30 s)."""
import hashlib, socket, struct, threading, time, zlib

from .. import sdp as SDP

SAP_GROUP, SAP_PORT = "239.255.255.255", 9875
EXPIRE_S = 300


def parse_packet(data):
    """-> (deletion: bool, origin ip, sdp text) or None for anything that is not a plain SAP announcement of an SDP."""
    if len(data) < 8:
        return None
    b0, auth_len = data[0], data[1]
    if b0 >> 5 != 1:                        # SAP version 1
        return None
    ipv6, deletion, encrypted, compressed = bool(b0 & 0x10), bool(b0 & 0x04), bool(b0 & 0x02), bool(b0 & 0x01)
    if encrypted:
        return None
    alen = 16 if ipv6 else 4
    pos = 4 + alen + 4 * auth_len
    if len(data) < pos:
        return None
    origin = socket.inet_ntop(socket.AF_INET6 if ipv6 else socket.AF_INET, data[4:4 + alen])
    body = data[pos:]
    if compressed:
        try:
            body = zlib.decompress(body)
        except zlib.error:
            return None
    if not body.startswith(b"v=0"):          # an optional payload type ("application/sdp\0") comes first
        nul = body.find(b"\0")
        if nul < 0 or body[:nul].strip().lower() != b"application/sdp":
            return None
        body = body[nul + 1:]
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        text = body.decode("latin-1")
    return deletion, origin, text


def stream_id(sdp_text, origin):
    """Stable id of an announced session: its SDP origin (o= username, session id, address), as RFC 4566 defines a
    session's identity (the version field changes on every edit and is left out)."""
    o = next((l[2:].split() for l in sdp_text.splitlines() if l.startswith("o=")), None)
    key = " ".join([o[0], o[1], o[5]]) if o and len(o) >= 6 else origin + "|" + sdp_text
    return hashlib.sha1(key.encode()).hexdigest()[:12]


class Directory:
    """The announced streams: id -> {id, name, origin, sdp, format, channels, group, port, seen, first_seen, error}."""

    def __init__(self, expire_s=EXPIRE_S):
        self.expire_s = expire_s
        self.lock = threading.Lock()
        self.streams = {}

    def feed(self, data, now=None):
        p = parse_packet(data)
        if not p:
            return None
        deletion, origin, text = p
        sid = stream_id(text, origin)
        now = time.time() if now is None else now
        with self.lock:
            if deletion:
                self.streams.pop(sid, None)
                return sid
            entry = self.streams.get(sid)
            if entry and entry["sdp"] == text:
                entry["seen"] = now
                return sid
            try:
                f = SDP.parse(text)
                err = None
            except SDP.SDPError as e:
                f, err = {}, str(e)
            self.streams[sid] = {"id": sid, "name": f.get("session") or next(
                                     (l[2:].strip() for l in text.splitlines() if l.startswith("s=")), sid),
                                 "origin": f.get("origin_ip") or origin, "sdp": text, "format": f or None,
                                 "channels": f.get("channels"), "group": f.get("group"), "port": f.get("port"),
                                 "seen": now, "first_seen": (entry or {}).get("first_seen", now), "error": err}
        return sid

    def expire(self, now=None):
        now = time.time() if now is None else now
        with self.lock:
            for sid in [k for k, v in self.streams.items() if now - v["seen"] > self.expire_s]:
                del self.streams[sid]

    def list(self):
        self.expire()
        with self.lock:
            return [dict(v) for v in sorted(self.streams.values(), key=lambda v: (v["name"] or "", v["id"]))]

    def get(self, sid):
        with self.lock:
            v = self.streams.get(sid)
            return dict(v) if v else None


class Listener(threading.Thread):
    """Joins the SAP group on the media interface and feeds a Directory. Shares port 9875 with PipeWire's own SAP
    module on the same host (SO_REUSEADDR: every socket bound to a multicast port gets each datagram)."""

    def __init__(self, iface_ip, directory=None, log=print):
        super().__init__(daemon=True, name="sap")
        self.iface_ip, self.dir, self.log = iface_ip, directory or Directory(), log
        self.error, self.packets, self._stop = None, 0, threading.Event()

    def _socket(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.setsockopt(socket.SOL_IP, 49, 0)           # IP_MULTICAST_ALL off: only the group we joined
        except OSError:
            pass
        s.bind((SAP_GROUP, SAP_PORT))
        s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                     socket.inet_aton(SAP_GROUP) + socket.inet_aton(self.iface_ip or "0.0.0.0"))
        s.settimeout(1.0)
        return s

    def run(self):
        while not self._stop.is_set():
            try:
                s = self._socket()
                self.error = None
            except OSError as e:
                if self.error != str(e):
                    self.log(f"SAP discovery: cannot listen on {SAP_GROUP}:{SAP_PORT} ({e}); retrying")
                self.error = str(e)
                self._stop.wait(10)
                continue
            try:
                while not self._stop.is_set():
                    try:
                        data, _ = s.recvfrom(65536)
                    except socket.timeout:
                        continue
                    self.packets += 1
                    self.dir.feed(data)
            except OSError as e:
                self.error = str(e)
                self.log(f"SAP discovery: {e}; reopening")
            finally:
                s.close()

    def stop(self):
        self._stop.set()

    def status(self):
        return {"listening": self.is_alive() and not self.error, "error": self.error, "packets": self.packets,
                "streams": len(self.dir.streams)}


def announce_packet(sdp_text, origin_ip, deletion=False, msg_id=1):
    """A SAP packet (used by the tests and handy for trying discovery by hand)."""
    hdr = struct.pack("!BBH", 0x20 | (0x04 if deletion else 0), 0, msg_id) + socket.inet_aton(origin_ip)
    return hdr + b"application/sdp\0" + sdp_text.encode()
