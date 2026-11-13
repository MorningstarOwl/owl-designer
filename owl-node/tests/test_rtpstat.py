"""The AES67 wire monitor reads levels per channel of the stream's real format (it assumed L24 stereo, so an 8-channel
flow showed nonsense levels)."""
import os, struct, sys, unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from owlnode.rtpstat import Watch  # noqa: E402


def packet(seq, frames):
    """RTP (PT 97) + L24 frames: frames = [[sample per channel], ...] as floats."""
    hdr = struct.pack("!BBHII", 0x80, 97, seq, seq * 48, 0x1234)
    body = b"".join(int(v * 8388607).to_bytes(3, "big", signed=True) for fr in frames for v in fr)
    return hdr + body


class Levels(unittest.TestCase):
    def watch(self, channels):
        try:
            return Watch("239.255.42.42", 50042, "127.0.0.1", channels=channels)
        except OSError as e:
            self.skipTest(f"no multicast here: {e}")

    def test_8ch_levels_per_channel(self):
        w = self.watch(8)
        try:
            amp = [0.5, 0, 0.25, 0, 0, 0, 0.125, 0]       # a constant per channel: its RMS is that value
            for seq in range(10):
                w.feed(packet(seq, [amp] * 48), ("192.0.2.1", 5004))
            w.roll()
            lvl = w.stats["rms_dbfs"]
            self.assertEqual(len(lvl), 8)
            self.assertAlmostEqual(lvl[0], -6.0, delta=0.1)
            self.assertAlmostEqual(lvl[2], -12.0, delta=0.1)
            self.assertAlmostEqual(lvl[6], -18.1, delta=0.1)
            self.assertIsNone(lvl[1])
            self.assertEqual(w.stats["lost"], 0)
        finally:
            w.close()


if __name__ == "__main__":
    unittest.main()
