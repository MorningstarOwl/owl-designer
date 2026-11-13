"""SAP discovery of third-party AES67 streams (Dante in AES67 mode, ...): parsing, the directory, the inventory's
blocks, the compiler's own-SDP patches and the deployer applying them."""
import os, socket, sys, time, unittest, zlib
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
from owlnode.dsp import sap as SAP  # noqa: E402
from owlnode.dsp.inventory import sap_blocks  # noqa: E402
from owlnode.dsp.compiler import compile_design  # noqa: E402
from test_compiler_senders import FakeInv, TYPES, rx_links  # noqa: E402

DANTE = "\r\n".join([
    "v=0", "o=- 1423986 1423994 IN IP4 192.0.2.77", "s=AVIO-USB-1a2b3c : 2", "c=IN IP4 239.69.83.67/32", "t=0 0",
    "a=keywds:Dante", "m=audio 5004 RTP/AVP 97", "i=2 channels: Left, Right", "a=recvonly", "a=rtpmap:97 L24/48000/2",
    "a=ptime:1", "a=ts-refclk:ptp=IEEE1588-2008:00-1D-C1-FF-FE-12-34-56:0", "a=mediaclk:direct=0", ""])
DANTE8 = DANTE.replace("1423986", "777").replace("AVIO-USB-1a2b3c : 2", "Stagebox : 8ch").replace("L24/48000/2", "L24/48000/8") \
              .replace("239.69.83.67", "239.69.83.68")
OWL = DANTE.replace("AVIO-USB-1a2b3c : 2", "owl-hub").replace("1423986", "42")


class Packets(unittest.TestCase):
    def test_announcement_and_payload_type(self):
        dele, origin, text = SAP.parse_packet(SAP.announce_packet(DANTE, "192.0.2.77"))
        self.assertEqual((dele, origin, text), (False, "192.0.2.77", DANTE))

    def test_no_payload_type_compressed_and_deletion(self):
        raw = bytes([0x21, 0, 0, 7]) + socket.inet_aton("192.0.2.77") + zlib.compress(DANTE.encode())
        self.assertEqual(SAP.parse_packet(raw)[2], DANTE)
        self.assertTrue(SAP.parse_packet(SAP.announce_packet(DANTE, "192.0.2.77", deletion=True))[0])

    def test_garbage_and_encrypted_are_ignored(self):
        self.assertIsNone(SAP.parse_packet(b"\x00\x01"))
        self.assertIsNone(SAP.parse_packet(bytes([0x22, 0, 0, 1]) + socket.inet_aton("192.0.2.1") + b"v=0"))


class DirectoryTests(unittest.TestCase):
    def test_feed_reannounce_delete_expire(self):
        d = SAP.Directory(expire_s=60)
        sid = d.feed(SAP.announce_packet(DANTE, "192.0.2.77"), now=100)
        st = d.get(sid)
        self.assertEqual((st["name"], st["channels"], st["group"], st["error"]), ("AVIO-USB-1a2b3c : 2", 2, "239.69.83.67", None))
        # a new version of the same session (o= version changes) is the same stream, with the new SDP
        v2 = DANTE.replace("1423994", "1423999").replace("239.69.83.67", "239.69.83.99")
        self.assertEqual(d.feed(SAP.announce_packet(v2, "192.0.2.77"), now=110), sid)
        self.assertEqual(d.get(sid)["group"], "239.69.83.99")
        d.feed(SAP.announce_packet(v2, "192.0.2.77", deletion=True))
        self.assertIsNone(d.get(sid))
        d.feed(SAP.announce_packet(DANTE, "192.0.2.77"), now=100)
        d.expire(now=161)
        self.assertEqual(d.list(), [])

    def test_unplayable_stream_is_kept_with_its_reason(self):
        d = SAP.Directory()
        sid = d.feed(SAP.announce_packet(DANTE.replace("L24/48000/2", "AM824/48000/2"), "192.0.2.77"))
        self.assertIn("AM824", d.get(sid)["error"])


def directory(*sdps):
    d = SAP.Directory()
    for x in sdps:
        d.feed(SAP.announce_packet(x, "192.0.2.77"))
    return d


class Blocks(unittest.TestCase):
    def test_owl_streams_are_left_out_and_channel_pairs_offered(self):
        bl = sap_blocks(directory(DANTE, DANTE8, OWL).list(), owl_sessions={"owl-hub"})
        self.assertEqual(sorted(b["label"] for b in bl), ["AVIO-USB-1a2b3c : 2", "Stagebox : 8ch"])
        eight = next(b for b in bl if b["label"] == "Stagebox : 8ch")
        self.assertEqual([p["id"] for p in eight["outputs"]], ["out", "ch1-2", "ch3-4", "ch5-6", "ch7-8"])
        self.assertEqual(eight["outputs"][2]["sap"]["ports"], [2, 3])
        self.assertEqual(sap_blocks(directory(DANTE).list(), owl_ips={"192.0.2.77"}), [])


