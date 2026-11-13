"""The hub's backup bundle: what it carries (never the MQTT password), the old hub's name moved to the new one on
restore, a satellite keeping and serving the latest bundle."""
import json, os, sys, tempfile, threading, unittest, urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from owlnode import config  # noqa: E402
from owlnode.dsp import backup as BK  # noqa: E402

DESIGN = {"name": "Living room", "blocks": [{"id": "vol", "type": "gain", "host": "oldhub"}, {"id": "k", "type": "gain", "host": "kitchen"}],
          "wires": [{"id": "w", "from": "pw:oldhub:alsa_input.mic|out", "to": "vol|in"},
                    {"id": "x", "from": "vol|out", "to": "pw:kitchen:alsa_output.k|in"}],
          "layout": {"pw:oldhub:alsa_input.mic": {"x": 1}}, "devices": {"pw:oldhub:alsa_input.mic": {"label": "Room mic"}},
          "panels": [{"items": [{"key": "vol.gain_db"}]}]}


class Bundle(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="owl-bk-")
        self._saved = config.STATE_DIR
        config.STATE_DIR = self.dir
        config.atomic_write(os.path.join(self.dir, "dsp-design.json"), DESIGN)
        config.atomic_write(os.path.join(self.dir, "dsp-nodes.json"), {"nodes": [{"host": "kitchen", "url": "http://192.0.2.20:8362"},
                                                                                 {"host": "newhub", "url": "http://192.0.2.30:8362"}], "removed": []})
        config.atomic_write(os.path.join(self.dir, "mqtt.json"), {"host": "broker", "username": "ha", "password": "s3cret"})

    def tearDown(self):
        config.STATE_DIR = self._saved

    def test_make_leaves_out_the_mqtt_password(self):
        b = BK.make("oldhub")
        self.assertEqual(b["data"]["design"], DESIGN)
        self.assertEqual(b["data"]["mqtt"], {"host": "broker", "username": "ha"})
        self.assertNotIn("s3cret", json.dumps(b))
        self.assertIsNone(b["data"]["prev"])

    def test_digest_ignores_the_time(self):
        a, b = BK.make("oldhub"), BK.make("oldhub")
        b["made_at"] += 100
        self.assertEqual(BK.digest(a), BK.digest(b))

    def test_restore_moves_the_old_hub_name_and_keeps_a_local_password(self):
        bundle = BK.make("oldhub")
        fresh = tempfile.mkdtemp(prefix="owl-bk2-")
        config.STATE_DIR = fresh
        config.atomic_write(os.path.join(fresh, "mqtt.json"), {"host": "x", "password": "local"})
        r = BK.restore(bundle, "newhub")
        self.assertTrue(r["renamed"])
        d = config.read_json(os.path.join(fresh, "dsp-design.json"))
        self.assertNotIn("host", d["blocks"][0])                       # was on the old hub: now on the hub
        self.assertEqual(d["blocks"][1]["host"], "kitchen")
        self.assertEqual(d["wires"][0]["from"], "pw:newhub:alsa_input.mic|out")
        self.assertEqual(d["wires"][1]["to"], "pw:kitchen:alsa_output.k|in")
        self.assertEqual(list(d["layout"]), ["pw:newhub:alsa_input.mic"])
        self.assertEqual(list(d["devices"]), ["pw:newhub:alsa_input.mic"])
        self.assertEqual(config.read_json(os.path.join(fresh, "mqtt.json")), {"host": "broker", "username": "ha", "password": "local"})
        # the new hub was one of the old hub's nodes: it is not its own node now
        self.assertEqual([n["host"] for n in config.read_json(os.path.join(fresh, "dsp-nodes.json"))["nodes"]], ["kitchen"])

    def test_restore_refuses_other_json(self):
        with self.assertRaises(ValueError):
            BK.restore({"design": {}}, "x")


class SatelliteKeepsIt(unittest.TestCase):
    def test_put_then_get(self):
        from owlnode.dsp import server
        d = tempfile.mkdtemp(prefix="owl-bk3-")
        saved = BK.HUB_BACKUP
        BK.HUB_BACKUP = os.path.join(d, "hub-backup.json")
        srv = ThreadingHTTPServer(("127.0.0.1", 0), server.SatHandler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}/api/dsp/hub-backup"
        try:
            with self.assertRaises(urllib.error.HTTPError):
                urllib.request.urlopen(base)
            bundle = {"format": 1, "hub": "oldhub", "made_at": 1, "data": {"design": DESIGN}}
            req = urllib.request.Request(base, data=json.dumps(bundle).encode(), method="PUT", headers={"Content-Type": "application/json"})
            self.assertEqual(json.loads(urllib.request.urlopen(req).read())["digest"], BK.digest(bundle))
            got = json.loads(urllib.request.urlopen(base).read())
            self.assertEqual((got["hub"], got["data"]["design"]["name"]), ("oldhub", "Living room"))
        finally:
            srv.shutdown()
            srv.server_close()
            BK.HUB_BACKUP = saved


if __name__ == "__main__":
    unittest.main()
