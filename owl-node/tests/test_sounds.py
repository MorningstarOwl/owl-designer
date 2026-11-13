"""The sound library and the Sound player block: formats by content, safe names, limits; Play/Stop controls (Panels,
HA buttons) and the sound select; the upload endpoint; and against a real PipeWire with WirePlumber (skipped without
one): Play puts the sound on the block's output, Stop silences it."""
import io, json, math, os, shutil, struct, subprocess, sys, tempfile, threading, time, unittest, urllib.request, wave
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
from owlnode.dsp import blocks as B, sounds as S  # noqa: E402
from owlnode.dsp.compiler import CompileError, compile_design  # noqa: E402

TYPES = B.load([os.path.join(os.path.dirname(__file__), "..", "dsp", "blocks")])


def wav_bytes(seconds=0.5, hz=880, amp=0.5):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(48000)
        w.writeframes(b"".join(struct.pack("<h", int(32767 * amp * math.sin(2 * math.pi * hz * i / 48000))) for i in range(int(48000 * seconds))))
    return buf.getvalue()


class Library(unittest.TestCase):
    def setUp(self):
        self.lib = S.Library(tempfile.mkdtemp(prefix="owl-snd-"))

    def test_add_list_delete(self):
        r = self.lib.add("Door Bell!.wav", wav_bytes())
        self.assertEqual((r["name"], r["format"], r["duration_s"]), ("Door Bell.wav", "wav", 0.5))
        self.assertEqual([x["name"] for x in self.lib.list()], ["Door Bell.wav"])
        self.lib.delete("Door Bell.wav")
        self.assertEqual(self.lib.list(), [])

    def test_format_by_content_and_extension_fixed(self):
        self.assertEqual(self.lib.add("chime.mp3", wav_bytes())["name"], "chime.wav")       # it IS a WAV
        self.assertEqual(self.lib.add("x.bin", b"fLaC" + b"\0" * 100)["name"], "x.flac")
        self.assertEqual(S.sniff(b"OggS\0\0"), "ogg")
        self.assertEqual(S.sniff(b"ID3\x04"), "mp3")
        with self.assertRaises(S.SoundError):
            self.lib.add("evil.wav", b"#!/bin/sh\nrm -rf /\n")

    def test_names_cannot_escape_the_library(self):
        r = self.lib.add("../../etc/passwd.wav", wav_bytes())
        self.assertEqual(r["name"], "passwd.wav")
        for bad in ("../x.wav", ".hidden", "a/b.wav", ""):
            with self.assertRaises(S.SoundError):
                self.lib.path(bad)

    def test_limits(self):
        saved = S.MAX_FILE, S.MAX_TOTAL
        try:
            S.MAX_FILE, S.MAX_TOTAL = 30_000, 60_000
            with self.assertRaises(S.SoundError):
                self.lib.add("big.wav", wav_bytes(1.0))          # 96 kB
            self.lib.add("a.wav", wav_bytes(0.25))               # 24 kB
            self.lib.add("b.wav", wav_bytes(0.25))
            with self.assertRaises(S.SoundError):
                self.lib.add("c.wav", wav_bytes(0.25))           # would pass 60 kB
            self.lib.add("a.wav", wav_bytes(0.25))               # replacing one is fine
        finally:
            S.MAX_FILE, S.MAX_TOTAL = saved


class Controls(unittest.TestCase):
    def controls(self, lib, player=None):
        from owlnode.dsp.controls import Controls as C
        dep = SimpleNamespace(design={"name": "t", "blocks": [{"id": "bell", "type": "sound", "label": "Door bell", "params": {"sound": "dong.wav"}}]},
                              state={}, on_change=None, design_error=None, repairs=0)
        c = C(dep, TYPES, SimpleNamespace(nmos={}, error=None), SimpleNamespace(node_id="n", name="hub"))
        c.sounds, c.player = lib, player
        return c

    def test_play_stop_buttons_and_a_sound_select(self):
        lib = S.Library(tempfile.mkdtemp(prefix="owl-snd-"))
        lib.add("dong.wav", wav_bytes())
        lib.add("alarm.wav", wav_bytes())
        calls = []
        player = SimpleNamespace(trigger=lambda bid, action, source="api": (calls.append((bid, action)), {"ok": True})[1])
        c = self.controls(lib, player)
        self.assertEqual(c.resolve("bell.play")["type"], "trigger")
        self.assertEqual(c.resolve("door_bell.stop")["kind"], "trigger")
        snd = c.resolve("bell.sound")
        self.assertEqual((snd["type"], snd["options"], snd["value"]), ("select", ["alarm.wav", "dong.wav"], "dong.wav"))
        c.set("door_bell.play", "PRESS", source="mqtt")
        self.assertEqual(calls, [("bell", "play")])

    def test_library_change_refreshes_the_options(self):
        lib = S.Library(tempfile.mkdtemp(prefix="owl-snd-"))
        c = self.controls(lib)
        self.assertEqual(c.resolve("bell.sound")["options"], [])
        lib.add("new.wav", wav_bytes())
        self.assertEqual(c.resolve("bell.sound")["options"], ["new.wav"])


