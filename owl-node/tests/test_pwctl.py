"""PwCtl against a fake pw-cli (no PipeWire needed): a fresh pw-cli learns the graph from registry events that arrive
after it starts reading commands, so the first set-param to a node must wait until pw-cli knows it (2026-11-14: a preset
recalled right after a hub boot was dropped with "unknown global" while the design recorded it)."""
import os, sys, tempfile, textwrap, time, unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from owlnode.dsp import pw  # noqa: E402

FAKE = textwrap.dedent('''
    import sys, time
    log, late, known = sys.argv[1], int(sys.argv[2]), set(int(x) for x in sys.argv[3].split(",") if x)
    t0 = time.time()
    def knows(i):
        return i in known and time.time() - t0 >= late / 1000
    print("Welcome to PipeWire version 1.6.2. Type 'help' for usage.", flush=True)
    print('Error: "unsupported type PipeWire:Interface:Profiler"', flush=True)
    for line in sys.stdin:
        cmd = line.split()
        if not cmd:
            continue
        i = int(cmd[1])
        if cmd[0] == "info":
            print(f"\\tid: {i}" if knows(i) else f'Error: "info: unknown global \\'{i}\\'"', flush=True)
        elif cmd[0] == "set-param":
            if knows(i):
                with open(log, "a") as f:
                    f.write(line)
                print("Object: size 24, type Spa:Pod:Object:Param:Props (262146)", flush=True)
            else:
                print(f'Error: "set-param: unknown global \\'{i}\\'"', flush=True)
''')


class FreshPwCli(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="owl-pwctl-")
        self.fake = os.path.join(self.dir, "fake_pw_cli.py")
        with open(self.fake, "w") as f:
            f.write(FAKE)
        self.log = os.path.join(self.dir, "sent.log")

    def ctl(self, late_ms, known="105,107"):
        c = pw.PwCtl(argv=(sys.executable, self.fake, self.log, str(late_ms), known))
        self.addCleanup(c.stop)
        return c

    def sent(self):
        try:
            with open(self.log) as f:
                return f.read().splitlines()
        except FileNotFoundError:
            return []

    def test_first_control_waits_for_the_registry(self):
        c = self.ctl(late_ms=200)
        self.assertTrue(c.set(105, [("mL:Gain 1", 1.0), ("mL:Gain 2", 0.0)]))
        time.sleep(0.2)
        self.assertEqual(self.sent(), ['set-param 105 Props { params = [ "mL:Gain 1" 1 "mL:Gain 2" 0 ] }'])
        self.assertEqual(c.error_seq, 0, c.status())       # the probe's own "unknown global" is not an error
        self.assertEqual(c.status()["probe_waits"], 1)

    def test_a_known_node_is_probed_once(self):
        c = self.ctl(late_ms=0)
        for v in (0.1, 0.2, 0.3):
            self.assertTrue(c.set(107, [("g0:Gain 1", v)]))
        self.assertTrue(c.set(105, [("mL:Gain 1", 1.0)]))
        time.sleep(0.2)
        self.assertEqual(len(self.sent()), 4)
        self.assertEqual(c.probes, 2)

    def test_a_node_pw_cli_never_learns_goes_to_the_one_shot(self):
        c = self.ctl(late_ms=0)
        c.PROBE_S = 0.3
        seq = c.error_seq
        # the one-shot is the real pw-cli: there is no node 987654 on any daemon (or no daemon at all), so it fails
        self.assertFalse(c.set(987654, [("g0:Gain 1", 0.5)]))
        self.assertGreater(c.error_seq, seq, c.status())
        self.assertEqual(self.sent(), [])


if __name__ == "__main__":
    unittest.main()
