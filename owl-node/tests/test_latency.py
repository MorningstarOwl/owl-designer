"""Receiver playout latency from the designer: the design's network latency, a wire's own, one value per stream; the
agent's x-owl endpoint and state.json; the audio role's receive config; the deployer setting and clearing it."""
import json, os, sys, tempfile, threading, unittest, urllib.request
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
TMP = tempfile.mkdtemp(prefix="owl-lat-")

from owlnode import agent as A, audio, config  # noqa: E402
from owlnode.dsp.compiler import CompileError, compile_design  # noqa: E402
from test_compiler_senders import FakeInv, TYPES, design  # noqa: E402
from _owltmp import isolate  # noqa: E402


class CompilerLatency(unittest.TestCase):
    def test_design_latency_for_every_receiver_wire_override_wins(self):
        des = design(("music|out", "pw:kitchen:spk|in"), ("news|out", "pw:bedroom:spk|in"))
        des["latency_ms"] = 10
        des["wires"][1]["latency_ms"] = 40
        d = compile_design(des, FakeInv(), TYPES)
        inv = FakeInv()
        k, b = inv.node_transport["kitchen"]["receivers"][0]["id"], inv.node_transport["bedroom"]["receivers"][0]["id"]
        self.assertEqual(d.rx_latency, {k: 10.0, b: 40.0})
        self.assertIn("receiver playout latency 40 ms", d.wire_impl["w1"])

    def test_no_setting_leaves_node_defaults(self):
        self.assertEqual(compile_design(design(("music|out", "pw:kitchen:spk|in")), FakeInv(), TYPES).rx_latency, {})

    def test_one_stream_one_latency_the_largest(self):
        inv = FakeInv(hub_tx=1, hub_ch=8, sat_rx=1)
        des = design(("music|out", "pw:kitchen:spk|in"), ("news|out", "pw:kitchen:spk|in"))
        des["wires"][0]["latency_ms"], des["wires"][1]["latency_ms"] = 8, 20
        d = compile_design(des, inv, TYPES)
        self.assertEqual(list(d.rx_latency.values()), [20.0])
        self.assertTrue(any("one stream has one latency" in w for w in d.warnings), d.warnings)

    def test_out_of_range_is_an_error(self):
        des = design(("music|out", "pw:kitchen:spk|in"))
        des["latency_ms"] = 900
        with self.assertRaises(CompileError):
            compile_design(des, FakeInv(), TYPES)


class AgentLatency(unittest.TestCase):
    def setUp(self):
        isolate(self)
        path = os.path.join(TMP, "node.yaml")
        with open(path, "w") as f:
            f.write("name: n\ninterfaces: {media: lo}\nclock: {phc_if: none}\naudio:\n  receivers: [{id: rx1}]\n")
        self.agent = A.Agent(config.Node(path))
        A.Handler.agent = self.agent
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), A.Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.rid = self.agent.n.receivers[0]["id"]

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def patch(self, body):
        req = urllib.request.Request(f"http://127.0.0.1:{self.srv.server_address[1]}/x-owl/v1/receivers/{self.rid}",
                                     data=json.dumps(body).encode(), method="PATCH", headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_set_persist_reach_audio_and_clear(self):
        self.assertEqual(self.patch({"latency_ms": 25})[0], 200)
        st = config.read_json(config.STATE)
        self.assertEqual(st["receiver_latency"], {"rx1": 25.0})
        self.assertEqual(self.agent.status()["receivers"][self.rid]["latency_ms"], 25.0)
        r = self.agent.n.receivers[0]
        a = {"transport_params": [{"multicast_ip": "239.69.1.1", "destination_port": 5004}], "format": {}}
        self.assertIn("sess.latency.msec = 25", audio.rx_conf(self.agent.n, r, a, st["receiver_latency"]["rx1"]))
        self.assertIn("sess.latency.msec = 6", audio.rx_conf(self.agent.n, r, a, None))
        code, body = self.patch({"latency_ms": None})
        self.assertEqual((code, body["latency_ms"], body["latency_override"]), (200, 6.0, None))
        self.assertEqual(config.read_json(config.STATE)["receiver_latency"], {})

    def test_bad_values_are_refused(self):
        self.assertEqual(self.patch({"latency_ms": 0})[0], 400)
        self.assertEqual(self.patch({"latency_ms": "10"})[0], 400)
        self.assertEqual(self.patch({"other": 1})[0], 400)


class DeployerLatency(unittest.TestCase):
    def dep(self, overrides, state=None):
        from owlnode.dsp.deploy import Deployer
        d = Deployer.__new__(Deployer)
        d.state, d.calls, d.events = dict(state or {}), [], []
        d.event = d.events.append
        d.inv = SimpleNamespace(nmos={"status": {"node": {"receivers": overrides}}})
        d.ctl = SimpleNamespace(set_receiver_latency=lambda rid, ms: (d.calls.append((rid, ms)), (200, {}))[1])
        return d

    def test_sets_changed_skips_matching_clears_dropped(self):
        d = self.dep({"a": {"latency_override": None}, "b": {"latency_override": 10.0}, "c": {"latency_override": 30.0}},
                     state={"rx_latency": {"c": 30.0}})
        d._apply_latency(SimpleNamespace(rx_latency={"a": 20.0, "b": 10.0}))
        self.assertEqual(sorted(d.calls), [("a", 20.0), ("c", None)])
        self.assertEqual(d.state["rx_latency"], {"a": 20.0, "b": 10.0})

    def test_unreachable_node_is_retried(self):
        d = self.dep({})
        d.ctl = SimpleNamespace(set_receiver_latency=lambda rid, ms: (0, "down"))
        d._apply_latency(SimpleNamespace(rx_latency={"a": 20.0}))
        self.assertEqual(d.state["rx_latency"], {})
        self.assertTrue(d.events)


if __name__ == "__main__":
    unittest.main()
