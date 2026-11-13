"""Home Assistant over MQTT with discovery (D2). Every control of the deployed design is an HA entity on one device
(the DSP = this hub): numbers (live params as sliders, restart params as boxes in the config category), switches,
selects; every meter a sensor (dBFS); presets a select plus one button each; the deploy state and PTP/registry health
as sensors. Retained discovery under <prefix>/<component>/owl_dsp_<hub>/<object>/config, an availability topic with a
last-will, re-publish on reconnect and on HA's birth message, stale entities removed when a design drops them.

Broker: OWL_MQTT_URL (mqtt://user:pass@host:1883) or /var/lib/owl-node/mqtt.json ({host, port, username, password,
discovery_prefix}); the password is never logged or exposed by the API. No config = the bridge stays off and
/api/dsp/status says so. paho-mqtt 2.x (python3-paho-mqtt in the image).

Topics (base = owl-dsp/<hub>):  base/availability  online|offline
  base/control/<key>/state  base/control/<key>/set      base/meter/<key>/state     base/system/<name>/state
  base/preset/state  base/preset/set (name)  base/preset/<id>/press
"""
import json, os, re, threading, time
from urllib.parse import unquote, urlsplit

from .. import VERSION, config
from ..config import atomic_write, read_json
from .deploy import DeployError

CONF = os.path.join(config.STATE_DIR, "mqtt.json")
PUBLISHED = os.path.join(config.STATE_DIR, "dsp-mqtt.json")


def log(*a):
    print(time.strftime("%H:%M:%S"), "mqtt:", *a, flush=True)


def load_config():
    url = os.environ.get("OWL_MQTT_URL")
    if url:
        u = urlsplit(url)
        return {"host": u.hostname, "port": u.port or (8883 if u.scheme == "mqtts" else 1883), "username": unquote(u.username or "") or None,
                "password": unquote(u.password or "") or None, "tls": u.scheme == "mqtts",
                "discovery_prefix": os.environ.get("OWL_MQTT_DISCOVERY", "homeassistant"), "source": "OWL_MQTT_URL"}
    c = read_json(CONF, None)
    if c and c.get("host"):
        return {"host": c["host"], "port": int(c.get("port") or 1883), "username": c.get("username"), "password": c.get("password"),
                "tls": bool(c.get("tls")), "discovery_prefix": c.get("discovery_prefix") or "homeassistant", "source": CONF}
    return None


def oid(key):
    return re.sub(r"[^a-zA-Z0-9_-]+", "_", key)


