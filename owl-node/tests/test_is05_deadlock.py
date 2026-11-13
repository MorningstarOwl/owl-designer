"""IS-05: concurrent activations of two endpoints of one node must not deadlock.

Before the fix each Endpoint had its own lock and activated while holding it; Agent.persist() then took every other
endpoint's lock, so two activations at the same moment (scheduled activations sharing a requested_time, or two
PATCHes) each held one lock and waited for the other forever. A barrier forces exactly that interleaving."""
import os, sys, tempfile, threading, unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
TMP = tempfile.mkdtemp(prefix="owl-test-")

from owlnode import config  # noqa: E402
from owlnode.agent import Agent  # noqa: E402
from _owltmp import isolate  # noqa: E402

NODE_YAML = """
name: testnode
interfaces: {media: lo}
clock: {phc_if: none}
audio:
  senders: [{id: tx1}]
  receivers: [{id: rx1}, {id: rx2}]
"""


def make_agent():
    path = os.path.join(TMP, "node.yaml")
    with open(path, "w") as f:
        f.write(NODE_YAML)
    return Agent(config.Node(path))


class ConcurrentActivation(unittest.TestCase):
    def test_two_receivers_activate_at_once(self):
        isolate(self)
        a = make_agent()
        rx = [ep for ep in a.eps.values() if ep.kind == "receiver"]
        self.assertEqual(len(rx), 2)
        barrier = threading.Barrier(2, timeout=1.0)
        orig = a.on_activate

        def on_activate(kind, ep):
            try:
                barrier.wait()          # both activations are inside their endpoint's critical section
            except threading.BrokenBarrierError:
                pass                    # serialised (the fix): the other one is not in yet
            orig(kind, ep)

        body = {"master_enable": True, "activation": {"mode": "activate_immediate"}}
        threads = [threading.Thread(target=ep.patch, args=(dict(body), on_activate), daemon=True) for ep in rx]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)
        self.assertFalse(any(t.is_alive() for t in threads), "IS-05 activations deadlocked")
        st = config.read_json(config.STATE)
        self.assertTrue(all(st["receivers"][ep.key]["master_enable"] for ep in rx))


if __name__ == "__main__":
    unittest.main()
