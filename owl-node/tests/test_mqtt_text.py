"""Voice texts to Home Assistant: a sensor state stops at 255 characters (HA rejects a longer one and the entity reads
"unknown"), so the state is shortened and the whole text travels as an attribute."""
import json, os, sys, unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from owlnode.dsp import mqtt as M  # noqa: E402


class Client:
    def __init__(self):
        self.sent = {}

    def publish(self, topic, payload, qos=0, retain=False):
        self.sent[topic] = payload


def bridge():
    b = M.Bridge.__new__(M.Bridge)
    b.base, b.client, b.connected = "owl-dsp/test", Client(), True
    return b


class StateText(unittest.TestCase):
    def test_short_text_is_unchanged(self):
        self.assertEqual(M.ha_state_text("Turned on the media player"), "Turned on the media player")
        self.assertEqual(M.ha_state_text(None), "")
        self.assertEqual(M.ha_state_text("x" * 255), "x" * 255)

    def test_long_text_fits_with_an_ellipsis(self):
        s = M.ha_state_text("word " * 80)
        self.assertLessEqual(len(s), 255)
        self.assertTrue(s.endswith("…"))


class PublishVoice(unittest.TestCase):
    def test_long_reply_state_fits_and_the_attribute_has_it_all(self):
        b = bridge()
        reply = "I don't have a way to search inside YouTube from here. " * 6      # ~330 characters, like the real one
        b.publish_voice({"state": "wake", "last_text": "search youtube", "last_reply": reply, "ha": "connected"})
        st = b.client.sent["owl-dsp/test/control/voice.last_reply/state"]
        self.assertLessEqual(len(st), M.HA_STATE_MAX)
        attrs = json.loads(b.client.sent["owl-dsp/test/control/voice.last_reply/attributes"])
        self.assertEqual(attrs, {"text": reply, "length": len(reply), "truncated": True})
        heard = json.loads(b.client.sent["owl-dsp/test/control/voice.last_text/attributes"])
        self.assertEqual(heard["text"], "search youtube")
        self.assertFalse(heard["truncated"])
        self.assertNotIn("owl-dsp/test/control/voice.state/attributes", b.client.sent)
        self.assertEqual(b.client.sent["owl-dsp/test/control/voice.state/state"], "wake")


class Discovery(unittest.TestCase):
    def test_text_sensors_carry_an_attributes_topic(self):
        b = bridge()
        b.node_id = "owl_dsp_test"
        b.device = lambda: {"identifiers": ["owl-dsp-test"]}
        voice = [{"kind": "voice", "key": f"voice.{f}", "label": f, "type": "text", "field": f}
                 for f in ("state", "last_text", "last_reply")]
        b.ctl = type("Ctl", (), {"listing": lambda self: {"controls": voice, "meters": [], "presets": []}})()
        cfg = {o: c for o, comp, c in b.discovery() if comp == "sensor"}
        self.assertEqual(cfg["voice_last_reply"]["json_attributes_topic"], "owl-dsp/test/control/voice.last_reply/attributes")
        self.assertIn("json_attributes_topic", cfg["voice_last_text"])
        self.assertNotIn("json_attributes_topic", cfg["voice_state"])


if __name__ == "__main__":
    unittest.main()
