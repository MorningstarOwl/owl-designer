"""Sound files the household uploads in the designer (doorbells, chimes, alarms, notification sounds) and the Sound
player block that plays them when triggered.

Library: /var/lib/owl-node/sounds/<name> on the hub. WAV, FLAC, Ogg (Vorbis/Opus) and MP3, recognised by their
content (not just the name), at most MAX_FILE each and MAX_TOTAL together.
  GET    /api/dsp/sounds                  the library
  POST   /api/dsp/sounds?name=<file name> the file as the request body (any content type): add or replace
  GET    /api/dsp/sounds/<name>/file      the file itself (the designer previews it in the browser)
  DELETE /api/dsp/sounds/<name>

Player: a Sound player block (dsp/blocks/sound.yaml) is a filter-chain sink -> volume -> source that runs all the time
on the hub, so its output can be wired anywhere (a speaker, a ducker's sidechain, another room over AES67) and stays
linked. Its named controls `<block>.play` and `<block>.stop` (Panels buttons, Home Assistant buttons, the API) start
and stop a `pw-play` of the block's sound, linked into the block's sink; with `loop` on it starts again until stopped.
A stream that is not linked yet is not scheduled, so a sound never loses its first moments to the linking."""
import os, re, subprocess, tempfile, threading, time, wave

from .. import config
from . import pw

MAX_FILE = 25 * 1024 * 1024
MAX_TOTAL = 500 * 1024 * 1024
FORMATS = {"wav": (".wav",), "flac": (".flac",), "ogg": (".ogg", ".oga", ".opus"), "mp3": (".mp3",)}


class SoundError(Exception):
    pass


def sniff(data):
    """The audio format of a file's first bytes, or None."""
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "wav"
    if data[:4] == b"fLaC":
        return "flac"
    if data[:4] == b"OggS":
        return "ogg"
    if data[:3] == b"ID3" or (len(data) > 1 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0):
        return "mp3"
    return None


def clean_name(name, fmt):
    """A safe file name for the library, with an extension that matches the content."""
    base = os.path.basename(str(name or "").replace("\\", "/")).strip()
    stem, ext = os.path.splitext(base)
    stem = re.sub(r"[^A-Za-z0-9 ._()-]+", "_", stem).strip(" ._") or "sound"
    stem = stem[:80]
    ext = ext.lower()
    if ext not in FORMATS[fmt]:
        ext = FORMATS[fmt][0]
    return stem + ext


class Library:
    def __init__(self, path=None):
        self.dir = path or os.path.join(config.STATE_DIR, "sounds")
        self.lock = threading.Lock()

    def _path(self, name):
        if not name or "/" in name or name.startswith(".") or name != os.path.basename(name):
            raise SoundError(f"no sound {name!r}")
        return os.path.join(self.dir, name)

    def names(self):
        try:
            return sorted(f for f in os.listdir(self.dir) if not f.startswith(".") and os.path.isfile(os.path.join(self.dir, f)))
        except OSError:
            return []

    def path(self, name):
        p = self._path(name)
        if not os.path.isfile(p):
            raise SoundError(f"no sound {name!r} in the library (deleted?)")
        return p

    def info(self, name):
        p = self.path(name)
        st = os.stat(p)
        with open(p, "rb") as f:
            fmt = sniff(f.read(16))
        dur = None
        if fmt == "wav":
            try:
                with wave.open(p) as w:
                    dur = round(w.getnframes() / float(w.getframerate()), 2)
            except Exception:
                pass                                     # float or extensible WAVs: wave cannot read them; fine
        return {"name": name, "size": st.st_size, "format": fmt, "duration_s": dur, "added": st.st_mtime}

    def list(self):
        return [self.info(n) for n in self.names()]

    def add(self, name, data):
        if not data:
            raise SoundError("the file is empty")
        if len(data) > MAX_FILE:
            raise SoundError(f"the file is {len(data) // (1024 * 1024)} MB; at most {MAX_FILE // (1024 * 1024)} MB")
        fmt = sniff(data[:16])
        if not fmt:
            raise SoundError("not a sound file this player knows (WAV, FLAC, Ogg Vorbis/Opus or MP3)")
        name = clean_name(name, fmt)
        with self.lock:
            others = sum(os.path.getsize(os.path.join(self.dir, n)) for n in self.names() if n != name)
            if others + len(data) > MAX_TOTAL:
                raise SoundError(f"the library would exceed {MAX_TOTAL // (1024 * 1024)} MB; delete some sounds first")
            os.makedirs(self.dir, exist_ok=True)
            self._write(name, data)
        return self.info(name)

    def _write(self, name, data):
        """Write-then-rename with fsync, like config.atomic_write (which writes text)."""
        fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=".up-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o644)
            os.replace(tmp, self._path(name))
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def delete(self, name):
        p = self.path(name)
        os.unlink(p)
        return {"ok": True, "name": name}


