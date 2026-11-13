"""Voice through the AEC, as a Wyoming satellite Home Assistant's Assist connects to: push-to-talk, and an optional wake
word.

Push-to-talk (the default) keeps the microphone closed except while someone talks. The microphone (the AEC block's clean
source) is opened only inside a PTT window (`pw-record`, 16 kHz mono) and closed the moment the utterance ends (HA's VAD says
`voice-stopped`), the hold control is released, or `max_listen_s` passes; nothing else ever reads the mic through this
module, and the state is visible (voice.mic_live: HA binary sensor, Panel, designer). The reply (Piper TTS audio HA
streams back) is played into the ducker's sidechain sink (`pw-play`), so it ducks the music like an announcement.

Wyoming (https://github.com/OHF-Voice/wyoming): one JSON header line {type, data_length?, payload_length?}, then
data_length bytes of JSON, then payload_length raw bytes. HA (2026.9, components/wyoming/assist_satellite.py) connects
to us, sends run-satellite, then describe in a loop; we answer info (a `satellite` section). On PTT we send
run-pipeline {start_stage: asr, end_stage: tts} + audio-start + audio-chunk ... + audio-stop; HA sends transcribe,
voice-started/voice-stopped (its VAD), transcript {text}, synthesize {text}, then audio-start/chunk/stop with the TTS
WAV (22.05 kHz, 16-bit mono) and expects `played` when we are done; ping/pong keep the link alive; error ends a run.
HA picks the Assist pipeline per satellite device (the device's "Assistant" select).

Named controls (controls.py, kind "voice"): voice.ptt (trigger), voice.ptt_hold (bool: held while on), read-only
voice.state (idle | listening | processing | responding), voice.mic_live, voice.last_text, voice.last_reply, voice.ha.

Wake-word mode (voice.wake_word, off unless switched on): a wake word chosen in Home Assistant. While on, the clean
mic streams to HA continuously with run-pipeline {start_stage: wake} and HA's
own wake word stage (the pipeline's openWakeWord entity; the word is the pipeline's "Wake word" setting in HA) detects;
the rest of the run is as PTT. The switch is PERSISTED (/var/lib/owl-node/dsp-voice.json): a satellite that was
listening comes up listening after a restart or a reboot and after HA reconnects. The time limit is optional:
OWL_VOICE_WAKE_MAX_MIN (default 0 = unbounded). MIC LIVE is shown the whole time, PTT
stays available when the mode is off, on/off is logged.

Audible cue. A wake-word detection plays two short ascending blips (90 ms + 110 ms, ~660 Hz then a raised fifth) into
the ducker's sidechain, i.e. the same sink as a reply, so it ducks the music and comes out of the speakers the reply
comes out of; it is 0.2 s and fires the moment HA sends `detection`, before the user has to speak. OWL_VOICE_CUE=0
silences it, OWL_VOICE_CUE_VOLUME (0.45) and OWL_VOICE_CUE_HZ (660) tune it. PTT is deliberately silent.
"""
import json, os, socket, struct, subprocess, threading, time

from .. import config
from ..config import atomic_write, read_json

MIC_RATE, MIC_CH, MIC_WIDTH = 16000, 1, 2
CHUNK = 1024 * MIC_CH * MIC_WIDTH          # 64 ms of 16 kHz mono
MAX_LISTEN_S = 12.0
RUN_TIMEOUT_S = 45.0
SEND_TIMEOUT_S = 10             # a peer that stops reading gets dropped after this, not held forever
WAKE_MAX_MIN = 0.0            # minutes; 0 = no limit; OWL_VOICE_WAKE_MAX_MIN overrides
# Last-resort re-arm interval, NOT a heartbeat. HA runs the wake stage with restart_on_end=True and
# re-arms itself; if we ALSO send run-pipeline we end up with two concurrent wake stages and HA logs
# 'Pipeline finished' + two 'Receiving audio from satellite'. openWakeWord then has its audio split
# between them and detects nothing at all, while the satellite still reports state=wake/mic_live.
# Measured: a 60 s re-arm produced exactly this. Keep this long enough that it
# only rescues a genuinely dead pipeline.
REARM_IDLE_S = float(os.environ.get("OWL_VOICE_REARM_IDLE_S") or 3600)
# After a NORMAL reply HA restarts the wake stage itself (restart_on_end=True). Sending run-pipeline as well
# leaves TWO concurrent wake stages that split the 16 kHz frames: openWakeWord then stops firing entirely
# (measured: first detection OK, every later one silent). Announcements/cancellations are the only
# case where HA has no run of its own. Set OWL_VOICE_REARM_AFTER_REPLY=1 to force the old behaviour.
REARM_AFTER_REPLY = (os.environ.get("OWL_VOICE_REARM_AFTER_REPLY") or "").lower() in ("1", "true", "yes")
# Audible cue on wake-word detection (otherwise the satellite is silent from the
# wake word until the reply). Two short ascending blips played into the ducker's sidechain, exactly like a reply, so
# they duck the music and come out of the same speakers. OWL_VOICE_CUE=0 turns it off, OWL_VOICE_CUE_VOLUME / _HZ tune
# it. PTT is left silent: only the wake word chimes.
CUE_ON = (os.environ.get("OWL_VOICE_CUE") or "1").lower() not in ("0", "false", "no", "off")
CUE_VOLUME = float(os.environ.get("OWL_VOICE_CUE_VOLUME") or 0.45)
CUE_HZ = float(os.environ.get("OWL_VOICE_CUE_HZ") or 660.0)
CUE_RATE = 48000
STATE_FILE = None             # /var/lib/owl-node/dsp-voice.json (set at import from config)
VERSION = "1"


