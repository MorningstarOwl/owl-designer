"""Cross-host wires: each distinct source gets its own AES67 sender from the host's pool.

Before the fix every cross-host wire of a host rode that host's FIRST sender, so two different hub outputs going to
two rooms were summed into one stream (the compiler only warned). Sharing (mixing) is now the fallback for an
exhausted pool or a far end without a free receiver, and the allocation is kept across deploys."""
import os, sys, unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from owlnode.dsp import blocks as B  # noqa: E402
from owlnode.dsp.compiler import CompileError, compile_design  # noqa: E402

TYPES = B.load([os.path.join(os.path.dirname(__file__), "..", "dsp", "blocks")])
HUB = "hub"


def nmos_tx(i, host, node):
    return {"id": f"00000000-0000-4000-8000-{host[:4].encode().hex()}{i:04x}", "label": f"{host} TX {i}", "pw_node": node,
            "tags": {"urn:x-owl:pipewire-node": [node]}, "subscription": {"active": True}}


def nmos_rx(i, host, node):
    return {"id": f"00000000-0000-4000-9000-{host[:4].encode().hex()}{i:04x}", "label": f"{host} RX {i}", "pw_node": node,
            "tags": {"urn:x-owl:pipewire-node": [node]}, "subscription": {"active": False}}


class FakeInv:
    """What compile_design reads from the Inventory: the hub's transport, satellites' transport and device blocks."""

    def __init__(self, hub_tx=2, sat_rx=2, sats=("kitchen", "bedroom")):
        self.cfg = SimpleNamespace(name=HUB)
        self.sats = {h: object() for h in sats}
        self.hub_senders = [nmos_tx(i + 1, HUB, f"aes67-tx-hub{'' if i == 0 else f'-{i + 1}'}") for i in range(hub_tx)]
        self.hub_receivers = [nmos_rx(i + 1, HUB, f"aes67-rx-hub-{i + 1}") for i in range(2)]
        self.node_transport = {h: {"senders": [nmos_tx(1, h, f"aes67-tx-{h}")],
                                   "receivers": [nmos_rx(i + 1, h, f"aes67-rx-{h}-{i + 1}") for i in range(sat_rx)]}
                               for h in sats}
        self.blocks = []
        for h in sats:
            self.blocks.append({"id": f"pw:{h}:spk", "host": h, "label": f"{h} speaker", "class": "sink",
                                "inputs": [{"id": "in", "label": "in", "media": "audio", "channels": 2,
                                            "pw": {"node": f"alsa_output.{h}", "dir": "in"}}], "outputs": []})
            self.blocks.append({"id": f"pw:{h}:mic", "host": h, "label": f"{h} mic", "class": "source", "inputs": [],
                                "outputs": [{"id": "out", "label": "out", "media": "audio", "channels": 2,
                                             "pw": {"node": f"alsa_input.{h}", "dir": "out", "monitor": False}}]})
        self.blocks.append({"id": f"pw:{HUB}:spk", "host": HUB, "label": "hub speaker", "class": "sink",
                            "inputs": [{"id": "in", "label": "in", "media": "audio", "channels": 2,
                                        "pw": {"node": "alsa_output.hub", "dir": "in"}}], "outputs": []})

    def block(self, bid):
        return next((b for b in self.blocks if b["id"] == bid), None)

    def port(self, bid, pid):
        b = self.block(bid)
        if not b:
            return None, None
        for p in b["inputs"]:
            if p["id"] == pid:
                return b, dict(p, dir="in")
        for p in b["outputs"]:
            if p["id"] == pid:
                return b, dict(p, dir="out")
        return b, None


def design(*wires, blocks=("music", "news")):
    return {"name": "t", "blocks": [{"id": b, "type": "gain"} for b in blocks],
            "wires": [{"id": f"w{i}", "from": f, "to": t} for i, (f, t) in enumerate(wires)]}


def tx_links(d):
    """{hub sender node: {source nodes}}"""
    out = {}
    for l in d.links:
        if l["dst"]["node"].startswith("aes67-tx-"):
            out.setdefault(l["dst"]["node"], set()).add(l["src"]["node"])
    return out


