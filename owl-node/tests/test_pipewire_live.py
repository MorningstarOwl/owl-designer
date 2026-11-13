"""Against a real PipeWire daemon (skipped without one: set XDG_RUNTIME_DIR to a running daemon's runtime dir): live
controls through the long-lived pw-cli land on a real filter-chain, fast; a block that restarts is found again by the next
graph dump."""
import os, shutil, subprocess, sys, tempfile, time, unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from owlnode.dsp import blocks as B, pw  # noqa: E402
from owlnode.dsp.deploy import GraphOps  # noqa: E402

RUNTIME = os.environ.get("XDG_RUNTIME_DIR") or os.environ.get("PIPEWIRE_RUNTIME_DIR") or ""
HAVE = bool(shutil.which("pipewire")) and os.path.exists(os.path.join(RUNTIME, "pipewire-0"))
TYPES = B.load([os.path.join(os.path.dirname(__file__), "..", "dsp", "blocks")])


def gain_values(node_id):
    out = subprocess.run(["pw-cli", "enum-params", str(node_id), "Props"], capture_output=True, text=True, timeout=5).stdout.splitlines()
    return {out[i].split('"')[1]: float(out[i + 1].split()[-1]) for i in range(len(out) - 1)
            if out[i].strip().startswith('String "g') and "Float" in out[i + 1]}


def wait_for(ops, names, timeout=10):
    """Until the nodes exist (ports are left to a session manager: a bare test daemon has no WirePlumber to configure
    them, and live controls do not need them); remembers the ids like a reconcile's dump."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        g = pw.dump()
        if all(g.node(n) for n in names):
            return ops.remember(g)
        time.sleep(0.2)
    raise AssertionError(f"nodes {names} did not appear")


@unittest.skipUnless(HAVE, "no PipeWire daemon (XDG_RUNTIME_DIR/pipewire-0)")
class LiveControls(unittest.TestCase):
    def start_block(self, bid):
        ctx = TYPES["gain"].ctx(bid, "Volume", {})
        path = os.path.join(self.dir, f"{bid}.conf")
        with open(path, "w") as f:
            f.write(TYPES["gain"].render_conf(ctx))
        return subprocess.Popen(["pipewire", "-c", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="owl-pw-")
        self.procs = [self.start_block("tvol")]
        self.addCleanup(self.stop_blocks)
        self.ops = GraphOps(taps=None)
        wait_for(self.ops, ["owl-tvol-in", "owl-tvol-out"])

    def stop_blocks(self):
        if getattr(self, "ops", None):
            self.ops.pwctl.stop()
        for p in self.procs:
            p.terminate()
            p.wait(5)

    def controls(self, db):
        return TYPES["gain"].controls(TYPES["gain"].ctx("tvol", "Volume", {"gain_db": db}))

    def test_values_land_and_are_fast(self):
        t0 = time.perf_counter()
        for i in range(200):
            self.assertEqual(self.ops.send_controls(self.controls(-40 + i * 0.1)), 2)
        dt = (time.perf_counter() - t0) / 200
        self.assertEqual(self.ops.send_controls(self.controls(-6.0)), 2)
        time.sleep(0.5)
        v = gain_values(self.ops.node_ids["owl-tvol-in"])
        self.assertAlmostEqual(v["g0:Gain 1"], 0.501187, places=4)
        self.assertAlmostEqual(v["g1:Gain 1"], 0.501187, places=4)
        self.assertLess(dt, 0.002, f"{dt * 1000:.2f} ms per change")
        self.assertTrue(self.ops.pwctl.running())

    def test_restarted_block_is_controlled_after_the_next_dump(self):
        self.ops.send_controls(self.controls(-3.0))
        self.procs[0].terminate()
        self.procs[0].wait(5)
        self.procs[0] = self.start_block("tvol")
        time.sleep(0.5)
        wait_for(self.ops, ["owl-tvol-in", "owl-tvol-out"])                  # the dump a deploy/reconcile makes
        # (PipeWire recycles ids: the new process may get the old one; either way the cache follows the dump)
        self.ops.send_controls(self.controls(-12.0))
        time.sleep(0.5)
        self.assertAlmostEqual(gain_values(self.ops.node_ids["owl-tvol-in"])["g0:Gain 1"], 10 ** (-12 / 20), places=4)

    def test_a_stale_id_is_reported(self):
        seq = self.ops.pwctl.error_seq
        self.ops.pwctl.set(987654, [("g0:Gain 1", 0.5)])
        time.sleep(0.5)
        self.assertGreater(self.ops.pwctl.error_seq, seq, self.ops.pwctl.status())


if __name__ == "__main__":
    unittest.main()
