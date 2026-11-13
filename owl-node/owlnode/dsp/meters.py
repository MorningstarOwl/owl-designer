"""Passive meter taps, read as raw float32 and reduced to RMS / peak / clip per channel. Used by Meter blocks and by
Run mode (every block output and every node a wire reads from).

A tap is a range of channels of a meter BANK: one `pw-record` stream of up to BANK_CH (64) channels per bank, its
node `owl-meter-bank-<n>`. The deployer links a tapped node's ports into its tap's channels like any wire. A design
with 30 stereo taps used to run 30 `pw-record` processes and 30 reader threads; it now runs one process and one thread,
and a tap that comes or goes only adds or removes links (no process starts or stops)."""
import math, os, subprocess, threading, time
from collections import deque

import numpy as np

RATE = 48000
BLOCK = 4800            # 100 ms
BANK_CH = int(os.environ.get("OWL_METER_BANK_CH") or 64)     # PipeWire's SPA_AUDIO_MAX_CHANNELS
_RAW = []


def record_has_raw():
    """pw-record --raw (PipeWire >= 1.2): headerless PCM on stdout. Older ones write a WAV stream instead."""
    if not _RAW:
        try:
            out = subprocess.run(["pw-record", "--help"], capture_output=True, text=True, timeout=5)
            _RAW.append("--raw" in (out.stdout + out.stderr))
        except Exception:
            _RAW.append(True)
    return _RAW[0]


def skip_wav_header(f):
    """Before the PCM of a pw-record without --raw: a streamed WAV header (RIFF/WAVE, chunks up to 'data') is read past;
    some versions write raw PCM to stdout anyway. -> the PCM bytes already read (b"" or the first 4), or None at EOF."""
    head = f.read(4)
    if len(head) < 4:
        return None
    if head != b"RIFF":
        return head                             # raw already: those 4 bytes are the first sample
    rest = f.read(8)
    if len(rest) < 8 or rest[4:8] != b"WAVE":
        return None
    while True:
        ch = f.read(8)
        if len(ch) < 8:
            return None
        size = int.from_bytes(ch[4:8], "little")
        if ch[:4] == b"data":
            return b""
        f.read(size + (size & 1))


def levels(x):
    """x: frames x channels float32 -> (rms, peak, clip fraction) per channel."""
    return np.sqrt(np.mean(x * x, axis=0)), np.max(np.abs(x), axis=0), np.mean(np.abs(x) >= 0.999, axis=0)


