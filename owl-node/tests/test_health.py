"""Health issues, Prometheus metrics and the deploy history."""
import os, sys, tempfile, unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from owlnode.dsp import health as HL  # noqa: E402


def status(port_state="SLAVE", offset=120, lost=0, pps=1000, active=True, ref="ptp", error=None):
    if error:
        return {"error": error}
    return {"name": "kitchen", "label": "Kitchen", "registered": True, "clock": {"ref_type": ref},
            "ptp": {"port_state": port_state, "offset_ns": offset},
            "receivers": {"rx-1": {"key": "rx1", "master_enable": active, "latency_ms": 6,
                                   "wire": {"pkts_per_s": pps, "lost_total": lost}}}}


class Proc:
    def __init__(self, restarts=0, running=True):
        self.total_restarts, self._running = restarts, running

    def running(self):
        return self._running


def world(st, procs=None, sats=None, design_error=None):
    inv = SimpleNamespace(nmos={"nodes": [{"id": "n1", "label": "Kitchen"}], "status": {"n1": st}}, error=None, sats=sats or {})
    dep = SimpleNamespace(design={"name": "x"}, design_error=design_error, procs=procs or {}, repairs=3, ops=None)
    return dep, inv


class Issues(unittest.TestCase):
    def kinds(self, h):
        return sorted(i["kind"] for i in h["issues"])

    def test_all_good(self):
        dep, inv = world(status())
        self.assertTrue(HL.Health(dep, inv).check()["ok"])

    def test_ptp_problems(self):
        for st in (status(port_state="LISTENING"), status(offset=50_000), status(port_state=None)):
            dep, inv = world(st)
            self.assertEqual(self.kinds(HL.Health(dep, inv).check()), ["ptp"], st)
        dep, inv = world(status(port_state=None, ref="internal"))          # a system-clock test node: fine
        self.assertTrue(HL.Health(dep, inv).check()["ok"])

    def test_loss_is_a_rate_not_a_total(self):
        dep, inv = world(status(lost=500))
        h = HL.Health(dep, inv)
        self.assertTrue(h.check(now=1000)["ok"])                             # old losses: not news
        inv.nmos["status"]["n1"] = status(lost=512)
        r = h.check(now=1010)
        self.assertEqual(self.kinds(r), ["loss"])
        self.assertIn("12 packet(s) lost", r["issues"][0]["text"])
        self.assertTrue(h.check(now=1100)["ok"])                             # a quiet minute later: gone

    def test_silent_receiver_restarts_unreachable_and_design(self):
        dep, inv = world(status(pps=0), procs={"vol": Proc(2)}, sats={"deck": SimpleNamespace(ok=False)}, design_error="boom")
        h = HL.Health(dep, inv)
        h.check(now=0)
        dep.procs["vol"].total_restarts = 3
        self.assertEqual(self.kinds(h.check(now=5)), ["design", "node", "restart", "silent"])

    def test_parked_receiver_is_not_silent(self):
        dep, inv = world(status(pps=0, active=False))
        self.assertTrue(HL.Health(dep, inv).check()["ok"])

    def test_node_down(self):
        dep, inv = world(status(error="timed out"))
        self.assertEqual(self.kinds(HL.Health(dep, inv).check()), ["node"])


class Metrics(unittest.TestCase):
    def test_exposition(self):
        dep, inv = world(status(offset=-250, lost=7), procs={"vol": Proc(1)})
        taps = SimpleNamespace(stats=lambda: {"vol-out": {"rms_dbfs": [-20.0, -18.5]}})
        text = HL.metrics(dep, inv, taps, HL.Health(dep, inv), "0.9.1")
        for line in ('owl_info{version="0.9.1"} 1', 'owl_ptp_locked{node="kitchen"} 1', 'owl_ptp_offset_ns{node="kitchen"} -250',
                     'owl_rx_lost_packets_total{node="kitchen",receiver="rx1"} 7', 'owl_block_restarts_total{block="vol"} 1',
                     'owl_meter_rms_dbfs{tap="vol-out"} -18.5', "owl_health_ok 1", "# TYPE owl_rx_lost_packets_total counter"):
            self.assertIn(line, text)
        self.assertEqual(text.count("# HELP owl_rx_active"), 1)


class History(unittest.TestCase):
    def test_keeps_the_last_n_and_reads_back(self):
        from owlnode.dsp import deploy as D
        d = tempfile.mkdtemp(prefix="owl-hist-")
        saved = D.HISTORY, D.HISTORY_KEEP
        D.HISTORY, D.HISTORY_KEEP = d, 3
        try:
            dep = D.Deployer.__new__(D.Deployer)
            dep.event = lambda m: None
            for i in range(5):
                dep._remember({"name": f"d{i}", "blocks": [{}] * i, "wires": []}, "deploy")
            h = dep.history()
            self.assertEqual([x["name"] for x in h], ["d4", "d3", "d2"])
            self.assertEqual(dep.history_item(h[1]["id"])["design"]["name"], "d3")
            with self.assertRaises(D.DeployError):
                dep.history_item("../../etc/passwd")
        finally:
            D.HISTORY, D.HISTORY_KEEP = saved


if __name__ == "__main__":
    unittest.main()
