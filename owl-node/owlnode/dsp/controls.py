"""Named controls (D2): every block parameter and every meter of the deployed design under a stable name, readable and
settable from one place, with change notifications; presets (named snapshots of chosen controls, recalled with an
optional fade); panels (pages of controls) stored with the design.

Two handles per control:
  key    "<block id>.<param>"            stable: panels, presets and HA unique_ids use it; survives a block rename
  name   "<slug of block label>.<param>" what people type (Q-SYS style: "living_room_volume.gain_db"); renaming a
                                         block changes it, and the old name keeps resolving through an alias until the
                                         next deploy (the deployer records label changes in its state)
Meters: "<block>.<port>.level" (every block output) and "<meter block>.level" (Meter blocks), value = max channel RMS.
Presets: "presets.recall" (select) and "preset.<slug>.recall" (trigger). System: design.name, design.state, ptp.state,
ptp.offset_ns, registry.nodes (read-only).

Everything that changes a value goes through Controls.set(): the designer (HTTP/WebSocket), MQTT, presets. The Bus
fans each change out to every subscriber (WebSocket sessions, the MQTT bridge)."""
import queue, threading, time

from . import blocks as B
from .deploy import DeployError

FADE_STEP_S = 0.01          # 10 ms steps: a step is a line to the long-lived pw-cli (pw.PwCtl), so smooth is cheap


