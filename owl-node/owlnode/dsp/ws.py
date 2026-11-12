"""A small RFC 6455 WebSocket server side for the dsp role's /api/dsp/ws (no dependency; text frames only).

Server -> client: {"type":"hello", ...controls listing...} once, then every Bus event: {"type":"control", key, name,
value, source}, {"type":"meters", values:{key: level}} at 4 Hz while anyone listens, {"type":"design", ...},
{"type":"preset", ...}, {"type":"event", msg}. Client -> server: {"type":"set", "key"|"name", "value", "fade_s"},
{"type":"recall", "preset", "fade_s"}, {"type":"ping"} (answered with {"type":"pong"}). The controller tunnels this
endpoint from :8361 so the designer page stays same-origin."""
import base64, hashlib, json, queue, socket, struct, threading, time

GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def accept_key(key):
    return base64.b64encode(hashlib.sha1(key.encode() + GUID).digest()).decode()


def frame(text, opcode=1):
    data = text.encode() if isinstance(text, str) else text
    n = len(data)
    if n < 126:
        hdr = bytes([0x80 | opcode, n])
    elif n < 65536:
        hdr = bytes([0x80 | opcode, 126]) + struct.pack(">H", n)
    else:
        hdr = bytes([0x80 | opcode, 127]) + struct.pack(">Q", n)
    return hdr + data


def read_frame(rfile):
    """-> (opcode, payload bytes) or None at EOF."""
    h = rfile.read(2)
    if len(h) < 2:
        return None
    fin_op, ln = h[0], h[1]
    masked, n = ln & 0x80, ln & 0x7F
    if n == 126:
        n = struct.unpack(">H", rfile.read(2))[0]
    elif n == 127:
        n = struct.unpack(">Q", rfile.read(8))[0]
    mask = rfile.read(4) if masked else None
    data = rfile.read(n) if n else b""
    if mask:
        data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
    return fin_op & 0x0F, data


class Session:
    """One connected browser/script. Runs in the HTTP handler's thread."""

    def __init__(self, handler, controls, log):
        self.h, self.ctl, self.log = handler, controls, log
        self.sock = handler.connection
        self.wlock = threading.Lock()
        self.alive = True

    def send(self, obj):
        try:
            with self.wlock:
                self.sock.sendall(frame(json.dumps(obj)))
        except OSError:
            self.alive = False

    def handshake(self):
        key = self.h.headers.get("Sec-WebSocket-Key")
        if not key or "websocket" not in (self.h.headers.get("Upgrade") or "").lower():
            return False
        resp = ("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept_key(key)}\r\n\r\n")
        self.sock.sendall(resp.encode())
        self.h.close_connection = True
        return True

    def _reader(self):
        try:
            while self.alive:
                fr = read_frame(self.h.rfile)
                if fr is None:
                    break
                op, data = fr
                if op == 8:
                    break
                if op == 9:
                    with self.wlock:
                        self.sock.sendall(frame(data, 10))
                    continue
                if op != 1:
                    continue
                try:
                    self.handle(json.loads(data.decode()))
                except Exception as e:
                    self.send({"type": "error", "error": f"{type(e).__name__}: {e}"})
        except OSError:
            pass
        finally:
            self.alive = False

    def handle(self, m):
        t = m.get("type")
        if t == "ping":
            self.send({"type": "pong", "ts": time.time()})
        elif t == "set":
            r = self.ctl.set(m.get("key") or m.get("name"), m.get("value"), fade_s=float(m.get("fade_s") or 0), source=m.get("source") or "ws")
            self.send(dict(r, type="ack", id=m.get("id")))
        elif t == "recall":
            r = self.ctl.recall(m.get("preset"), fade_s=m.get("fade_s"), source="ws")
            self.send(dict(r, type="ack", id=m.get("id")))
        elif t == "get":
            self.send(dict(self.ctl.get(m.get("key") or m.get("name")), type="value", id=m.get("id")))
        elif t == "list":
            self.send(dict(self.ctl.listing(), type="hello"))
        else:
            self.send({"type": "error", "error": f"unknown message type {t!r}", "id": m.get("id")})

    def run(self):
        if not self.handshake():
            return False
        q = self.ctl.bus.subscribe()
        self.sock.settimeout(None)
        threading.Thread(target=self._reader, daemon=True).start()
        self.send(self.ctl.snapshot_event())
        last_meters = 0
        try:
            while self.alive:
                try:
                    ev = q.get(timeout=0.25)
                    self.send(ev)
                except queue.Empty:
                    pass
                if time.time() - last_meters >= 0.25:
                    last_meters = time.time()
                    mv = self.ctl.meter_values()
                    if mv:
                        self.send({"type": "meters", "values": {k: v.get("level") for k, v in mv.items()},
                                   "detail": {k: {f: v.get(f) for f in ("rms", "peak", "clip", "signal", "running", "stale",
                                                                         "age_s", "peak_level", "channels", "level")}
                                              for k, v in mv.items()}})
        finally:
            self.ctl.bus.unsubscribe(q)
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        return True