class Compile(unittest.TestCase):
    def inv(self, *sdps):
        inv = FakeInv()
        inv.blocks += sap_blocks(directory(*sdps).list())
        return inv

    def sid(self, inv, label):
        return next(b["id"] for b in inv.blocks if b.get("label") == label)

    def test_stream_into_a_hub_speaker_patches_a_hub_receiver_with_its_sdp(self):
        inv = self.inv(DANTE)
        d = compile_design({"name": "t", "blocks": [], "wires": [
            {"id": "w", "from": f"{self.sid(inv, 'AVIO-USB-1a2b3c : 2')}|out", "to": "pw:hub:spk|in"}]}, inv, TYPES)
        self.assertEqual(d.patches, {})
        (rid, rp), = d.raw_patches.items()
        self.assertEqual(rid, inv.hub_receivers[0]["id"])
        self.assertEqual((rp["sdp"], rp["wire"]), (DANTE, "w"))
        self.assertEqual(rx_links(d.links), {("aes67-rx-hub-1", ()): "alsa_output.hub"})

    def test_one_channel_pair_of_an_8ch_flow_into_a_satellite(self):
        inv = self.inv(DANTE8)
        b = self.sid(inv, "Stagebox : 8ch")
        d = compile_design({"name": "t", "blocks": [], "wires": [
            {"id": "a", "from": f"{b}|ch3-4", "to": "pw:kitchen:spk|in"},
            {"id": "b", "from": f"{b}|ch7-8", "to": "pw:kitchen:spk|in"}]}, inv, TYPES)
        self.assertEqual(len(d.raw_patches), 1)                 # one kitchen receiver takes the flow once
        self.assertEqual(rx_links(d.shares["kitchen"]["links"]), {("aes67-rx-kitchen-1", (2, 3)): "alsa_output.kitchen",
                                                                  ("aes67-rx-kitchen-1", (6, 7)): "alsa_output.kitchen"})

    def test_receiver_allocation_is_kept_across_deploys(self):
        inv = self.inv(DANTE, DANTE8)
        des = {"name": "t", "blocks": [], "wires": [
            {"id": "a", "from": f"{self.sid(inv, 'Stagebox : 8ch')}|ch1-2", "to": "pw:hub:spk|in"}]}
        first = compile_design(des, inv, TYPES)
        des["wires"].insert(0, {"id": "z", "from": f"{self.sid(inv, 'AVIO-USB-1a2b3c : 2')}|out", "to": "pw:hub:spk|in"})
        again = compile_design(des, inv, TYPES, prev_alloc=first.alloc)
        self.assertEqual(next(r for r, p in again.raw_patches.items() if p["wire"] == "a"), next(iter(first.raw_patches)))


class Deployer(unittest.TestCase):
    def test_raw_patch_uses_the_announced_sdp(self):
        from owlnode.dsp.deploy import Deployer as D
        calls = []
        dep = D.__new__(D)
        dep.state, dep.events = {"raw_patches": {}}, []
        dep.event = dep.events.append
        dep.inv = SimpleNamespace(receivers={"rx1": {"subscription": {"active": False}}})
        dep.ctl = SimpleNamespace(patch_sdp=lambda rid, sdp, tp: (calls.append((rid, sdp, tp)), (200, {}))[1],
                                  patch=lambda rid, sid: (200, {}))
        desired = SimpleNamespace(raw_patches={"rx1": {"sdp": DANTE, "label": "AVIO", "stream": "x", "wire": "w"}}, patches={})
        changes = dep._apply_raw_patches(desired)
        self.assertEqual(calls, [("rx1", DANTE, [{"rtp_enabled": True}])])
        self.assertEqual(len(changes), 1)
        # applied and playing: nothing to do the next time
        dep.state["raw_patches"] = dict(dep._raw_applied)
        dep.inv.receivers["rx1"]["subscription"] = {"active": True, "sender_id": None}
        self.assertEqual(dep._apply_raw_patches(desired), [])


class Listen(unittest.TestCase):
    def test_listener_hears_an_announcement(self):
        lst = SAP.Listener("127.0.0.1", log=lambda *a: None)
        try:
            lst._socket().close()
        except OSError as e:
            self.skipTest(f"no multicast here: {e}")
        lst.start()
        tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        tx.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton("127.0.0.1"))
        tx.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
        try:
            for _ in range(20):
                tx.sendto(SAP.announce_packet(DANTE, "192.0.2.77"), (SAP.SAP_GROUP, SAP.SAP_PORT))
                time.sleep(0.1)
                if lst.dir.list():
                    break
            self.assertEqual([s["name"] for s in lst.dir.list()], ["AVIO-USB-1a2b3c : 2"])
        finally:
            tx.close()
            lst.stop()
            lst.join(3)


if __name__ == "__main__":
    unittest.main()