class Bus:
    def __init__(self):
        self.subs, self.lock = [], threading.Lock()

    def subscribe(self):
        q = queue.Queue(maxsize=500)
        with self.lock:
            self.subs.append(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            if q in self.subs:
                self.subs.remove(q)

    def publish(self, ev):
        ev = dict(ev, ts=round(time.time(), 3))
        with self.lock:
            subs = list(self.subs)
        for q in subs:
            try:
                q.put_nowait(ev)
            except queue.Full:
                pass

    def count(self):
        return len(self.subs)


def control_name(label, bid, param):
    return f"{B.slug(label or bid)}.{param}"


class Controls:
    def __init__(self, dep, types, inv, cfg):
        self.dep, self.types, self.inv, self.cfg = dep, types, inv, cfg
        self.bus = Bus()
        self.lock = threading.RLock()
        self.fades = {}                    # key -> Fade thread
        self.last_preset = None
        self.version = 0                   # bumps when the set of controls changes (design deployed/edited)
        self.voice = None                  # D3: the PTT voice satellite (voice.py), set by the server in hub mode
        self._design_sig = None
        self.health = None                 # health.Health, set by the server (hub mode)
        self.sounds = None                 # sounds.Library and sounds.Player, set by the server (hub mode)
        self.player = None
        self._cache = None
        dep.on_change = self._on_deployer_change

    # ------------------------------------------------------------------ building
    def _on_deployer_change(self, kind, **info):
        """The deployer calls this after apply/rollback/restore and on every param it sets."""
        if kind == "param":
            c = self.resolve(f"{info['block']}.{info['param']}")
            if c:
                self.bus.publish({"type": "control", "key": c["key"], "name": c["name"], "value": c["value"],
                                  "source": info.get("source", "api")})
        else:
            self.refresh(force=True)
            self.bus.publish({"type": "design", "name": (self.dep.design or {}).get("name"), "kind": kind,
                              "version": self.version})

    def refresh(self, force=False):
        with self.lock:
            d = self.dep.design or {}
            # the shape of the controls (values are read live, _live): a live change replaces the design object, which
            # must not rebuild the listing and bump the version at every fader step
            sig = (tuple(self.sounds.names()) if self.sounds else (), len(d.get("blocks", [])), tuple(sorted(f"{b['id']}:{b.get('label')}:{b.get('type')}" for b in d.get("blocks", []))),
                   len(d.get("presets") or []), tuple(p.get("name") for p in d.get("presets") or []))
            if not force and self._cache is not None and sig == self._design_sig:
                return self._cache
            self._design_sig = sig
            self.version += 1
            by_key, by_name, meters = {}, {}, {}
            names_seen = {}
            for b in d.get("blocks", []):
                bt = self.types.get(b.get("type"))
                if not bt:
                    continue
                params = bt.coerce(b.get("params"))
                lbl = b.get("label") or bt.label
                base = B.slug(lbl)
                if base in names_seen and names_seen[base] != b["id"]:
                    base = f"{base}_{B.slug(b['id'])}"          # two blocks with the same label
                names_seen[base] = b["id"]
                for pid, spec in bt.params.items():
                    if spec.get("secret") or spec.get("type") == "text":
                        continue                         # an RTSP URL (credentials): never a control or an HA entity
                    key = f"{b['id']}.{pid}"
                    c = {"key": key, "name": f"{base}.{pid}", "block": b["id"], "block_label": lbl, "block_type": bt.type,
                         "param": pid, "label": spec.get("label") or pid, "type": spec.get("type", "number"),
                         "live": bool(spec.get("live")), "value": params.get(pid), "kind": "param"}
                    if c["type"] == "sound":             # which library sound: a select of the library (HA select too)
                        c["type"], c["options"] = "select", self.sounds.names() if self.sounds else []
                    for k in ("min", "max", "step", "unit", "options", "help"):
                        if k in spec:
                            c[k] = spec[k]
                    if c["type"] == "number" and "unit" not in c and pid.endswith("_db"):
                        c["unit"] = "dB"
                    by_key[key] = c
                    by_name[c["name"]] = c
                for t in bt.raw.get("triggers") or []:   # a Sound player's Play / Stop: buttons
                    key = f"{b['id']}.{t['id']}"
                    c = {"key": key, "name": f"{base}.{t['id']}", "block": b["id"], "block_label": lbl, "block_type": bt.type,
                         "label": t.get("label") or t["id"], "type": "trigger", "live": True, "kind": "trigger", "action": t["id"]}
                    by_key[key] = c
                    by_name[c["name"]] = c
                ports = bt.ports(params)
                if bt.engine == "voice":
                    pass                                  # no audio of its own to meter (voice.* controls cover it)
                elif bt.engine == "meter":
                    key = f"{b['id']}.level"
                    meters[key] = {"key": key, "name": f"{base}.level", "block": b["id"], "block_label": lbl, "label": "level",
                                   "tap": b["id"], "kind": "meter", "type": "meter", "unit": "dBFS"}
                else:
                    for p in ports["outputs"]:
                        if p.get("media", "audio") != "audio":
                            continue                     # a video output (RTSP block) has no level meter
                        key = f"{b['id']}.{p['id']}.level"
                        meters[key] = {"key": key, "name": f"{base}.{p['id']}.level", "block": b["id"], "block_label": lbl,
                                       "label": f"{p['label']} level", "tap": f"{b['id']}-{p['id']}", "kind": "meter",
                                       "type": "meter", "unit": "dBFS"}
            presets = d.get("presets") or []
            pc = {}
            if presets:
                pc["presets.recall"] = {"key": "presets.recall", "name": "presets.recall", "label": "Preset", "type": "select",
                                        "options": [p["name"] for p in presets], "value": self.last_preset, "kind": "preset", "live": True}
                for p in presets:
                    k = f"preset.{B.slug(p['name'])}.recall"
                    pc[k] = {"key": k, "name": k, "label": f"Recall {p['name']}", "type": "trigger", "preset": p["id"], "kind": "preset", "live": True}
            for m in meters.values():
                by_name.setdefault(m["name"], m)
            vc = {}
            if self.voice is not None:         # D3: push-to-talk voice (no block: the satellite lives in the dsp role)
                for key, label, typ, live, help_ in (
                        ("voice.ptt", "Push to talk", "trigger", True, "Opens the AEC's clean mic to Assist until the utterance ends"),
                        ("voice.ptt_hold", "Talk (hold)", "bool", True, "Mic open while on; off closes it"),
                        ("voice.wake_word", "Wake word", "bool", True, "Listens for the pipeline's wake word (chosen in HA: Settings > Voice assistants > the pipeline); persists across restarts; MIC LIVE while on"),
                        ("voice.state", "Voice state", "text", False, "idle, listening, processing, responding"),
                        ("voice.mic_live", "Mic live", "bool", False, "True only inside a PTT window"),
                        ("voice.last_text", "Heard", "text", False, ""), ("voice.last_reply", "Reply", "text", False, ""),
                        ("voice.ha", "Assist link", "text", False, "Home Assistant's Wyoming connection")):
                    vc[key] = {"key": key, "name": key, "label": label, "type": typ, "kind": "voice", "live": live, "block": "voice",
                               "block_label": "Voice", "field": key.split(".", 1)[1], "help": help_, "readonly": not live}
            aliases = dict((self.dep.state.get("aliases") or {}))
            self._cache = {"by_key": by_key, "by_name": by_name, "meters": meters, "presets": pc, "voice": vc, "aliases": aliases}
            return self._cache

    # ------------------------------------------------------------------ reading
    def _live(self, c):
        """A copy of a control with its current value (the cache holds the shape, the deployed design the values)."""
        if not c:
            return None
        if c["kind"] == "param":
            b = next((x for x in (self.dep.design or {}).get("blocks", []) if x["id"] == c["block"]), None)
            bt = self.types.get(c.get("block_type"))
            if b is not None and bt:
                return dict(c, value=bt.coerce(b.get("params")).get(c["param"]))
        elif c["kind"] == "preset" and c["type"] == "select":
            return dict(c, value=self.last_preset)
        elif c["kind"] == "voice" and self.voice is not None:
            st = self.voice.status()
            f = c["field"]
            return dict(c, value=st.get("hold") if f == "ptt_hold" else st.get("wake") if f == "wake_word" else None if f == "ptt" else st.get(f))
        return dict(c)

    def resolve(self, ref):
        if not isinstance(ref, str):
            return None
        c = self.refresh()
        return self._live(c["by_key"].get(ref) or c["by_name"].get(ref) or c["presets"].get(ref) or c.get("voice", {}).get(ref)
                          or c["meters"].get(ref) or (c["by_key"].get(c["aliases"][ref]) if ref in c["aliases"] else None))

    def meter_values(self):
        """{meter key: {level, rms, peak, clip, signal}} from the deployer's taps."""
        c = self.refresh()
        st = self.dep.tap_stats()
        out = {}
        for key, m in c["meters"].items():
            s = st.get(m["tap"]) or {}
            rms = [x for x in (s.get("rms_dbfs") or []) if x is not None]
            peak = [x for x in (s.get("peak_dbfs") or []) if x is not None]
            age = s.get("age_s")
            run = bool(s.get("running", False))
            # stale = the tap delivered no frames in the last second (not running / not linked). A running tap whose
            # frames are all digital silence reports rms_dbfs=[None, ...] — that is "silent", not stale.
            stale = (not run) or s.get("rms_dbfs") is None or age is None or age > 2.0
            out[key] = {"level": max(rms) if rms else None, "rms": s.get("rms_dbfs"), "peak": s.get("peak_dbfs"),
                        "clip": s.get("clip"), "signal": s.get("signal"), "running": run,
                        "age_s": age, "stale": stale, "unit": "dBFS", "floor_db": -60,
                        "peak_level": max(peak) if peak else None,
                        "channels": len(s.get("rms_dbfs") or []) or None}
        return out

    def system_values(self):
        d = self.dep.design or {}
        hub_st = (((self.inv.nmos or {}).get("status") or {}).get(self.cfg.node_id)) or {}
        ptp = hub_st.get("ptp") or {}
        nodes = (self.inv.nmos or {}).get("nodes") or []
        state = "none" if not d else "error" if self.dep.design_error else "ok"
        h = self.health.check() if self.health else {"ok": True, "issues": []}
        return {"design.name": d.get("name") or "", "design.state": state, "design.error": self.dep.design_error or "",
                "design.deployed_at": self.dep.state.get("deployed_at"),
                "ptp.state": ptp.get("port_state") or "unknown", "ptp.offset_ns": ptp.get("offset_ns"),
                "registry.nodes": len(nodes), "registry.error": self.inv.error or "", "dsp.repairs": self.dep.repairs,
                "health.ok": h["ok"], "health.issues": [i["text"] for i in h["issues"]]}

    def listing(self):
        c = self.refresh()
        d = self.dep.design or {}
        return {"version": self.version, "deployed": bool(self.dep.design), "design": d.get("name"),
                "controls": [self._live(x) for x in list(c["by_key"].values()) + list(c["presets"].values()) + list(c.get("voice", {}).values())],
                "meters": list(c["meters"].values()), "meter_values": self.meter_values(),
                "presets": d.get("presets") or [], "panels": d.get("panels") or [], "aliases": c["aliases"],
                "system": self.system_values(), "last_preset": self.last_preset,
                "voice": self.voice.status() if self.voice is not None else None}

    def snapshot_event(self):
        return dict(self.listing(), type="hello")

    # ------------------------------------------------------------------ writing
    def coerce(self, c, value):
        bt = self.types.get(c.get("block_type"))
        if bt and c.get("param"):
            return bt.coerce({c["param"]: value})[c["param"]]
        return value

    def set(self, ref, value, fade_s=0, source="api"):
        c = self.resolve(ref)
        if not c:
            raise DeployError(f"no control {ref!r}")
        if c["kind"] == "preset":
            if c["type"] == "trigger":
                return self.recall(c["preset"], source=source)
            return self.recall(value, source=source)
        if c["kind"] == "meter":
            raise DeployError(f"{c['name']} is a meter (read-only)")
        if c["kind"] == "trigger":
            if not self.player:
                raise DeployError("no sound player on this hub")
            from .sounds import SoundError
            try:
                return dict(self.player.trigger(c["block"], c["action"], source=source), key=c["key"], name=c["name"])
            except SoundError as e:
                raise DeployError(str(e))
        if c["kind"] == "voice":
            try:
                if c["field"] == "ptt":
                    return dict(self.voice.ptt(source=source), key=c["key"], name=c["name"])
                if c["field"] == "ptt_hold":
                    return dict(self.voice.ptt_hold(B.truthy(value), source=source), key=c["key"], name=c["name"])
                if c["field"] == "wake_word":
                    return dict(self.voice.set_wake(B.truthy(value), source=source), key=c["key"], name=c["name"])
            except RuntimeError as e:
                raise DeployError(str(e))
            raise DeployError(f"{c['name']} is read-only")
        value = self.coerce(c, value)
        with self.lock:
            f = self.fades.pop(c["key"], None)
            if f:
                f.stop()
        if fade_s and c["type"] in ("number", "int") and c["live"] and isinstance(value, (int, float)):
            cur = self.resolve(c["key"])["value"]
            if cur is not None and abs(float(cur) - float(value)) > 1e-9:
                f = Fade(self, c, float(cur), float(value), float(fade_s), source)
                with self.lock:
                    self.fades[c["key"]] = f
                f.start()
                return {"ok": True, "key": c["key"], "name": c["name"], "value": value, "fading": True, "fade_s": fade_s}
        r = self.dep.set_param(c["block"], c["param"], value, source=source)
        return dict(r, key=c["key"], name=c["name"], value=self.resolve(c["key"])["value"])

    def get(self, ref):
        c = self.resolve(ref)
        if not c:
            raise DeployError(f"no control {ref!r}")
        if c["kind"] == "meter":
            return dict(c, **(self.meter_values().get(c["key"]) or {}))
        return c

    # ------------------------------------------------------------------ presets
    def _preset(self, ref):
        for p in (self.dep.design or {}).get("presets") or []:
            if ref in (p.get("id"), p.get("name")) or B.slug(p.get("name")) == B.slug(ref or ""):
                return p
        return None

    def recall(self, ref, fade_s=None, source="api"):
        p = self._preset(ref)
        if not p:
            raise DeployError(f"no preset {ref!r}")
        fade = p.get("fade_s", 0) if fade_s is None else fade_s
        done, skipped = [], []
        for key, v in (p.get("values") or {}).items():
            c = self.resolve(key)
            if not c or c["kind"] != "param":
                skipped.append(key)
                continue
            self.set(key, v, fade_s=fade if c["live"] else 0, source=f"preset:{p['name']}")
            done.append(key)
        self.last_preset = p["name"]
        self.refresh(force=True)
        self.bus.publish({"type": "preset", "name": p["name"], "id": p["id"], "fade_s": fade, "set": done, "skipped": skipped, "source": source})
        return {"ok": True, "preset": p["name"], "set": done, "skipped": skipped, "fade_s": fade}

    def save_preset(self, name, keys, fade_s=0, pid=None, values=None):
        """Capture the live values of `keys` (or take `values`) as preset `name` (new, or replacing `pid`)."""
        if not self.dep.design:
            raise DeployError("nothing deployed")
        if not name:
            raise DeployError("a preset needs a name")
        vals = {}
        for k in keys or []:
            c = self.resolve(k)
            if not c or c["kind"] != "param":
                raise DeployError(f"no control {k!r}")
            vals[c["key"]] = c["value"]
        for k, v in (values or {}).items():
            c = self.resolve(k)
            if not c or c["kind"] != "param":
                raise DeployError(f"no control {k!r}")
            vals[c["key"]] = self.coerce(c, v)
        presets = list(self.dep.design.get("presets") or [])
        taken = {p["id"] for p in presets}
        if pid is None:
            pid = B.slug(name)
            n = 2
            while pid in taken:
                pid = f"{B.slug(name)}_{n}"
                n += 1
        new = {"id": pid, "name": name, "values": vals, "fade_s": float(fade_s or 0)}
        presets = [p for p in presets if p["id"] != pid] + [new]
        self.dep.update_design(presets=presets)
        return new

    def delete_preset(self, ref):
        p = self._preset(ref)
        if not p:
            raise DeployError(f"no preset {ref!r}")
        self.dep.update_design(presets=[x for x in self.dep.design.get("presets") or [] if x["id"] != p["id"]])
        return {"ok": True, "deleted": p["id"]}

    # ------------------------------------------------------------------ panels
    def set_panels(self, panels):
        if not isinstance(panels, list):
            raise DeployError("panels must be a list")
        for p in panels:
            if not p.get("id") or not isinstance(p.get("items"), list):
                raise DeployError("every panel needs an id and an items list")
        self.dep.update_design(panels=panels)
        return {"ok": True, "panels": len(panels)}

    # ------------------------------------------------------------------ validation for the designer's plan
    def validate(self, design):
        """Warnings for a design about to be deployed: renamed blocks (names change), panel/preset items whose control
        no longer exists."""
        warns, renames = [], []
        cur = {b["id"]: b for b in (self.dep.design or {}).get("blocks", [])}
        new = {b["id"]: b for b in design.get("blocks", [])}
        for bid, b in new.items():
            old = cur.get(bid)
            if old and (old.get("label") or "") != (b.get("label") or "") and old.get("type") == b.get("type"):
                renames.append({"block": bid, "from": B.slug(old.get("label") or bid), "to": B.slug(b.get("label") or bid)})
        keys = set()
        for b in new.values():
            bt = self.types.get(b.get("type"))
            if bt:
                keys.update(f"{b['id']}.{p}" for p in bt.params)
        for pn in design.get("panels") or []:
            for it in pn.get("items") or []:
                k = it.get("key") or ""
                if k.endswith(".level") or k.startswith("voice."):
                    continue
                if k and k not in keys and not k.startswith("preset"):
                    warns.append(f"panel {pn.get('name') or pn.get('id')!r}: control {k} is not in this design")
        for ps in design.get("presets") or []:
            for k in (ps.get("values") or {}):
                if k not in keys:
                    warns.append(f"preset {ps.get('name')!r}: control {k} is not in this design")
        for r in renames:
            warns.append(f"block {r['block']} renamed: controls {r['from']}.* become {r['to']}.* (old names keep working until the next deploy; panels, presets and HA use the block id)")
        return {"warnings": warns, "renames": renames}

    def stop(self):
        for f in list(self.fades.values()):
            f.stop()


class Fade(threading.Thread):
    """Linear ramp of one live numeric control over fade_s (FADE_STEP_S steps, in the control's own unit: dB for a
    gain, so the ramp sounds even); the last step persists the design."""

    def __init__(self, controls, c, start, end, secs, source):
        super().__init__(daemon=True)
        self.ctl, self.c, self.start_v, self.end_v, self.secs, self.source = controls, c, start, end, max(0.05, secs), source
        self._halt = threading.Event()

    def stop(self):
        self._halt.set()

    def run(self):
        t0 = time.time()
        n = max(1, int(self.secs / FADE_STEP_S))
        for i in range(1, n + 1):
            if self._halt.is_set():
                return
            v = self.start_v + (self.end_v - self.start_v) * i / n
            if self.c["type"] == "int":
                v = int(round(v))
            try:
                self.ctl.dep.set_param(self.c["block"], self.c["param"], v, persist=(i == n), source=self.source, quiet=(i < n))
            except DeployError:
                return
            if i % max(1, round(0.2 / FADE_STEP_S)) == 0 and i < n:          # the UI and HA: 5 updates a second
                self.ctl.bus.publish({"type": "control", "key": self.c["key"], "name": self.c["name"], "value": v, "source": self.source, "fading": True})
            time.sleep(max(0, t0 + i * self.secs / n - time.time()))
        with self.ctl.lock:
            if self.ctl.fades.get(self.c["key"]) is self:
                del self.ctl.fades[self.c["key"]]
