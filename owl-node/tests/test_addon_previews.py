"""The HA add-on's designer learns which video previews are live from the hub's dsp engine (/api/dsp/previews).

MediaMTX's API listens on the hub's loopback (127.0.0.1:9997), so the add-on (another machine) could never reach it
and every preview showed "MediaMTX not reachable"."""
import os, sys, threading, unittest
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from owlnode import controller  # noqa: E402
from owlnode.dsp import server  # noqa: E402

LIVE = {"hub-vtx1": {"ready": True, "readers": 1, "tracks": ["H264"], "bytes": 123}}


class AddonPreviews(unittest.TestCase):
    def setUp(self):
        # the engine side: the dsp role's Handler with a Deployer stand-in whose Controller reads MediaMTX
        server.Handler.dep = SimpleNamespace(ctl=SimpleNamespace(previews=lambda: LIVE))
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.saved = controller.ADDON, controller.DSP_API

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        controller.ADDON, controller.DSP_API = self.saved

    def test_addon_asks_the_engine(self):
        controller.ADDON, controller.DSP_API = "remote", f"http://127.0.0.1:{self.srv.server_address[1]}"
        self.assertEqual(controller.Controller("http://registry.invalid:8010").previews(), LIVE)

    def test_addon_with_engine_down_says_unknown(self):
        controller.ADDON, controller.DSP_API = "remote", "http://127.0.0.1:9"
        self.assertIsNone(controller.Controller("http://registry.invalid:8010").previews())


if __name__ == "__main__":
    unittest.main()