class HubOnly(unittest.TestCase):
    def test_a_sound_player_on_a_satellite_is_refused(self):
        from test_compiler_senders import FakeInv
        with self.assertRaises(CompileError) as e:
            compile_design({"name": "t", "blocks": [{"id": "bell", "type": "sound", "host": "kitchen"}], "wires": []}, FakeInv(), TYPES)
        self.assertIn("hub only", str(e.exception))
        d = compile_design({"name": "t", "blocks": [{"id": "bell", "type": "sound"}],
                            "wires": [{"id": "w", "from": "bell|out", "to": "pw:kitchen:spk|in"}]}, FakeInv(), TYPES)
        self.assertIn("owl-bell", d.block_nodes["bell"])                     # its sink exists for the player
        self.assertTrue(any(l["src"]["node"] == "owl-bell-out" for l in d.links))   # and its output crosses to the kitchen


class Upload(unittest.TestCase):
    def test_upload_and_fetch_through_the_api(self):
        from owlnode.dsp import server
        lib = S.Library(tempfile.mkdtemp(prefix="owl-snd-"))
        events = []
        server.Handler.sounds = lib
        server.Handler.player = SimpleNamespace(status=lambda: {}, stop_sound=lambda n: None)
        server.Handler.ctl = SimpleNamespace(refresh=lambda force=False: None, version=1, bus=SimpleNamespace(publish=events.append))
        server.Handler.dep = SimpleNamespace(design=None)
        srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}/api/dsp/sounds"
        try:
            data = wav_bytes()
            req = urllib.request.Request(base + "?name=doorbell.wav", data=data, method="POST", headers={"Content-Type": "audio/wav"})
            r = json.loads(urllib.request.urlopen(req).read())
            self.assertEqual((r["name"], r["format"]), ("doorbell.wav", "wav"))
            self.assertEqual(json.loads(urllib.request.urlopen(base).read())["sounds"][0]["name"], "doorbell.wav")
            with urllib.request.urlopen(base + "/doorbell.wav/file") as f:
                self.assertEqual((f.headers["Content-Type"], f.read()), ("audio/wav", data))
            self.assertEqual(events[-1]["kind"], "sounds")                  # Home Assistant's select gets the new option
            bad = urllib.request.Request(base + "?name=x.wav", data=b"hello", method="POST")
            with self.assertRaises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(bad)
            self.assertEqual(e.exception.code, 400)
            urllib.request.urlopen(urllib.request.Request(base + "/doorbell.wav", method="DELETE"))
            self.assertEqual(lib.list(), [])
        finally:
            srv.shutdown()
            srv.server_close()


RUNTIME = os.environ.get("XDG_RUNTIME_DIR") or ""
HAVE = bool(shutil.which("pipewire")) and os.path.exists(os.path.join(RUNTIME, "pipewire-0")) and \
    bool(subprocess.run(["pgrep", "-x", "wireplumber"], capture_output=True).stdout)


@unittest.skipUnless(HAVE, "needs a PipeWire daemon with WirePlumber (XDG_RUNTIME_DIR)")
class LivePlayer(unittest.TestCase):
    def test_play_reaches_the_output_and_stop_silences_it(self):
        from owlnode.dsp import meters as M
        from owlnode.dsp.deploy import GraphOps
        bid = f"bell{os.getpid()}"
        d = tempfile.mkdtemp(prefix="owl-sndp-")
        with open(os.path.join(d, "b.conf"), "w") as f:
            f.write(TYPES["sound"].render_conf(TYPES["sound"].ctx(bid, "Door bell", {"gain_db": 0})))
        blk = subprocess.Popen(["pipewire", "-c", os.path.join(d, "b.conf")], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        lib = S.Library(os.path.join(d, "lib"))
        lib.add("dong.wav", wav_bytes(seconds=30, amp=0.5))
        dep = SimpleNamespace(design={"blocks": [{"id": bid, "type": "sound", "params": {"sound": "dong.wav", "gain_db": 0}}]}, types=TYPES)
        changes = []
        player = S.Player(dep, lib, log=lambda *a: None, on_change=lambda b, playing, src: changes.append(playing))
        taps = M.Taps()
        ops = GraphOps(taps)
        try:
            g, missing = ops.wait_nodes([f"owl-{bid}", f"owl-{bid}-out"], timeout=10)
            self.assertEqual(missing, [])
            taps.want({"out": 2})
            g, _ = ops.wait_nodes(taps.nodes(), timeout=10)
            ops.ensure_links(g, ops.tap_links({"out": {"node": f"owl-{bid}-out", "monitor": False, "ports": None}}), [])
            r = player.trigger(bid, "play")
            self.assertTrue(r["playing"])
            time.sleep(2.5)
            st = taps.stats()["out"]
            self.assertTrue(st["signal"], st)
            self.assertAlmostEqual(st["rms_dbfs"][0], -9.0, delta=2.0)       # a 0.5-amplitude sine: -9 dBFS RMS
            self.assertEqual(player.status()[bid]["sound"], "dong.wav")
            player.trigger(bid, "stop")
            time.sleep(2.0)
            self.assertFalse(taps.stats()["out"]["signal"])
            self.assertEqual(player.status(), {})
            self.assertEqual(changes, [True, False])
        finally:
            player.stop_all()
            taps.stop()
            blk.terminate()
            blk.wait(5)


if __name__ == "__main__":
    unittest.main()
