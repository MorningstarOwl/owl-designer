"""Meter banks: taps are channel ranges of shared pw-record streams. Allocation (gaps reused, taps that stay keep their
channels, a new bank when one is full), levels per tap from a synthetic stream, and against a real PipeWire with
WirePlumber (skipped without one): a test tone linked into a tap's channels reads there and nowhere else."""
import os, shutil, subprocess, sys, tempfile, time, unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from owlnode.dsp import meters as M, pw  # noqa: E402
from owlnode.dsp.deploy import GraphOps  # noqa: E402

# a stand-in for pw-record --raw: 8 channels, channel c at a constant amplitude AMP[c]
AMP = [0.5, 0.5, 0.1, 0.1, 0, 0, 0, 0]          # taps are placed in name order: music, quiet, silent
GEN = [sys.executable, "-c", "import sys, struct, time\n"
       f"amp = {AMP!r}\n"
       "frame = struct.pack('<8f', *amp) * 4800\n"
       "while True:\n    sys.stdout.buffer.write(frame); sys.stdout.buffer.flush(); time.sleep(0.1)\n"]


class Allocation(unittest.TestCase):
    def taps(self, ch=8):
        t = M.Taps(bank_ch=ch, argv=["sleep", "30"])
        self.addCleanup(t.stop)
        return t

    def test_places_keeps_reuses_and_opens_banks(self):
        t = self.taps()
        t.want({"a": 2, "b": 2, "c": 2})
        self.assertEqual([t.target(n) for n in "abc"], [("owl-meter-bank-0", [0, 1]), ("owl-meter-bank-0", [2, 3]),
                                                         ("owl-meter-bank-0", [4, 5])])
        t.want({"a": 2, "c": 2, "d": 1, "e": 2})                     # b leaves; d fits its gap; e still fits bank 0
        self.assertEqual(t.target("a"), ("owl-meter-bank-0", [0, 1]))
        self.assertEqual(t.target("c"), ("owl-meter-bank-0", [4, 5]))
        self.assertEqual(t.target("d"), ("owl-meter-bank-0", [2]))
        self.assertEqual(t.target("e"), ("owl-meter-bank-0", [6, 7]))
        t.want({"a": 2, "c": 2, "d": 1, "e": 2, "f": 2})              # full: a second bank
        self.assertEqual(t.target("f"), ("owl-meter-bank-1", [0, 1]))
        self.assertEqual(t.nodes(), ["owl-meter-bank-0", "owl-meter-bank-1"])
        first = t.banks[0].p
        t.want({"f": 2})                                              # bank 0 empties: stopped; bank 1 untouched
        self.assertEqual(t.nodes(), ["owl-meter-bank-1"])
        self.assertIsNotNone(first.poll())

    def test_channel_count_change_moves_the_tap(self):
        t = self.taps()
        t.want({"a": 2, "b": 2})
        t.want({"a": 2, "b": 4})
        self.assertEqual(t.target("b"), ("owl-meter-bank-0", [2, 3, 4, 5]))

    def test_one_process_for_many_taps(self):
        t = self.taps(ch=64)
        t.want({f"t{i}": 2 for i in range(30)})
        self.assertEqual(len(t.banks), 1)


class Levels(unittest.TestCase):
    def test_each_tap_reads_its_own_channels(self):
        t = M.Taps(bank_ch=8, argv=GEN)
        self.addCleanup(t.stop)
        t.want({"music": 2, "silent": 2, "quiet": 2})
        time.sleep(1.0)
        st = t.stats()
        self.assertAlmostEqual(st["music"]["rms_dbfs"][0], -6.0, delta=0.1)
        self.assertIsNone(st["silent"]["rms_dbfs"][0])
        self.assertFalse(st["silent"]["signal"])
        self.assertAlmostEqual(st["quiet"]["rms_dbfs"][1], -20.0, delta=0.1)
        self.assertTrue(st["music"]["running"])

    def test_wav_header_is_skipped(self):
        import io, struct
        hdr = b"RIFF" + struct.pack("<I", 0xFFFFFFFF) + b"WAVE" + b"fmt " + struct.pack("<I", 16) + b"\0" * 16 + \
              b"LIST" + struct.pack("<I", 3) + b"abc\0" + b"data" + struct.pack("<I", 0xFFFFFFFF)
        f = io.BytesIO(hdr + b"PCM")
        self.assertEqual(M.skip_wav_header(f), b"")
        self.assertEqual(f.read(), b"PCM")
        f = io.BytesIO(b"\x00\x00\x80\x3fmore")                 # raw PCM already: nothing is lost
        self.assertEqual(M.skip_wav_header(f) + f.read(), b"\x00\x00\x80\x3fmore")


RUNTIME = os.environ.get("XDG_RUNTIME_DIR") or ""
HAVE = bool(shutil.which("pipewire")) and os.path.exists(os.path.join(RUNTIME, "pipewire-0")) and \
    bool(subprocess.run(["pgrep", "-x", "wireplumber"], capture_output=True).stdout)


@unittest.skipUnless(HAVE, "needs a PipeWire daemon with WirePlumber (XDG_RUNTIME_DIR)")
class LiveBank(unittest.TestCase):
    def test_tone_lands_in_its_tap_only(self):
        import math, struct, wave
        name = f"owl-test-tone-{os.getpid()}"
        path = os.path.join(tempfile.mkdtemp(prefix="owl-tone-"), "tone.wav")
        with wave.open(path, "wb") as w:                       # 20 s of a 440 Hz sine at -6 dBFS peak, stereo
            w.setnchannels(2), w.setsampwidth(2), w.setframerate(48000)
            one = b"".join(struct.pack("<hh", v, v) for v in (int(16383 * math.sin(2 * math.pi * 440 * i / 48000)) for i in range(4800)))
            w.writeframes(one * 200)
        play = subprocess.Popen(["pw-play", "--target", "0", "-P", f"{{ node.name = {name} node.autoconnect = false }}", path],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        taps = M.Taps()
        ops = GraphOps(taps)
        try:
            taps.want({"spare": 4, "tone": 2})                      # 'tone' lands on channels 5-6 of bank 0
            g, missing = ops.wait_nodes([name] + taps.nodes(), timeout=10)
            self.assertEqual(missing, [])
            self.assertEqual(taps.target("tone"), ("owl-meter-bank-0", [4, 5]))
            owned = []
            miss, created, pending = ops.ensure_links(g, ops.tap_links({"tone": {"node": name, "monitor": False, "ports": None}}), owned)
            self.assertEqual((miss, pending, created), ([], [], 2))
            self.assertEqual(sorted(o["in_port"] for o in owned), ["input_5", "input_6"])
            time.sleep(1.5)
            st = taps.stats()
            self.assertTrue(st["tone"]["signal"], st["tone"])
            self.assertAlmostEqual(st["tone"]["rms_dbfs"][0], -9.0, delta=1.0)    # a sine's RMS: 3 dB under its peak
            self.assertFalse(st["spare"]["signal"], st["spare"])    # the channels next to it hear nothing
        finally:
            taps.stop()
            play.terminate()
            play.wait(5)


if __name__ == "__main__":
    unittest.main()
