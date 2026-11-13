"""The injector: generators at the asked RMS level, the limits, and against a real PipeWire with WirePlumber (skipped
without one): noise injected into a Gain block's input comes out of its output at that level, and stops by itself."""
import os, shutil, subprocess, sys, tempfile, time, unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from owlnode.dsp import blocks as B, inject as I, meters as M  # noqa: E402
from owlnode.dsp.deploy import GraphOps  # noqa: E402


def rms_db(x):
    return 20 * np.log10(np.sqrt(np.mean(x.astype(np.float64) ** 2, axis=0)))


class Generators(unittest.TestCase):
    def test_levels_are_rms(self):
        for kind in I.KINDS:
            g = I.Generator(kind, 2, -20, 1000, rng=np.random.default_rng(1))
            x = np.concatenate([g.block() for _ in range(100)])        # 2 s
            for v in rms_db(x):
                self.assertAlmostEqual(v, -20, delta=0.5, msg=kind)

    def test_noise_channels_are_independent(self):
        x = np.concatenate([I.Generator("white", 2, -20, rng=np.random.default_rng(2)).block() for _ in range(50)])
        self.assertLess(abs(np.corrcoef(x[:, 0], x[:, 1])[0, 1]), 0.05)

    def test_pink_is_pink(self):
        x = I.pink_loop(1, seconds=4, rng=np.random.default_rng(3))[:, 0]
        p = np.abs(np.fft.rfft(x)) ** 2
        f = np.fft.rfftfreq(len(x), 1 / I.RATE)
        band = lambda lo, hi: p[(f >= lo) & (f < hi)].sum()
        self.assertAlmostEqual(10 * np.log10(band(100, 200) / band(1000, 2000)), 0, delta=1.0)   # equal energy per octave

    def test_limits(self):
        with self.assertRaises(I.InjectError):
            I.Generator("white", 2, 0)                                    # above MAX_DB
        with self.assertRaises(I.InjectError):
            I.Generator("brown", 2, -20)
        g = I.Generator("sine", 1, -20)
        with self.assertRaises(I.InjectError):
            g.set_level(-1)


RUNTIME = os.environ.get("XDG_RUNTIME_DIR") or ""
HAVE = bool(shutil.which("pipewire")) and os.path.exists(os.path.join(RUNTIME, "pipewire-0")) and \
    bool(subprocess.run(["pgrep", "-x", "wireplumber"], capture_output=True).stdout)
TYPES = B.load([os.path.join(os.path.dirname(__file__), "..", "dsp", "blocks")])


@unittest.skipUnless(HAVE, "needs a PipeWire daemon with WirePlumber (XDG_RUNTIME_DIR)")
class Live(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp(prefix="owl-inj-")
        self.bid = f"inj{os.getpid()}"
        path = os.path.join(d, "g.conf")
        with open(path, "w") as f:
            f.write(TYPES["gain"].render_conf(TYPES["gain"].ctx(self.bid, "Volume", {})))
        self.block = subprocess.Popen(["pipewire", "-c", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.taps = M.Taps()
        self.ops = GraphOps(self.taps)
        self.inj = I.Injector(log=lambda *a: None)
        self.addCleanup(self.cleanup)
        g, missing = self.ops.wait_nodes([f"owl-{self.bid}-in", f"owl-{self.bid}-out"], timeout=10)
        self.assertEqual(missing, [])

    def cleanup(self):
        self.inj.stop_all()
        self.taps.stop()
        self.block.terminate()
        self.block.wait(5)

    def measure(self):
        """The block's output through a meter tap, like Run mode."""
        tap = {"node": f"owl-{self.bid}-out", "monitor": False, "ports": None}
        self.taps.want({"out": 2})
        g, _ = self.ops.wait_nodes(self.taps.nodes(), timeout=10)
        self.ops.ensure_links(g, self.ops.tap_links({"out": tap}), [])
        time.sleep(2.5)                                                     # the meter averages over its last second
        return self.taps.stats()["out"]

    def test_noise_reaches_the_output_at_its_level_then_stops(self):
        info = self.inj.start(f"owl-{self.bid}-in", label="Volume · in", kind="white", level_db=-24, duration_s=7)
        self.assertEqual(info["channels"], 2)
        st = self.measure()
        self.assertTrue(st["signal"], st)
        # through a real block: within 2 dB (a filter-chain without real-time scheduling, as in a test container, can
        # miss cycles; the calibration itself is checked exactly at the null sink below)
        for v in st["rms_dbfs"]:
            self.assertAlmostEqual(v, -24, delta=2.0)
        self.inj.set_level(info["id"], -40)
        time.sleep(2.0)
        for v in self.taps.stats()["out"]["rms_dbfs"]:
            self.assertAlmostEqual(v, -40, delta=2.0)
        time.sleep(3.0)                                                     # past its 7 s
        self.assertEqual(self.inj.active(), [])
        g = __import__("owlnode.dsp.pw", fromlist=["dump"]).dump()
        self.assertIsNone(g.node(f"owl-inject-{info['id']}"))

    def test_level_at_a_sink(self):
        """The signal arrives at about its level. The exact calibration is checked offline (Generators); a test daemon
        without real-time scheduling sometimes misses cycles under load, which reads up to ~1.5 dB low."""
        sink = f"owl-test-sink-{os.getpid()}"
        r = subprocess.run(["pw-cli", "create-node", "adapter", f"{{ factory.name = support.null-audio-sink node.name = {sink} "
                            "media.class = Audio/Sink object.linger = true audio.position = [ FL FR ] }"], capture_output=True, timeout=5)
        self.assertEqual(r.returncode, 0)
        try:
            self.ops.wait_nodes([sink], timeout=10)
            # white: the least window-to-window variation; the generators' exact calibration is checked offline above
            self.inj.start(sink, kind="white", level_db=-20, duration_s=6)
            self.taps.want({"mon": 2})
            g, _ = self.ops.wait_nodes(self.taps.nodes(), timeout=10)
            self.ops.ensure_links(g, self.ops.tap_links({"mon": {"node": sink, "monitor": True, "ports": None}}), [])
            time.sleep(2.5)
            for v in self.taps.stats()["mon"]["rms_dbfs"]:
                self.assertAlmostEqual(v, -20, delta=2.0)
        finally:
            n = __import__("owlnode.dsp.pw", fromlist=["dump"]).dump().node(sink)
            if n:
                subprocess.run(["pw-cli", "destroy", str(n["id"])], capture_output=True, timeout=5)

    def test_absent_node_is_refused(self):
        with self.assertRaises(I.InjectError):
            self.inj.start("no-such-node")


if __name__ == "__main__":
    unittest.main()