class Bank:
    """One pw-record stream of `channels` channels; keeps ~2 s of 100 ms per-channel level blocks."""

    def __init__(self, idx, channels=BANK_CH, argv=None):
        self.idx, self.channels = idx, channels
        self.node = f"owl-meter-bank-{idx}"
        self.p, self.started, self.frames = None, None, 0
        self.hist, self.lock = deque(maxlen=20), threading.Lock()
        self._argv = argv                       # tests: a generator instead of pw-record

    def argv(self):
        if self._argv:
            return self._argv
        props = f'{{ node.name = "{self.node}" node.description = "owl meters {self.idx}" node.autoconnect = false ' \
                f'owl.dsp.meter = "bank-{self.idx}" media.class = Stream/Input/Audio }}'
        return (["pw-record"] + (["--raw"] if record_has_raw() else []) +
                ["--format", "f32", "--rate", str(RATE), "--channels", str(self.channels), "--target", "0",
                 "--latency", "1024", "-P", props, "-"])

    def start(self):
        self.p = subprocess.Popen(self.argv(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
        self.started = time.time()
        threading.Thread(target=self._read, args=(self.p,), daemon=True, name=self.node).start()

    def _read(self, p):
        pre = b""
        if not self._argv and not record_has_raw():
            pre = skip_wav_header(p.stdout)
            if pre is None:
                return
        n = BLOCK * self.channels * 4
        while True:
            buf = pre + p.stdout.read(n - len(pre))
            pre = b""
            if len(buf) < n:
                break
            rms, peak, clip = levels(np.frombuffer(buf, dtype=np.float32).reshape(-1, self.channels))
            with self.lock:
                self.hist.append((time.time(), rms, peak, clip))
                self.frames += BLOCK

    def running(self):
        return self.p is not None and self.p.poll() is None

    def stop(self):
        if self.running():
            self.p.terminate()
            try:
                self.p.wait(3)
            except subprocess.TimeoutExpired:
                self.p.kill()
        if self.p is not None and self.p.stdout:
            self.p.stdout.close()
        self.p = None

    def stats(self, off, ch, since=0.0):
        """Levels of channels [off, off+ch) over the last second, from blocks recorded after `since` (when the tap got
        these channels: older blocks belong to whatever used them before)."""
        now = time.time()
        with self.lock:
            h = [x for x in self.hist if now - x[0] < 1.0 and x[0] >= since]
            last = self.hist[-1][0] if self.hist else None
        if not h:
            return {"running": self.running(), "signal": False, "rms_dbfs": None, "peak_dbfs": None, "clip": 0.0,
                    "age_s": round(now - last, 1) if last else None}
        rms = np.sqrt(np.mean(np.stack([x[1][off:off + ch] ** 2 for x in h]), axis=0))
        peak = np.max(np.stack([x[2][off:off + ch] for x in h]), axis=0)
        db = lambda v: None if v <= 1e-6 else round(20 * math.log10(float(v)), 1)
        return {"running": self.running(), "signal": bool(np.max(rms) > 10 ** (-70 / 20)),
                "rms_dbfs": [db(v) for v in rms], "peak_dbfs": [db(v) for v in peak],
                "clip": round(float(max(np.max(x[3][off:off + ch]) for x in h)), 4), "age_s": 0}


class Taps:
    """Taps by name -> channel ranges of meter banks. want({name: channels}) keeps every tap that still fits where it
    is, places new ones in the first gap (or a new bank), starts the banks in use, stops the empty ones and restarts a
    bank whose process died."""

    def __init__(self, bank_ch=BANK_CH, argv=None):
        self.bank_ch, self._argv = bank_ch, argv
        self.banks = {}                         # idx -> Bank
        self.alloc = {}                         # name -> (bank idx, offset, channels, since)

    def _place(self, name, ch):
        ch = max(1, min(int(ch), self.bank_ch))
        idx = 0
        while True:
            used = sorted((o, o + c) for (b, o, c, _) in self.alloc.values() if b == idx)
            off = 0
            for a, z in used:
                if a - off >= ch:
                    break
                off = max(off, z)
            if off + ch <= self.bank_ch:
                self.alloc[name] = (idx, off, ch, time.time() + BLOCK / RATE)
                return
            idx += 1

    def want(self, desired):
        for name in list(self.alloc):
            if name not in desired or self.alloc[name][2] != max(1, min(int(desired[name]), self.bank_ch)):
                del self.alloc[name]
        for name in sorted(desired):
            if name not in self.alloc:
                self._place(name, desired[name])
        needed = {b for (b, _, _, _) in self.alloc.values()}
        for idx in list(self.banks):
            if idx not in needed:
                self.banks.pop(idx).stop()
        for idx in needed:
            bk = self.banks.get(idx)
            if bk and not bk.running() and time.time() - (bk.started or 0) > 2:
                bk.stop()
                bk = None
            if not bk:
                bk = self.banks[idx] = Bank(idx, self.bank_ch, self._argv)
                bk.start()

    def target(self, name):
        """(bank node, [channel indexes]) the deployer links tap `name` into, or None."""
        a = self.alloc.get(name)
        if not a:
            return None
        idx, off, ch, _ = a
        return f"owl-meter-bank-{idx}", list(range(off, off + ch))

    def nodes(self):
        return sorted(f"owl-meter-bank-{i}" for i in self.banks)

    def stats(self):
        out = {}
        for name, (idx, off, ch, since) in self.alloc.items():
            bk = self.banks.get(idx)
            out[name] = bk.stats(off, ch, since) if bk else {"running": False, "signal": False, "rms_dbfs": None,
                                                             "peak_dbfs": None, "clip": 0.0, "age_s": None}
        return out

    def stop(self):
        for bk in self.banks.values():
            bk.stop()
        self.banks, self.alloc = {}, {}
