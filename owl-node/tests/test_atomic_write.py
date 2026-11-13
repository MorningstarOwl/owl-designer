"""config.atomic_write: data and directory are fsync'd (a power cut must not leave an empty state file), and a failed
write leaves neither a temp file nor a damaged target."""
import json, os, sys, tempfile, unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from owlnode import config  # noqa: E402


class AtomicWrite(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="owl-aw-")
        self.path = os.path.join(self.dir, "state.json")

    def test_writes_and_fsyncs_file_and_directory(self):
        real = os.fsync
        calls = []
        with mock.patch.object(config.os, "fsync", side_effect=lambda fd: (calls.append(fd), real(fd))):
            config.atomic_write(self.path, {"a": 1})
        self.assertEqual(json.load(open(self.path)), {"a": 1})
        self.assertEqual(len(calls), 2)                    # the file, then its directory

    def test_failed_write_keeps_the_old_file_and_no_temp(self):
        config.atomic_write(self.path, {"old": True})
        with mock.patch.object(config.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                config.atomic_write(self.path, {"new": True})
        self.assertEqual(json.load(open(self.path)), {"old": True})
        self.assertEqual(os.listdir(self.dir), ["state.json"])


if __name__ == "__main__":
    unittest.main()
