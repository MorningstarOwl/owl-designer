"""The designer's wire router and Organize (owlnode/web/wires.js): its own tests are JavaScript, run here under node."""
import os
import shutil
import subprocess
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class WiresTest(unittest.TestCase):
    def test_router_and_organizer(self):
        r = subprocess.run(["node", os.path.join(HERE, "wires.test.js")], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