class Player:
    """Plays library sounds into Sound player blocks of the deployed design (hub only)."""

    def __init__(self, dep, library, log=print, on_change=None):
        self.dep, self.lib, self.log, self.on_change = dep, library, log, on_change
        self.lock = threading.Lock()
        self.playing = {}                 # block id -> {"p": Popen, "sound", "since", "loop", "stop": Event}

    def _block(self, bid):
        b = next((x for x in (self.dep.design or {}).get("blocks") or [] if x.get("id") == bid), None)
        bt = self.dep.types.get(b.get("type")) if b else None
        if not b or not bt or not bt.raw.get("triggers"):
            raise SoundError(f"no sound player {bid} in the deployed design")
        ctx = bt.ctx(bid, b.get("label"), b.get("params"))
        return b, bt, ctx

    def trigger(self, bid, action, source="api"):
        if action == "stop":
            stopped = self.stop(bid)
            return {"ok": True, "block": bid, "playing": False, "stopped": stopped}
        if action != "play":
            raise SoundError(f"unknown action {action!r} (play, stop)")
        b, bt, ctx = self._block(bid)
        sound = ctx.params.get("sound")
        if not sound:
            raise SoundError(f"{ctx.label}: no sound chosen (its inspector, or upload one in the Design panel)")
        path = self.lib.path(sound)
        sink = bt.node_map(ctx)["sink"]["node"]
        self.stop(bid, quiet=True)                         # a new trigger restarts the sound
        ev = threading.Event()
        rec = {"sound": sound, "since": time.time(), "loop": bool(ctx.params.get("loop")), "stop": ev, "p": None, "sink": sink}
        with self.lock:
            self.playing[bid] = rec
        threading.Thread(target=self._run, args=(bid, rec, path), daemon=True, name=f"sound-{bid}").start()
        self.log(f"sound {bid}: {sound} ({source})" + (" looping" if rec["loop"] else ""))
        self._changed(bid, True, source)
        return {"ok": True, "block": bid, "sound": sound, "playing": True, "loop": rec["loop"]}

    def _run(self, bid, rec, path):
        me = f"owl-sound-{bid}"
        props = f'{{ node.name = "{me}" node.description = "owl sound {rec["sound"]}" node.autoconnect = false media.class = Stream/Output/Audio }}'
        while not rec["stop"].is_set():
            p = subprocess.Popen(["pw-play", "--target", "0", "-P", props, path], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
            rec["p"] = p
            linked = False
            while p.poll() is None and not rec["stop"].is_set():
                if not linked:
                    try:
                        g = pw.dump()
                        pairs = pw.pair_ports(g.node_ports(me, "out"), g.node_ports(rec["sink"], "in"))
                        have = g.link_set()
                        for a, z in pairs:
                            if (a["id"], z["id"]) not in have:
                                pw.link(a["id"], z["id"])
                        linked = bool(pairs)
                    except Exception as e:
                        self.log(f"sound {bid} link: {type(e).__name__}: {e}")
                rec["stop"].wait(0.05 if not linked else 0.2)
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(2)
                except subprocess.TimeoutExpired:
                    p.kill()
            if not rec["loop"]:
                break
        with self.lock:
            if self.playing.get(bid) is rec:
                del self.playing[bid]
                ended = True
            else:
                ended = False
        if ended:
            self._changed(bid, False, "ended")

    def stop(self, bid, quiet=False):
        with self.lock:
            rec = self.playing.pop(bid, None)
        if not rec:
            return False
        rec["stop"].set()
        p = rec.get("p")
        if p is not None and p.poll() is None:
            p.terminate()
        if not quiet:
            self._changed(bid, False, "stopped")
        return True

    def stop_sound(self, name):
        """Stop every block playing this sound (it is being deleted)."""
        for bid, r in list(self.playing.items()):
            if r["sound"] == name:
                self.stop(bid)

    def stop_all(self):
        for bid in list(self.playing):
            self.stop(bid, quiet=True)

    def status(self):
        with self.lock:
            return {bid: {"sound": r["sound"], "since": r["since"], "loop": r["loop"]} for bid, r in self.playing.items()}

    def _changed(self, bid, playing, source):
        if self.on_change:
            try:
                self.on_change(bid, playing, source)
            except Exception as e:
                self.log(f"sound {bid}: {type(e).__name__}: {e}")