def log(*a):
    print(time.strftime("%H:%M:%S"), "voice:", *a, flush=True)


def cue_pcm(rate=CUE_RATE, hz=CUE_HZ):
    """The wake cue as raw s16 mono PCM: two blips (hz, then a raised fifth), each faded in/out so nothing clicks."""
    import math
    out = bytearray()
    for f, ms in ((hz, 90.0), (hz * 1.5, 110.0)):
        n, fade = int(rate * ms / 1000.0), max(1, int(rate * 0.006))
        for i in range(n):
            env = min(1.0, i / fade, (n - 1 - i) / fade)
            # a little second harmonic: small speakers reproduce 660 Hz weakly on their own
            s = 0.75 * math.sin(2 * math.pi * f * i / rate) + 0.25 * math.sin(4 * math.pi * f * i / rate)
            out += struct.pack("<h", int(32767 * 0.6 * env * s))
        out += b"\x00\x00" * int(rate * 0.02)      # 20 ms between the blips
    return bytes(out)


def encode_event(typ, data=None, payload=b""):
    """One Wyoming frame as bytes, so it can go out through sendall() under a send timeout."""
    hdr = {"type": typ, "version": VERSION}
    db = json.dumps(data).encode() if data else b""
    if db:
        hdr["data_length"] = len(db)
    if payload:
        hdr["payload_length"] = len(payload)
    return json.dumps(hdr).encode() + b"\n" + db + payload


def write_event(f, typ, data=None, payload=b""):
    f.write(encode_event(typ, data, payload))
    f.flush()


def read_event(f):
    """-> (type, data, payload) or None at EOF."""
    line = f.readline()
    if not line:
        return None
    try:
        hdr = json.loads(line)
    except ValueError:
        return None
    data = dict(hdr.get("data") or {})
    n = int(hdr.get("data_length") or 0)
    if n:
        b = f.read(n)
        if len(b) < n:
            return None
        data.update(json.loads(b))
    m = int(hdr.get("payload_length") or 0)
    payload = f.read(m) if m else b""
    if m and len(payload) < m:
        return None
    return hdr.get("type"), data, payload