class SenderAllocation(unittest.TestCase):
    def test_two_sources_to_two_rooms_get_two_senders(self):
        d = compile_design(design(("music|out", "pw:kitchen:spk|in"), ("news|out", "pw:bedroom:spk|in")), FakeInv(), TYPES)
        self.assertEqual(tx_links(d), {"aes67-tx-hub": {"owl-music-out"}, "aes67-tx-hub-2": {"owl-news-out"}})
        self.assertFalse([w for w in d.warnings if "sum" in w], d.warnings)
        # each satellite's receiver is patched to the sender carrying its own source
        rx_k, rx_b = FakeInv().node_transport["kitchen"]["receivers"][0], FakeInv().node_transport["bedroom"]["receivers"][0]
        self.assertNotEqual(d.patches[rx_k["id"]], d.patches[rx_b["id"]])

    def test_one_source_to_two_rooms_shares_one_sender(self):
        d = compile_design(design(("music|out", "pw:kitchen:spk|in"), ("music|out", "pw:bedroom:spk|in")), FakeInv(), TYPES)
        self.assertEqual(tx_links(d), {"aes67-tx-hub": {"owl-music-out"}})
        self.assertEqual(len(set(d.patches.values())), 1)

    def test_two_sources_into_one_room_use_two_receivers(self):
        d = compile_design(design(("music|out", "pw:kitchen:spk|in"), ("news|out", "pw:kitchen:spk|in")), FakeInv(), TYPES)
        self.assertEqual(len(tx_links(d)), 2)
        self.assertEqual(len(d.patches), 2)                 # both kitchen receivers, one per stream

    def test_exhausted_pool_falls_back_to_mixing_with_a_warning(self):
        d = compile_design(design(("music|out", "pw:kitchen:spk|in"), ("news|out", "pw:bedroom:spk|in")), FakeInv(hub_tx=1), TYPES)
        self.assertEqual(tx_links(d), {"aes67-tx-hub": {"owl-music-out", "owl-news-out"}})
        self.assertTrue(any("shares AES67 sender" in w and "install.sh --tx" in w for w in d.warnings), d.warnings)

    def test_no_free_receiver_at_far_end_mixes_into_what_it_already_receives(self):
        d = compile_design(design(("music|out", "pw:kitchen:spk|in"), ("news|out", "pw:kitchen:spk|in")), FakeInv(sat_rx=1), TYPES)
        self.assertEqual(tx_links(d), {"aes67-tx-hub": {"owl-music-out", "owl-news-out"}})
        self.assertEqual(len(d.patches), 1)
        self.assertTrue(any("no free AES67 receiver" in w for w in d.warnings), d.warnings)

    def test_allocation_is_kept_across_deploys(self):
        inv = FakeInv()
        first = compile_design(design(("news|out", "pw:bedroom:spk|in")), inv, TYPES)
        self.assertEqual(tx_links(first), {"aes67-tx-hub": {"owl-news-out"}})
        # music is added and sorts first: news must stay on the sender it had, music takes the other one
        again = compile_design(design(("music|out", "pw:kitchen:spk|in"), ("news|out", "pw:bedroom:spk|in")), inv, TYPES,
                               prev_tx_alloc=first.tx_alloc)
        self.assertEqual(tx_links(again), {"aes67-tx-hub": {"owl-news-out"}, "aes67-tx-hub-2": {"owl-music-out"}})

    def test_satellite_to_satellite_and_to_hub(self):
        inv = FakeInv()
        inv.node_transport["kitchen"]["senders"].append(nmos_tx(2, "kitchen", "aes67-tx-kitchen-2"))
        d = compile_design({"name": "t", "blocks": [{"id": "kgain", "type": "gain", "host": "kitchen"}],
                            "wires": [{"id": "a", "from": "pw:kitchen:mic|out", "to": "pw:hub:spk|in"},
                                      {"id": "b", "from": "kgain|out", "to": "pw:bedroom:spk|in"}]}, inv, TYPES)
        share = {l["dst"]["node"]: l["src"]["node"] for l in d.shares["kitchen"]["links"] if l["dst"]["node"].startswith("aes67-tx-")}
        self.assertEqual(share, {"aes67-tx-kitchen": "alsa_input.kitchen", "aes67-tx-kitchen-2": "owl-kgain-out"})

    def test_no_sender_is_an_error(self):
        with self.assertRaises(CompileError) as e:
            compile_design(design(("music|out", "pw:kitchen:spk|in")), FakeInv(hub_tx=0), TYPES)
        self.assertIn("no AES67 sender", str(e.exception))


if __name__ == "__main__":
    unittest.main()
