"""The deployer's live-control path: not blocked by the deployer lock (a deploy or reconcile can hold it for seconds),
copy-on-write of the design, one design-file write per burst, and no overwrite of a design a deploy just replaced."""
import os, sys, tempfile, threading, time, unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
from owlnode.dsp import deploy as D, pw  # noqa: E402
from test_compiler_senders import TYPES  # noqa: E402


class FakeOps:
    def __init__(self):
        self.sent = []

    def send_controls(self, controls):
        self.sent.append(list(controls))
        return len(controls)


def deployer():
    d = D.Deployer.__new__(D.Deployer)
    d.cfg = SimpleNamespace(name="hub")
    d.types = TYPES
    d.lock, d.dlock = threading.RLock(), threading.Lock()
    d.design_gen, d._block_locks, d._block_locks_guard = 0, {}, threading.Lock()
    d.design = {"name": "t", "blocks": [{"id": "vol", "type": "gain", "params": {"gain_db": 0}}, {"id": "other", "type": "gain"}]}
    d.desired = SimpleNamespace(block_host={}, procs={}, shares={}, _design=None)
    d.ops, d.sats = FakeOps(), {}
    d.ops.procs = {}
    d.writes = []
    d.persist_design = pw.Debounced(lambda: d.writes.append(d.design), 0.2)
    d.notes = []
    d._notify = lambda kind, **kw: d.notes.append((kind, kw))
    return d


class LivePath(unittest.TestCase):
    def test_not_blocked_by_the_deployer_lock(self):
        d = deployer()
        held, done = threading.Event(), threading.Event()

        def long_reconcile():
            with d.lock:
                held.set()
                done.wait(5)
        t = threading.Thread(target=long_reconcile)
        t.start()
        held.wait()
        t0 = time.time()
        r = d.set_param("vol", "gain_db", -6, persist=False)
        self.assertLess(time.time() - t0, 0.5)
        done.set()
        t.join()
        self.assertEqual(r["controls_set"], 2)
        self.assertAlmostEqual(d.ops.sent[0][0][2], 0.501187, places=5)

    def test_copy_on_write_and_one_write_per_burst(self):
        d = deployer()
        before = d.design
        for i in range(50):
            d.set_param("vol", "gain_db", -i * 0.5)
        self.assertIsNot(d.design, before)
        self.assertEqual(before["blocks"][0]["params"]["gain_db"], 0)            # the old object is untouched
        self.assertEqual(d.design["blocks"][0]["params"]["gain_db"], -24.5)
        self.assertIs(d.design["blocks"][1], before["blocks"][1])                # other blocks are shared, not copied
        time.sleep(0.4)
        self.assertEqual(len(d.writes), 1)
        self.assertEqual(d.writes[0]["blocks"][0]["params"]["gain_db"], -24.5)

    def test_a_deploy_in_between_wins(self):
        d = deployer()
        orig = d.ops.send_controls

        def send_then_deploy(controls):
            with d.dlock:                         # what apply() does while the control is on its way
                d.design = {"name": "new", "blocks": [{"id": "vol", "type": "gain", "params": {"gain_db": -20}}]}
                d.design_gen += 1
            return orig(controls)
        d.ops.send_controls = send_then_deploy
        r = d.set_param("vol", "gain_db", -3)
        self.assertTrue(r.get("superseded"))
        self.assertEqual(d.design["name"], "new")
        self.assertEqual(d.design["blocks"][0]["params"]["gain_db"], -20)

    def test_non_live_param_goes_through_a_deploy(self):
        d = deployer()
        with mock.patch.object(D.Deployer, "apply", return_value={"ok": True, "deployed": True}) as ap:
            d.set_param("vol", "channels", 1)
        ap.assert_called_once()
        self.assertEqual(ap.call_args[0][0]["blocks"][0]["params"]["channels"], 1)
        self.assertEqual(d.ops.sent, [])


if __name__ == "__main__":
    unittest.main()