class Voice(threading.Thread):
    def __init__(self, cfg, dep, bus, port=None):
        super().__init__(daemon=True, name="voice")
        self.cfg, self.dep, self.bus = cfg, dep, bus
        self.port = int(port or os.environ.get("OWL_VOICE_PORT") or 10700)
        self.name = f"Owl DSP {cfg.label}"
        self.mic_override = os.environ.get("OWL_VOICE_MIC") or None
        self.reply_override = os.environ.get("OWL_VOICE_REPLY") or None
        self.max_listen_s = float(os.environ.get("OWL_VOICE_MAX_LISTEN_S") or MAX_LISTEN_S)
        self.state, self.mic_live = "idle", False
        self.last_text, self.last_reply, self.last_error = "", "", ""
        self.ha_connected, self.ha_enabled, self.ha_peer = False, False, None
        self.runs, self.run_started, self.timings = 0, None, {}
        self.hold = False
        self.wake, self.wake_until, self.wake_detections = False, 0, 0
        self.wake_max_min = float(os.environ.get("OWL_VOICE_WAKE_MAX_MIN") or WAKE_MAX_MIN)
        self.state_file = os.path.join(config.STATE_DIR, "dsp-voice.json")
        self.wake_wanted = bool((read_json(self.state_file, {}) or {}).get("wake"))   # persisted switch
        self.wake_word = os.environ.get("OWL_VOICE_WAKE_WORD") or "Hey Mycroft"
        self._off_reason, self._off_at, self._wd_resume_at = None, 0.0, 0.0   # why wake went off (watchdog resume)
        self.lock = threading.RLock()
        self._wlock = threading.Lock()     # serialises socket writes only; never held while computing status
        self._out = None                   # HA's socket file (write side)
        self._out_conn = None              # ... and the socket itself, for the send timeout and forced close
        self._rec, self._play = None, None
        self._halt = threading.Event()
        self._listen_until = 0
        self._zc = None
        self.srv = None

    # ------------------------------------------------------------------ status / controls
    def _block(self):
        """The design's Voice assistant block (its listen/reply wires), or None for a design without one."""
        d = self.dep.desired
        return getattr(d, "voice", None) if d else None

    def mic_node(self):
        if self.mic_override:
            return self.mic_override
        v = self._block()
        if v is not None:
            return v.get("mic")                    # what is wired into its 'listen' input (None: nothing)
        d = self.dep.desired
        if d:                                      # legacy designs without the block: the first AEC clean mic
            for bid, nodes in d.block_nodes.items():
                for n in nodes:
                    if n.endswith("-clean"):
                        return n
        return None

    def reply_node(self):
        if self.reply_override:
            return self.reply_override
        v = self._block()
        if v is not None:
            return v.get("reply")
        d = self.dep.desired
        if d:
            for bid, nodes in d.block_nodes.items():
                for n in nodes:
                    if n.endswith("-sc"):
                        return n
        return None

    def status(self):
        with self.lock:
            return {"state": self.state, "mic_live": self.mic_live, "last_text": self.last_text, "last_reply": self.last_reply,
                    "last_error": self.last_error, "ha": "connected" if self.ha_connected else "disconnected",
                    "ha_enabled": self.ha_enabled, "ha_peer": self.ha_peer, "port": self.port, "runs": self.runs,
                    "mic_node": self.mic_node(), "reply_node": self.reply_node(), "hold": self.hold,
                    "wired_by": ("override" if self.mic_override else "block" if self._block() is not None else "guess"),
                    "block": (self._block() or {}).get("block"),
                    "timings": dict(self.timings), "run_started": self.run_started, "max_listen_s": self.max_listen_s,
                    "wake": self.wake, "wake_wanted": self.wake_wanted, "wake_word": self.wake_word, "wake_max_min": self.wake_max_min,
                    "wake_ends_in_s": (round(self.wake_until - time.time()) if self.wake_until != float("inf") else None) if self.wake else None,
                    "wake_detections": self.wake_detections}

    def _set(self, **kw):
        with self.lock:
            for k, v in kw.items():
                setattr(self, k, v)
        self.bus.publish(dict({"type": "voice"}, **self.status()))

    def ptt(self, source="api"):
        """Start a PTT window (a trigger: ends at the end of the utterance, or after max_listen_s)."""
        with self.lock:
            if not self.ha_connected:
                raise RuntimeError("Home Assistant is not connected to the voice satellite (Wyoming on :%d)" % self.port)
            if not self.ha_enabled:
                raise RuntimeError("the satellite is paused in Home Assistant (muted or disabled)")
            if self.wake:
                raise RuntimeError("wake word test mode is on (the mic already streams); switch voice.wake_word off to use push-to-talk")
            if self.state == "listening":
                return {"ok": True, "state": self.state, "note": "already listening"}
            mic = self.mic_node()
            if not mic:
                raise RuntimeError("no microphone: wire a source (the AEC's clean mic) into the Voice assistant block's \"listen\" input")
            self.runs += 1
            self.run_started = time.time()
            self.timings = {"ptt": self.run_started}
            self.last_text, self.last_reply, self.last_error = "", "", ""
            self._send("run-pipeline", {"start_stage": "asr", "end_stage": "tts", "restart_on_end": False,
                                        "snd_format": {"rate": 22050, "width": 2, "channels": 1}})
            self._send("audio-start", {"rate": MIC_RATE, "width": MIC_WIDTH, "channels": MIC_CH, "timestamp": 0})
            self._rec = subprocess.Popen(["pw-record", "--raw", "--rate", str(MIC_RATE), "--channels", str(MIC_CH), "--format", "s16",
                                          "--target", mic, "--latency", "512",
                                          "-P", '{ node.name = "owl-voice-mic" node.description = "owl voice PTT mic" }', "-"],
                                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
            self._listen_until = time.time() + self.max_listen_s
            threading.Thread(target=self._pump, args=(self._rec,), daemon=True).start()
        self._set(state="listening", mic_live=True)
        log(f"PTT ({source}): listening on {mic} (run {self.runs})")
        return {"ok": True, "state": "listening", "mic": mic, "run": self.runs}

    def ptt_hold(self, on, source="api"):
        on = bool(on)
        with self.lock:
            self.hold = on
        if on:
            if self.state != "listening":
                return self.ptt(source=source)
            return {"ok": True, "state": self.state}
        self._stop_listening("released")
        return {"ok": True, "state": self.state}

    # ------------------------------------------------------------------ wake word test mode
    def set_wake(self, on, source="api"):
        on = bool(on)
        with self.lock:
            if not on:
                if not self.wake:
                    return {"ok": True, "wake": False}
                self.wake = False
                self._off_reason, self._off_at = source, time.time()
                self._stop_listening("wake word mode off")
                self._abort_play()
                self._set(state="idle")
                if not source.startswith("time limit") and source not in ("HA disconnected", "paused by HA", "mic ended", "no mic in the design"):
                    self._remember(False)              # an explicit off; a disconnect or pause keeps the wish
                log(f"wake word mode OFF ({source}) after {self.wake_detections} detection(s)")
                return {"ok": True, "wake": False}
            if self.wake:
                return {"ok": True, "wake": True, "note": "already on"}
            if not self.ha_connected:
                raise RuntimeError("Home Assistant is not connected to the voice satellite (Wyoming on :%d)" % self.port)
            if not self.ha_enabled:
                raise RuntimeError("the satellite is paused in Home Assistant (muted or disabled)")
            mic = self.mic_node()
            if not mic:
                raise RuntimeError("no microphone: wire a source (the AEC's clean mic) into the Voice assistant block's \"listen\" input")
            if self._rec is not None:
                self._stop_listening("wake word test mode replaces the PTT window")
            self.wake, self.wake_detections = True, 0
            self.wake_until = time.time() + self.wake_max_min * 60 if self.wake_max_min > 0 else float("inf")
            self.runs += 1
            self.run_started = time.time()
            self.timings = {"wake_on": self.run_started}
            self.last_text, self.last_reply, self.last_error = "", "", ""
            self._start_wake_stream(mic)
        self._set(state="wake", mic_live=True)
        self._remember(True)
        log(f"wake word mode ON ({source}): streaming {mic} to HA for the pipeline's wake word"
            + (f" for at most {self.wake_max_min:g} min" if self.wake_max_min > 0 else " (no time limit)"))
        return {"ok": True, "wake": True, "ends_in_s": self.wake_max_min * 60 if self.wake_max_min > 0 else None}

    def _remember(self, on):
        """The switch survives restarts and reboots: a listening satellite comes back listening."""
        try:
            self.wake_wanted = bool(on)
            atomic_write(self.state_file, {"wake": bool(on), "changed": time.time()})
        except Exception as e:
            log(f"could not persist the wake switch: {e}")

    def _resume_if_wanted(self, why):
        """After HA connects and enables us: back to listening if the switch was on. A skipped resume is logged (it
        used to be silent, which hid the reconnect race), and the watchdog retries while the link is up."""
        if not self.wake_wanted or self.wake:
            return
        if not (self.ha_connected and self.ha_enabled):
            log(f"wake word mode not resumed ({why}): HA connected={self.ha_connected} enabled={self.ha_enabled}")
            return
        try:
            self.set_wake(True, source=f"resume ({why})")
        except Exception as e:
            log(f"resume of the wake mode failed ({why}): {e}")

    def _rearm_wake(self, why):
        """HA stops its pipeline after an announcement, a started conversation or an error and then waits for the
        satellite's next run-pipeline (restart_on_end only covers a normal run end): send the wake stage again."""
        if not self.wake or self._out is None:
            return
        try:
            self._send("run-pipeline", {"start_stage": "wake", "end_stage": "tts", "restart_on_end": True,
                                        "snd_format": {"rate": 22050, "width": 2, "channels": 1}})
            self._last_arm = time.time()
            log(f"wake stage re-armed ({why})")
        except Exception as e:
            log(f"re-arm failed: {type(e).__name__}: {e}")

    def _start_wake_stream(self, mic):
        """run-pipeline from the wake stage (HA detects with the pipeline's wake word entity; restart_on_end keeps it
        listening after every run) and the continuous mic stream."""
        self._send("run-pipeline", {"start_stage": "wake", "end_stage": "tts", "restart_on_end": True,
                                    "snd_format": {"rate": 22050, "width": 2, "channels": 1}})
        self._send("audio-start", {"rate": MIC_RATE, "width": MIC_WIDTH, "channels": MIC_CH, "timestamp": 0})
        self._rec = self._record(mic)
        self._listen_until = self.wake_until
        self._last_arm = time.time()
        self._announce = False
        threading.Thread(target=self._pump, args=(self._rec,), daemon=True).start()

    def _record(self, mic):
        self._rec_target = mic
        return subprocess.Popen(["pw-record", "--raw", "--rate", str(MIC_RATE), "--channels", str(MIC_CH), "--format", "s16",
                                 "--target", mic, "--latency", "512",
                                 "-P", '{ node.name = "owl-voice-mic" node.description = "owl voice wake-word mic" }', "-"],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)

    def _retarget(self, mic):
        """The design now wires another source into the assistant's listen input: swap the recorder under the running
        wake stream (HA keeps its pipeline; the old pump sees it is no longer current and exits quietly)."""
        new = self._record(mic)
        with self.lock:
            old, self._rec = self._rec, new
        threading.Thread(target=self._pump, args=(new,), daemon=True).start()
        if old is not None:
            try:
                old.terminate()
                old.wait(2)
            except Exception:
                try:
                    old.kill()
                except Exception:
                    pass
        log(f"mic now {mic} (the design re-wired the voice assistant's listen input)")

    # ------------------------------------------------------------------ mic pump
    def _pump(self, rec):
        try:
            while rec.poll() is None:
                buf = rec.stdout.read(CHUNK)
                if not buf:
                    break
                # Level instrumentation: proves the satellite is streaming real audio to HA (remote debugging aid).
                _s = struct.unpack("<%dh" % (len(buf) // 2), buf[: len(buf) // 2 * 2])
                _pk = max((abs(x) for x in _s), default=0) / 32768.0
                self._lvl_pk = max(getattr(self, "_lvl_pk", 0.0), _pk)
                self._lvl_n = getattr(self, "_lvl_n", 0) + 1
                if time.time() - getattr(self, "_lvl_t", 0.0) > 2.0:
                    _db = 20 * __import__("math").log10(self._lvl_pk) if self._lvl_pk > 0 else -99.0
                    log(f"mic level peak {_db:.1f} dBFS over {self._lvl_n} chunks")
                    self._lvl_pk, self._lvl_n, self._lvl_t = 0.0, 0, time.time()
                with self.lock:
                    if self._rec is not rec:
                        break
                    if time.time() > self._listen_until:
                        break
                # Never send while holding self.lock: a stalled socket would freeze status()
                # and the Web GUI along with it.
                self._send("audio-chunk", {"rate": MIC_RATE, "width": MIC_WIDTH, "channels": MIC_CH, "timestamp": 0}, buf)
        except Exception as e:
            log(f"mic pump: {type(e).__name__}: {e}")
        if self._rec is rec:
            if self.wake:
                self.set_wake(False, source="time limit" if time.time() > self._listen_until else "mic ended")
                if time.time() <= self._listen_until:
                    threading.Timer(3.0, self._resume_if_wanted, ("mic ended",)).start()
            else:
                self._stop_listening("max listen time" if time.time() > self._listen_until else "mic ended")

    def _stop_listening(self, why):
        with self.lock:
            rec, self._rec = self._rec, None
        if rec is None:
            return
        # Teardown happens outside self.lock: rec.wait(2) would otherwise stall status()
        # (and the Web GUI polling it) for up to two seconds after every utterance.
        try:
            rec.terminate()
            rec.wait(2)
        except Exception:
            try:
                rec.kill()
            except Exception:
                pass
        self.timings["mic_closed"] = time.time()
        try:
            self._send("audio-stop", {"timestamp": 0})
        except Exception:
            pass
        self._set(mic_live=False, state="processing" if self.state == "listening" else self.state)
        log(f"mic closed ({why}) after {self.timings['mic_closed'] - self.timings.get('ptt', self.timings['mic_closed']):.1f} s")

    # ------------------------------------------------------------------ HA link
    def _kill_link(self, why):
        """Drop a dead HA connection. A peer that stops reading must never wedge the process:
        the old code wrote under self.lock with no timeout, so one half-dead socket froze
        status(), the designer and the wake stage forever."""
        with self.lock:
            wf, conn = self._out, self._out_conn
            self._out, self._out_conn = None, None
        log(f"link killed: {why}")
        for x in (wf, conn):
            try:
                if x is not None:
                    x.close()
            except Exception:
                pass
        self._set(ha_connected=False, ha_peer=None, last_error=why)

    def _send(self, typ, data=None, payload=b"", link=None):
        """Write one frame. `link` is a socket to answer on (a discovery probe that asked `describe`
        must get its own `info` back); without it the frame goes to the control link."""
        if link is not None:
            conn = link          # a socket, exactly like self._out_conn
        else:
            with self.lock:
                conn = self._out_conn
        if conn is None:
            raise RuntimeError("Home Assistant is not connected")
        frame = encode_event(typ, data, payload)
        with self._wlock:
            try:
                conn.sendall(frame)
            except OSError as e:
                if link is not None:
                    raise
                self._kill_link(f"send failed ({type(e).__name__}: {e})")
                raise

    def run(self):
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("0.0.0.0", self.port))
        self.srv.listen(32)
        self.srv.settimeout(1.0)
        log(f"Wyoming satellite {self.name!r} on :{self.port} (PTT only; mic {self.mic_node() or 'none yet'}, reply -> {self.reply_node() or 'none yet'})")
        self._advertise()
        threading.Thread(target=self._watchdog, daemon=True).start()
        while not self._halt.is_set():
            try:
                conn, addr = self.srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            # One thread per connection: an idle or half-dead HA link must not stop us accepting
            # the next one (the old inline _serve() left CLOSE-WAIT sockets jammed in the backlog).
            threading.Thread(target=self._serve, args=(conn, addr), daemon=True).start()
        self._unadvertise()

    def _advertise(self):
        """mDNS _wyoming._tcp so HA's Wyoming integration discovers us (python3-zeroconf is in the image)."""
        try:
            from zeroconf import ServiceInfo, Zeroconf
            ip = self.cfg.ip
            self._zc = Zeroconf()
            self._zi = ServiceInfo("_wyoming._tcp.local.", f"owl-dsp-{self.cfg.name}._wyoming._tcp.local.", addresses=[socket.inet_aton(ip)],
                                   port=self.port, properties={}, server=f"owl-dsp-{self.cfg.name}.local.")
            self._zc.register_service(self._zi)
            log(f"advertised _wyoming._tcp as owl-dsp-{self.cfg.name} at {ip}:{self.port}")
        except Exception as e:
            log(f"zeroconf: {type(e).__name__}: {e} (add the satellite in HA by host and port)")

    def _unadvertise(self):
        try:
            if self._zc:
                self._zc.unregister_service(self._zi)
                self._zc.close()
        except Exception:
            pass

    def _info(self):
        return {"asr": [], "tts": [], "handle": [], "intent": [], "wake": [],
                "satellite": {"name": self.name, "attribution": {"name": "owl-node", "url": "https://github.com/MorningstarOwl"},
                              "installed": True, "description": "owl-node dsp push-to-talk satellite (AEC clean mic, reply ducks the music)",
                              "version": "0.7.0", "area": self.cfg.area or None, "has_vad": False,
                              "snd_format": {"rate": 22050, "width": 2, "channels": 1}}}

    def _serve(self, conn, addr):
        peer = f"{addr[0]}:{addr[1]}"
        conn.settimeout(None)
        rf, wf = conn.makefile("rb"), conn.makefile("wb")
        try:
            # SO_SNDTIMEO bounds send() only, so a peer that stops reading raises instead of
            # blocking forever (recv is unaffected, so an idle-but-healthy HA link stays up).
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_SNDTIMEO, struct.pack("ll", SEND_TIMEOUT_S, 0))
        except OSError:
            pass
        # HA opens TWO kinds of connection: a persistent control link that starts with
        # run-satellite, and the ~30 s zeroconf discovery probe that connects, asks describe and
        # hangs up. A probe must never take over the control link, nor its close be mistaken for
        # HA going away -- that used to disarm the wake word every 30 s and the pipeline never ran.
        is_ctrl = False
        try:
            while not self._halt.is_set():
                ev = read_event(rf)
                if ev is None:
                    break
                if ev[0] == "run-satellite" and not is_ctrl:
                    is_ctrl = True
                    self._take_over(conn, wf, peer)
                self._handle(*ev, link=conn)
        except (OSError, ValueError) as e:
            log(f"link {peer}: {type(e).__name__}: {e}")
        finally:
            with self.lock:
                mine = self._out is wf
                if mine:
                    self._out, self._out_conn = None, None
            if mine:
                # HA can open its next control link while this one is still being torn down: the
                # old link's reset and the new link's run-satellite landed in the same second, and this cleanup
                # (set_wake -> _stop_listening waits up to 2 s for pw-record) then marked the NEW link
                # disconnected, so the 1 s resume saw ha_connected=False and voice stayed silently dead. Clean up
                # and drop the flags only while no newer link has taken over; _take_over stops a wake stream the
                # old link left running.
                with self.lock:
                    replaced = self._out is not None
                if not replaced:
                    if self.wake:
                        self.set_wake(False, source="HA disconnected")
                    self._stop_listening("HA disconnected")
                    self._abort_play()
                with self.lock:
                    replaced = self._out is not None
                    if not replaced:
                        self._set(ha_connected=False, ha_enabled=False, ha_peer=None, state="idle", mic_live=False)
                log(f"Home Assistant disconnected ({peer})" + (" — a newer link had already taken over" if replaced else ""))
            elif not is_ctrl:
                log(f"probe {peer} closed (describe only, link untouched)")
            try:
                conn.close()
            except OSError:
                pass

    def _take_over(self, conn, wf, peer):
        """Promote a connection to the control link, replacing any previous one.

        A wake stream still running for the previous link is stopped first (the wish is kept), so the run-satellite
        resume starts a fresh wake stage on THIS link instead of finding wake 'already on' and never arming it. The
        swap and the connected flag change together under the lock, so the previous link's cleanup cannot mark this
        link disconnected afterwards."""
        with self.lock:
            carried = self._out is not None and self.wake
        if carried:
            self.set_wake(False, source="HA disconnected")
        with self.lock:
            old, old_conn = self._out, self._out_conn
            self._out, self._out_conn = wf, conn
            self._set(ha_connected=True, ha_peer=peer, last_error="")
        for x in (old, old_conn):
            try:
                if x is not None:
                    x.close()
            except Exception:
                pass
        log(f"Home Assistant link established ({peer})" + (" (replaced the previous link)" if old is not None else ""))

    def _handle(self, typ, data, payload, link=None):
        if typ not in ("ping", "pong", "audio-chunk"):
            log(f"ha -> {typ}{f' ({len(payload)} B payload)' if payload else ''}")
        if typ == "describe":
            self._send("info", self._info(), link=link)
        elif typ == "run-satellite":
            self._set(ha_enabled=True)
            threading.Timer(1.0, self._resume_if_wanted, ("HA enabled the satellite",)).start()
        elif typ == "pause-satellite":
            if self.wake:
                self.set_wake(False, source="paused by HA")
            self._set(ha_enabled=False, state="idle")
        elif typ == "ping":
            self._send("pong", data or None, link=link)
        elif typ == "pong":
            pass
        elif typ == "transcribe":
            self.timings["transcribe"] = time.time()
        elif typ == "voice-started":
            self.timings["voice_started"] = time.time()
        elif typ == "voice-stopped":
            self.timings["voice_stopped"] = time.time()
            if self.wake:
                self._set(state="processing")           # the stream stays open: HA restarts at the wake stage after the run
            else:
                self._stop_listening("end of utterance")
        elif typ == "detection":
            self.timings = {"wake_on": self.timings.get("wake_on"), "detection": time.time()}     # one run's timings
            self.wake_detections += 1
            self.run_started = time.time()
            self._set(state="listening", last_text="", last_reply="", last_error="")
            log(f"wake word detected ({data.get('name')!r}), listening for the command")
            self._chime()                     # the audible cue: a blip, then talk
        elif typ == "transcript":
            self.timings["transcript"] = time.time()
            self._set(last_text=data.get("text") or "", state="processing")
            if not self.wake:
                self._stop_listening("transcript")
            log(f"heard: {data.get('text')!r}")
        elif typ == "synthesize":
            self.timings["synthesize"] = time.time()
            self._announce = False
            self._set(last_reply=data.get("text") or "", state="responding")
            log(f"reply: {data.get('text')!r}")
        elif typ == "audio-start":
            # audio with no synthesize before it in this run = an announcement / started conversation from HA's side
            self._announce = not self.timings.get("synthesize") or (self.timings.get("played") or 0) > self.timings["synthesize"]
            self._start_play(data)
        elif typ == "audio-chunk":
            self._play_chunk(payload)
        elif typ == "audio-stop":
            self._finish_play()
        elif typ == "error":
            self.timings["error"] = time.time()
            if self.wake:
                # HA does not restart the pipeline after an error (restart_on_end is ignored then): start the wake stage again
                self._set(last_error=f"{data.get('code') or 'error'}: {data.get('text') or ''}", state="wake")
                self._rearm_wake("pipeline error")
            else:
                self._stop_listening("pipeline error")
                self._set(last_error=f"{data.get('code') or 'error'}: {data.get('text') or ''}", state="idle")
            log(f"pipeline error: {data}")
        elif typ in ("detect", "streaming-started", "streaming-stopped") or (typ or "").startswith("timer-"):
            pass
        else:
            log(f"unhandled event {typ} {data}")

    # ------------------------------------------------------------------ reply playback (into the ducker's sidechain)
    def _chime(self):
        """Audible cue on wake-word detection, in its own thread so the Wyoming reader never blocks on it."""
        if not CUE_ON:
            return
        pcm, target = cue_pcm(), self.reply_node()

        def run():
            cmd = ["pw-play", "--raw", "--rate", str(CUE_RATE), "--channels", "1", "--format", "s16",
                   "--volume", f"{CUE_VOLUME:.2f}",
                   "-P", '{ node.name = "owl-voice-cue" node.description = "owl voice wake cue" '
                         'media.role = Notification }']
            if target:
                cmd += ["--target", target]
            cmd.append("-")
            p = None
            try:
                p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     start_new_session=True)
                p.communicate(pcm, timeout=5)
                log(f"wake cue played ({len(pcm) / (CUE_RATE * 2):.2f} s at {CUE_VOLUME:.2f} into "
                    f"{target or 'the default sink'})")
            except Exception as e:
                try:
                    if p:
                        p.kill()
                except Exception:
                    pass
                log(f"wake cue failed: {e}")

        threading.Thread(target=run, daemon=True, name="voice-cue").start()

    def _start_play(self, fmt):
        self._abort_play()
        target = self.reply_node()
        rate, width, ch = int(fmt.get("rate") or 22050), int(fmt.get("width") or 2), int(fmt.get("channels") or 1)
        f = {1: "s8", 2: "s16", 4: "s32"}.get(width, "s16")
        # --volume 1.0: WirePlumber restores a stored per-application volume onto pw-play streams (once 0.006: every
        # reply inaudible); an explicit volume is applied after the restore
        cmd = ["pw-play", "--raw", "--rate", str(rate), "--channels", str(ch), "--format", f, "--volume", "1.0",
               "-P", '{ node.name = "owl-voice-reply" node.description = "owl voice reply" media.role = Notification }']
        if target:
            cmd += ["--target", target]
        cmd.append("-")
        self.timings["reply_audio"] = time.time()
        self._play = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        self._play_bytes, self._play_rate = 0, rate * width * ch
        self._set(state="responding")

    def _play_chunk(self, payload):
        p = self._play
        if p and p.stdin and payload:
            try:
                p.stdin.write(payload)
                self._play_bytes += len(payload)
            except (BrokenPipeError, OSError):
                pass

    def _finish_play(self):
        p, self._play = self._play, None
        if not p:
            return
        try:
            p.stdin.close()
        except Exception:
            pass
        secs = self._play_bytes / float(self._play_rate or 44100)
        try:
            p.wait(secs + 5)
        except subprocess.TimeoutExpired:
            p.kill()
        self.timings["played"] = time.time()
        try:
            self._send("played")
        except Exception:
            pass
        self._set(state="wake" if self.wake else "idle")
        log(f"{'announcement' if getattr(self, '_announce', False) else 'reply'} played ({secs:.1f} s of audio into {self.reply_node() or 'the default sink'})")
        if self.wake and (getattr(self, "_announce", False) or REARM_AFTER_REPLY):
            # only announcements/cancellations: HA restores its own wake stage after a normal reply
            self._rearm_wake("after playback")

    def _abort_play(self):
        p, self._play = self._play, None
        if p:
            try:
                p.kill()
            except Exception:
                pass

    def _watchdog(self):
        """A run that gets no reply ends after RUN_TIMEOUT_S; a listening window never outlives max_listen_s."""
        while not self._halt.is_set():
            time.sleep(1.0)
            with self.lock:
                st, t0, rec = self.state, self.run_started, self._rec
            if self.wake and time.time() > self.wake_until:
                self.set_wake(False, source="time limit")
            elif self.wake and st == "wake" and time.time() - max(getattr(self, "_last_arm", 0), self.timings.get("detection") or 0) > REARM_IDLE_S:
                self._rearm_wake("periodic")           # safety net (idle re-arm): a wake stage HA dropped without telling us; a
                                                       # re-arm restarts HA's wake stage and drops audio in flight, so rarely
            elif rec is not None and not self.wake and time.time() > self._listen_until:
                self._stop_listening("max listen time")
            elif st in ("processing", "responding") and not self.wake and t0 and time.time() - t0 > RUN_TIMEOUT_S:
                self._abort_play()
                self._set(state="idle", last_error=self.last_error or "no reply from the pipeline in time")
                log("run timed out")
            elif st in ("listening", "processing", "responding") and self.wake and self.timings.get("detection") and time.time() - self.timings["detection"] > RUN_TIMEOUT_S:
                self._abort_play()
                self._set(state="wake", last_error=self.last_error or "no reply from the pipeline in time")
                log("wake-word run timed out, back to listening for the wake word")
            # Safety net for the HA reconnect race: the switch is on and a control link is up and enabled, but wake
            # went off because of a disconnect and no resume armed it. Never after an explicit off, a time limit, a
            # pause or during PTT; at most every 10 s, so a resume that keeps failing cannot flood the log.
            # D3.1: the listen wire is the design's: follow a re-wire (or a removed wire) while streaming
            if self.wake and rec is not None and st == "wake":
                want = self.mic_node()
                if want is None:
                    self.set_wake(False, source="no mic in the design")
                elif want != getattr(self, "_rec_target", want):
                    try:
                        self._retarget(want)
                    except Exception as e:
                        log(f"retarget to {want}: {type(e).__name__}: {e}")
            now = time.time()
            if (not self.wake and self.wake_wanted and self._off_reason == "no mic in the design" and rec is None
                    and st == "idle" and self.mic_node() and self._out is not None and self.ha_connected and self.ha_enabled
                    and now - self._wd_resume_at > 10):
                self._wd_resume_at = now
                self._resume_if_wanted("watchdog: a mic is wired again")
            if (not self.wake and self.wake_wanted and self._off_reason == "HA disconnected" and rec is None
                    and st == "idle" and self._out is not None and self.ha_connected and self.ha_enabled
                    and now - self._off_at > 5 and now - self._wd_resume_at > 10):
                self._wd_resume_at = now
                self._resume_if_wanted("watchdog: link up, wake wanted")

    def stop(self):
        self._halt.set()
        self._stop_listening("stopping")
        self._abort_play()
        try:
            if self.srv:
                self.srv.close()
        except Exception:
            pass
