"""The hub's backup, kept on every paired node, so a dead hub is replaced in minutes instead of rebuilt by hand.

What a hub holds that exists nowhere else: the deployed design (with its panels and presets), the one before it
(Rollback), the canvas draft, the nodes added by address, the voice assistant's wake-word switch and the MQTT broker
settings. The hub's dsp role pushes that bundle to the dsp role of every satellite it talks to whenever it changes
(PUT /api/dsp/hub-backup there; /var/lib/owl-node/hub-backup.json). A new hub restores it (POST /api/dsp/restore, or
`install.sh --hub --restore-from <node address or file>`): the files are written back, references to the old hub's own
devices are moved to the new hub's name, every node is paired with the new hub and the design is re-applied (devices
the new machine lacks stay pending, greyed on the canvas, until they are re-wired).

Not in it: the MQTT password (the bundle travels to other machines; set it again with --mqtt), the deployer's runtime
ownership records (rebuilt by the first deploy) and anything a node keeps itself (its patches, its share of the design).
"""
import hashlib, json, os, time

from .. import VERSION, config
from ..config import atomic_write, read_json

HUB_BACKUP = os.path.join(config.STATE_DIR, "hub-backup.json")      # on a satellite: the latest bundle of its hub
FORMAT = 1


def _files():
    """name -> path of what the bundle carries (paths resolved at call time: tests point STATE_DIR elsewhere)."""
    d = config.STATE_DIR
    return {"design": os.path.join(d, "dsp-design.json"), "prev": os.path.join(d, "dsp-design.prev.json"),
            "draft": os.path.join(d, "dsp-draft.json"), "nodes": os.path.join(d, "dsp-nodes.json"),
            "voice": os.path.join(d, "dsp-voice.json"), "mqtt": os.path.join(d, "mqtt.json")}


def make(hub_name):
    """The current bundle (None entries: files the hub does not have)."""
    data = {k: read_json(p, None) for k, p in _files().items()}
    if isinstance(data.get("mqtt"), dict):
        data["mqtt"] = {k: v for k, v in data["mqtt"].items() if k != "password"}
    return {"format": FORMAT, "hub": hub_name, "owl_node": VERSION, "made_at": time.time(), "data": data}


def digest(bundle):
    """What changed, ignoring when the bundle was made."""
    return hashlib.sha1(json.dumps({"hub": bundle.get("hub"), "data": bundle.get("data")}, sort_keys=True).encode()).hexdigest()[:12]


def rename_hub(obj, old, new):
    """References to the old hub's own devices and blocks -> the new hub's name: device block ids pw:<old>:<node> and
    nmos:<old> (in wires, layout, device names, panels), and blocks placed on host <old> (now: the hub, no host)."""
    if not old or old == new:
        return obj

    def s(v):
        if v.startswith(f"pw:{old}:"):
            return f"pw:{new}:" + v[len(f"pw:{old}:"):]
        if v == f"nmos:{old}" or v.startswith(f"nmos:{old}|"):
            return f"nmos:{new}" + v[len(f"nmos:{old}"):]
        return v

    def walk(x, key=None):
        if isinstance(x, dict):
            out = {}
            for k, v in x.items():
                if k == "host" and v == old:
                    continue                    # a block placed on the old hub runs on the hub
                out[s(k) if isinstance(k, str) else k] = walk(v, k)
            return out
        if isinstance(x, list):
            return [walk(v) for v in x]
        if isinstance(x, str):
            return s(x)
        return x
    return walk(obj)


def restore(bundle, hub_name):
    """Write a bundle's files on this hub (the old hub's name moved to hub_name). -> summary. The caller re-pairs the
    nodes and re-applies the design."""
    if not isinstance(bundle, dict) or bundle.get("format") != FORMAT or not isinstance(bundle.get("data"), dict):
        raise ValueError(f"not an owl-node hub backup (format {FORMAT})")
    old, data = bundle.get("hub"), bundle["data"]
    written = []
    for k, path in _files().items():
        v = data.get(k)
        if v is None:
            continue
        if k in ("design", "prev", "draft"):
            v = rename_hub(v, old, hub_name)
        if k == "mqtt":
            cur = read_json(path, None) or {}
            v = dict(v, **({"password": cur["password"]} if cur.get("password") else {}))   # keep a password set here
        if k == "nodes" and isinstance(v, dict):
            v = dict(v, nodes=[n for n in v.get("nodes") or [] if n.get("host") != hub_name])  # this machine was a node
        atomic_write(path, v)
        written.append(k)
    return {"from_hub": old, "made_at": bundle.get("made_at"), "owl_node": bundle.get("owl_node"), "restored": written,
            "renamed": bool(old and old != hub_name)}


def store(bundle):
    """On a satellite: keep the hub's latest bundle."""
    if not isinstance(bundle, dict) or bundle.get("format") != FORMAT:
        raise ValueError("not an owl-node hub backup")
    atomic_write(HUB_BACKUP, dict(bundle, stored_at=time.time()))
    return {"ok": True, "digest": digest(bundle)}


def stored():
    return read_json(HUB_BACKUP, None)