class Bridge(threading.Thread):
    def __init__(self, controls, cfg, controller_port=8361):
        super().__init__(daemon=True)
        self.ctl, self.cfg = controls, cfg
        self.conf = load_config()
        self.hub = cfg.name
        self.base = f"owl-dsp/{self.hub}"
        self.node_id = f"owl_dsp_{oid(self.hub)}"
        self.client = None
        self.connected, self.error, self.connects, self.last_publish = False, None, 0, None
        self.published = set((read_json(PUBLISHED, {}) or {}).get("objects") or [])
        self.entities = 0
        self.port = controller_port
        self._halt = threading.Event()
        self._last_meter = {}
        self._last_sys = {}
        self._discovery_version = -1
        self.ip = None

    # ------------------------------------------------------------------ status
    def status(self):
        c = self.conf or {}
        return {"enabled": bool(self.conf), "connected": self.connected, "broker": f"{c.get('host')}:{c.get('port')}" if self.conf else None,
                "username": c.get("username"), "config_source": c.get("source"), "entities": self.entities, "connects": self.connects,
                "error": self.error, "last_publish": self.last_publish, "base_topic": self.base,
                "hint": None if self.conf else f"set OWL_MQTT_URL or write {CONF} {{host, port, username, password}}"}

    # ------------------------------------------------------------------ paho
    def run(self):
        if not self.conf:
            log("no broker configured (OWL_MQTT_URL or %s): bridge off" % CONF)
            return
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            self.error = "paho-mqtt is not installed"
            log(self.error)
            return
        try:
            self.ip = config.if_ipv4(self.cfg.media_if) if getattr(self.cfg, "media_if", None) else None
        except Exception:
            self.ip = None
        cl = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"owl-dsp-{self.hub}", clean_session=True)
        if self.conf.get("username"):
            cl.username_pw_set(self.conf["username"], self.conf.get("password"))
        if self.conf.get("tls"):
            cl.tls_set()
        cl.will_set(f"{self.base}/availability", "offline", qos=1, retain=True)
        cl.on_connect, cl.on_disconnect, cl.on_message = self._on_connect, self._on_disconnect, self._on_message
        cl.reconnect_delay_set(2, 60)
        self.client = cl
        q = self.ctl.bus.subscribe()
        threading.Thread(target=self._events, args=(q,), daemon=True).start()
        while not self._halt.is_set():
            try:
                cl.connect(self.conf["host"], self.conf["port"], keepalive=30)
                cl.loop_forever(retry_first_connection=True)
            except Exception as e:
                self.error = f"{type(e).__name__}: {e}"
                self.connected = False
                log(f"connect to {self.conf['host']}:{self.conf['port']} failed: {self.error}; retry in 10 s")
                self._halt.wait(10)

    def _on_connect(self, cl, userdata, flags, rc, props=None):
        if getattr(rc, "is_failure", False) or (isinstance(rc, int) and rc != 0):
            self.error = f"broker refused: {rc}"
            self.connected = False
            log(self.error)
            return
        self.connected, self.error, self.connects = True, None, self.connects + 1
        log(f"connected to {self.conf['host']}:{self.conf['port']} as {self.conf.get('username') or 'anonymous'}")
        cl.subscribe([(f"{self.base}/control/+/set", 0), (f"{self.base}/preset/set", 0), (f"{self.base}/preset/+/press", 0),
                      (f"{self.conf['discovery_prefix']}/status", 0)])
        self._discovery_version = -1
        self.publish_all()

    def _on_disconnect(self, cl, userdata, flags=None, rc=None, props=None):
        self.connected = False
        log(f"disconnected ({rc})")

    def _on_message(self, cl, userdata, msg):
        t, payload = msg.topic, msg.payload.decode(errors="replace").strip()
        try:
            if t == f"{self.conf['discovery_prefix']}/status":
                if payload.lower() == "online":
                    log("HA is back: re-publishing discovery")
                    self._discovery_version = -1
                    self.publish_all()
                return
            if t == f"{self.base}/preset/set":
                self.ctl.recall(payload, source="mqtt")
                return
            m = re.fullmatch(re.escape(self.base) + r"/preset/([^/]+)/press", t)
            if m:
                self.ctl.recall(m.group(1), source="mqtt")
                return
            m = re.fullmatch(re.escape(self.base) + r"/control/([^/]+)/set", t)
            if m:
                key = m.group(1)
                c = self.ctl.resolve(key)
                if not c:
                    log(f"set of unknown control {key}")
                    return
                v = payload
                if c["type"] == "bool":
                    v = payload.upper() in ("ON", "TRUE", "1")
                elif c["type"] in ("number", "int"):
                    v = float(payload)
                self.ctl.set(key, v, source="mqtt")
        except (DeployError, ValueError) as e:
            log(f"{t}: {e}")
        except Exception as e:
            log(f"{t}: {type(e).__name__}: {e}")

    # ------------------------------------------------------------------ publishing
    def _pub(self, topic, payload, retain=False, qos=0):
        if not self.client or not self.connected:
            return
        if payload is not None and not isinstance(payload, (str, bytes)):
            payload = json.dumps(payload)
        self.client.publish(topic, payload, qos=qos, retain=retain)
        self.last_publish = time.time()

    def device(self):
        d = {"identifiers": [f"owl-dsp-{self.hub}"], "name": f"Owl DSP {self.cfg.label}", "manufacturer": "owl-node",
             "model": "owl-node dsp", "sw_version": VERSION}
        if self.ip:
            d["configuration_url"] = f"http://{self.ip}:{self.port}/"
        return d

    def discovery(self):
        """(object id, component, config) for every entity of the deployed design."""
        lst = self.ctl.listing()
        out = []
        dev, avail = self.device(), [{"topic": f"{self.base}/availability"}]
        for c in lst["controls"]:
            if c["kind"] == "preset":
                continue
            if c["kind"] == "voice":           # D3: push-to-talk (button), hold (switch), mic live (binary sensor), texts (sensors)
                o = oid(c["key"])
                vb = {"name": c["label"], "unique_id": f"{self.node_id}_{o}", "object_id": f"{self.node_id}_{o}", "device": dev, "availability": avail}
                st_t, cmd_t = f"{self.base}/control/{c['key']}/state", f"{self.base}/control/{c['key']}/set"
                if c["type"] == "trigger":
                    out.append((o, "button", dict(vb, command_topic=cmd_t, payload_press="PRESS", icon="mdi:microphone-message")))
                elif c["field"] in ("ptt_hold", "wake_word"):
                    out.append((o, "switch", dict(vb, state_topic=st_t, command_topic=cmd_t, payload_on="ON", payload_off="OFF", state_on="ON", state_off="OFF",
                                                  icon="mdi:microphone" if c["field"] == "ptt_hold" else "mdi:ear-hearing")))
                elif c["type"] == "select":
                    out.append((o, "select", dict(vb, state_topic=st_t, command_topic=cmd_t, options=[str(x) for x in c.get("options") or []],
                                                  icon="mdi:account-voice")))
                elif c["field"] == "mic_live":
                    out.append((o, "binary_sensor", dict(vb, state_topic=st_t, payload_on="ON", payload_off="OFF", icon="mdi:microphone")))
                else:
                    out.append((o, "sensor", dict(vb, state_topic=st_t, icon={"state": "mdi:account-voice", "ha": "mdi:link-variant"}.get(c["field"], "mdi:message-text"))))
                continue
            o = oid(c["key"])
            base = {"name": f"{c['block_label']} {c['label']}", "unique_id": f"{self.node_id}_{o}", "device": dev, "availability": avail,
                    "state_topic": f"{self.base}/control/{c['key']}/state", "command_topic": f"{self.base}/control/{c['key']}/set",
                    "object_id": f"{self.node_id}_{o}"}
            if not c["live"]:
                base["entity_category"] = "config"
            if c["type"] in ("number", "int"):
                comp = "number"
                base.update({"min": c.get("min", -1e9), "max": c.get("max", 1e9), "step": c.get("step", 1 if c["type"] == "int" else 0.1),
                             "mode": "slider" if c["live"] and "min" in c and "max" in c else "box"})
                if c.get("unit"):
                    base["unit_of_measurement"] = c["unit"]
            elif c["type"] == "bool":
                comp = "switch"
                base.update({"payload_on": "ON", "payload_off": "OFF", "state_on": "ON", "state_off": "OFF"})
            elif c["type"] == "select":
                comp = "select"
                base["options"] = [str(x) for x in c.get("options") or []]
            elif c["type"] == "trigger":              # a Sound player's Play / Stop
                comp = "button"
                base.pop("state_topic", None)
                base.update({"payload_press": "PRESS", "icon": "mdi:play" if c.get("action") == "play" else "mdi:stop"})
            else:
                comp = "text"
            out.append((o, comp, base))
        for m in lst["meters"]:
            o = oid(m["key"])
            out.append((o, "sensor", {"name": f"{m['block_label']} {m['label']}", "unique_id": f"{self.node_id}_{o}", "object_id": f"{self.node_id}_{o}",
                                      "device": dev, "availability": avail, "state_topic": f"{self.base}/meter/{m['key']}/state",
                                      "unit_of_measurement": "dBFS", "state_class": "measurement", "icon": "mdi:waveform",
                                      "suggested_display_precision": 1, "expire_after": 120}))
        if lst["presets"]:
            out.append(("presets", "select", {"name": "Preset", "unique_id": f"{self.node_id}_presets", "object_id": f"{self.node_id}_preset",
                                              "device": dev, "availability": avail, "state_topic": f"{self.base}/preset/state",
                                              "command_topic": f"{self.base}/preset/set", "options": [p["name"] for p in lst["presets"]],
                                              "icon": "mdi:tune-vertical"}))
            for p in lst["presets"]:
                o = f"preset_{oid(p['id'])}"
                out.append((o, "button", {"name": f"Recall {p['name']}", "unique_id": f"{self.node_id}_{o}", "object_id": f"{self.node_id}_{o}",
                                          "device": dev, "availability": avail, "command_topic": f"{self.base}/preset/{p['id']}/press",
                                          "payload_press": "PRESS", "icon": "mdi:play-box"}))
        sys_ = [("design", "Design", "mdi:sitemap", None), ("design_state", "DSP state", "mdi:check-network", None),
                ("ptp_state", "PTP state", "mdi:clock-check", None), ("ptp_offset_ns", "PTP offset", "mdi:timer-sand", "ns"),
                ("registry_nodes", "NMOS nodes", "mdi:lan", None)]
        for o, name, icon, unit in sys_:
            cfg = {"name": name, "unique_id": f"{self.node_id}_{o}", "object_id": f"{self.node_id}_{o}", "device": dev, "availability": avail,
                   "state_topic": f"{self.base}/system/{o}/state", "icon": icon, "entity_category": "diagnostic",
                   "json_attributes_topic": f"{self.base}/system/{o}/attributes"}
            if unit:
                cfg["unit_of_measurement"] = unit
                cfg["state_class"] = "measurement"
            out.append((o, "sensor", cfg))
        # one problem sensor for the whole system: ON when something needs attention, the list in its attributes
        out.append(("health", "binary_sensor", {"name": "Needs attention", "unique_id": f"{self.node_id}_health",
                                                "object_id": f"{self.node_id}_health", "device": dev, "availability": avail,
                                                "state_topic": f"{self.base}/system/health/state", "device_class": "problem",
                                                "payload_on": "ON", "payload_off": "OFF", "entity_category": "diagnostic",
                                                "json_attributes_topic": f"{self.base}/system/health/attributes"}))
        return out

    def publish_all(self):
        """Discovery (when the control set changed), availability, every state."""
        lst = self.ctl.listing()
        if lst["version"] != self._discovery_version:
            ents = self.discovery()
            now = set()
            for o, comp, cfg in ents:
                self._pub(f"{self.conf['discovery_prefix']}/{comp}/{self.node_id}/{o}/config", cfg, retain=True, qos=1)
                now.add(f"{comp}/{o}")
            for stale in self.published - now:
                comp, o = stale.split("/", 1)
                self._pub(f"{self.conf['discovery_prefix']}/{comp}/{self.node_id}/{o}/config", "", retain=True, qos=1)
            self.published = now
            atomic_write(PUBLISHED, {"objects": sorted(now), "time": time.time()})
            self.entities = len(now)
            self._discovery_version = lst["version"]
            log(f"discovery: {len(now)} entities for design {lst['design']!r}")
        self._pub(f"{self.base}/availability", "online", retain=True, qos=1)
        for c in lst["controls"]:
            if c["kind"] == "param":
                self._pub(f"{self.base}/control/{c['key']}/state", self.fmt(c, c["value"]), retain=True)
        if lst["last_preset"]:
            self._pub(f"{self.base}/preset/state", lst["last_preset"], retain=True)
        if lst.get("voice"):
            self.publish_voice(lst["voice"])
        self._last_meter, self._last_sys = {}, {}
        self.publish_meters(lst["meter_values"])
        self.publish_system(lst["system"], force=True)

    @staticmethod
    def fmt(c, v):
        if c["type"] == "bool":
            return "ON" if v else "OFF"
        if isinstance(v, float):
            return f"{v:.6g}"
        return "" if v is None else str(v)

    def publish_voice(self, st):
        for f in ("state", "last_text", "last_reply", "ha"):
            self._pub(f"{self.base}/control/voice.{f}/state", str(st.get(f) or ("" if f != "state" else "idle")), retain=True)
        self._pub(f"{self.base}/control/voice.mic_live/state", "ON" if st.get("mic_live") else "OFF", retain=True)
        self._pub(f"{self.base}/control/voice.ptt_hold/state", "ON" if st.get("hold") else "OFF", retain=True)
        self._pub(f"{self.base}/control/voice.wake_word/state", "ON" if st.get("wake") else "OFF", retain=True)
        if st.get("wake_model"):
            self._pub(f"{self.base}/control/voice.wake_model/state", str(st["wake_model"]), retain=True)

    def publish_meters(self, values):
        # changed by >= 0.5 dB, or not sent for 45 s: the sensors carry expire_after 120 s, so a silent meter must keep
        # saying so (found 2026-11-09: silent meters went "unavailable" in HA two minutes after the last change)
        now = time.time()
        for k, v in values.items():
            lvl = v.get("level")
            lvl = -100.0 if lvl is None else round(lvl, 1)
            last = self._last_meter.get(k)
            if last is None or abs(last[0] - lvl) >= 0.5 or now - last[1] >= 45:
                self._last_meter[k] = (lvl, now)
                self._pub(f"{self.base}/meter/{k}/state", f"{lvl:.1f}")

    def publish_system(self, s, force=False):
        vals = {"design": (s["design.name"] or "none", {"state": s["design.state"], "error": s["design.error"], "deployed_at": s["design.deployed_at"]}),
                "design_state": (s["design.state"], {"error": s["design.error"], "repairs": s["dsp.repairs"]}),
                "ptp_state": (s["ptp.state"], {"offset_ns": s["ptp.offset_ns"]}),
                "ptp_offset_ns": (s["ptp.offset_ns"] if s["ptp.offset_ns"] is not None else "unknown", {}),
                "registry_nodes": (s["registry.nodes"], {"error": s["registry.error"]}),
                "health": ("OFF" if s.get("health.ok", True) else "ON", {"issues": s.get("health.issues") or [],
                                                                         "count": len(s.get("health.issues") or [])})}
        for o, (st, attrs) in vals.items():
            if force or self._last_sys.get(o) != (st, json.dumps(attrs, sort_keys=True)):
                self._last_sys[o] = (st, json.dumps(attrs, sort_keys=True))
                self._pub(f"{self.base}/system/{o}/state", str(st), retain=True)
                self._pub(f"{self.base}/system/{o}/attributes", attrs, retain=True)

    def _events(self, q):
        """Bus events -> state topics; meters at 1 Hz; system every 10 s; discovery when the design changes."""
        last_m = last_s = 0
        while not self._halt.is_set():
            try:
                ev = q.get(timeout=0.5)
            except Exception:
                ev = None
            if not self.connected:
                continue
            try:
                if ev:
                    if ev["type"] == "control" and not ev.get("fading"):
                        c = self.ctl.resolve(ev["key"])
                        if c and c["kind"] == "param":
                            self._pub(f"{self.base}/control/{c['key']}/state", self.fmt(c, c["value"]), retain=True)
                    elif ev["type"] == "preset":
                        self._pub(f"{self.base}/preset/state", ev["name"], retain=True)
                    elif ev["type"] == "voice":
                        self.publish_voice(ev)
                    elif ev["type"] == "design":
                        self.publish_all()
                now = time.time()
                if now - last_m >= 1.0:
                    last_m = now
                    self.publish_meters(self.ctl.meter_values())
                if now - last_s >= 10.0:
                    last_s = now
                    self.publish_system(self.ctl.system_values())
            except Exception as e:
                log(f"publish: {type(e).__name__}: {e}")

    def stop(self):
        self._halt.set()
        if self.client and self.connected:
            try:
                self.client.publish(f"{self.base}/availability", "offline", qos=1, retain=True)
                self.client.disconnect()
            except Exception:
                pass
