"""The controller serves the designer page and its code and styles (split out of designer.html), and nothing outside
its web folder."""
import os, sys, threading, unittest, urllib.error, urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from owlnode import controller  # noqa: E402


class Static(unittest.TestCase):
    def setUp(self):
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), controller.Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def get(self, path):
        with urllib.request.urlopen(self.base + path) as r:
            return r.headers["Content-Type"], r.read().decode()

    def test_page_code_and_styles(self):
        ct, html = self.get("/")
        self.assertTrue(ct.startswith("text/html"))
        self.assertIn('src="designer.js"', html)
        self.assertIn('href="designer.css"', html)
        ct, js = self.get("/designer.js")
        self.assertEqual(ct, "application/javascript")
        self.assertIn("function InjectPanel", js)
        self.assertEqual(self.get("/designer.css")[0], "text/css")
        self.assertIn('src="wires.js"', html)
        ct, js = self.get("/wires.js")
        self.assertEqual(ct, "application/javascript")
        self.assertIn("function organize", js)

    def test_nothing_outside_the_web_folder(self):
        for p in ("/vendor/../../controller.py", "/vendor/%2e%2e/%2e%2e/controller.py", "/controller.py"):
            with self.assertRaises(urllib.error.HTTPError) as e:
                self.get(p)
            self.assertEqual(e.exception.code, 404, p)


if __name__ == "__main__":
    unittest.main()
