"""Passive meter taps: one `pw-record` stream per tapped hub node (linked by the deployer like any wire), read as raw
float32, reduced to RMS/peak per channel. Used by Meter blocks and by Run mode for the AEC's own ports."""
import math, subprocess, sys, threading, time
from collections import deque

import numpy as np

RATE = 48000
BLOCK = 4800            # 100 ms


class Tap:
    def __init__(self, name, node_name, channels):
        self.name, self.node_name, self.channels = name, node_name, channels
        self.p, self.hist, self.lock, self.started = None, deque(maxlen=20), threading.Lock(), None
        self.frames = 0

    def start(self):
        props = f'{{ node.name = "{self.node_name}" node.description = "owl meter {self.name}" node.autoconnect = false ' \
                f'owl.dsp.meter = "{self.name}" media.class = Stream/Input/Audio }}'
        self.p = subprocess.Popen(["pw-record", "--raw", "--format", "f32", "--rate", str(RATE), "--channels", str(self.channels),
                                   "--target", "0", "--latency", "1024", "-P", props, "-"],
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
        self.started = time.time()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        n = BLOCK * self.channels * 4
        p = self.p
        while True:
            buf = p.stdout.read(n)
            if not buf or len(buf) < n:
                break
            x = np.frombuffer(buf, dtype=np.float32).reshape(-1, self.channels)
            rms = np.sqrt(np.mean(x * x, axis=0))
            peak = np.max(np.abs(x), axis=0)
            clip = float(np.mean(np.abs(x) >= 0.999))
            with self.lock:
                self.hist.append((time.time(), rms, peak, clip))
                self.frames += len(x)

    def running(self):
        return self.p is not None and self.p.poll() is None

    def stop(self):
        if self.running():
            self.p.terminate()
            try:
                self.p.wait(3)
            except subprocess.TimeoutExpired:
                self.p.kill()
        self.p = None

    def stats(self):
        with self.lock:
            h = [x for x in self.hist if time.time() - x[0] < 1.0]
        if not h:
            return {"running": self.running(), "signal": False, "rms_dbfs": None, "peak_dbfs": None, "clip": 0.0,
                    "age_s": round(time.time() - self.hist[-1][0], 1) if self.hist else None}
        rms = np.sqrt(np.mean(np.stack([x[1] ** 2 for x in h]), axis=0))
        peak = np.max(np.stack([x[2] for x in h]), axis=0)
        db = lambda v: None if v <= 1e-6 else round(20 * math.log10(float(v)), 1)
        return {"running": self.running(), "signal": bool(np.max(rms) > 10 ** (-70 / 20)),
                "rms_dbfs": [db(v) for v in rms], "peak_dbfs": [db(v) for v in peak],
                "clip": round(max(x[3] for x in h), 4), "age_s": 0}


class Taps:
    def __init__(self):
        self.taps = {}

    def want(self, desired):
        """desired: {name: channels}; starts/stops taps to match."""
        for name in list(self.taps):
            if name not in desired or self.taps[name].channels != desired[name]:
                self.taps.pop(name).stop()
        for name, ch in desired.items():
            t = self.taps.get(name)
            if t and not t.running() and time.time() - (t.started or 0) > 2:
                t.stop()
                t = None
            if not t:
                t = self.taps[name] = Tap(name, f"owl-meter-{name}", ch)
                t.start()

    def stats(self):
        return {n: t.stats() for n, t in self.taps.items()}

    def stop(self):
        for t in self.taps.values():
            t.stop()
        self.taps = {}
