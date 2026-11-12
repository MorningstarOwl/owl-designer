#!/usr/bin/env python3
"""owl-node host helper (runs as root, outside the container): ptp4l's own view of the clock, every 2 s, as
/run/owl-node/ptp.json for the agent (NMOS clock 'locked' + gmid, the Routing page's PTP badge).

pmc needs root, and Ubuntu's ptp4l AppArmor profile only lets ptp4l answer clients bound under /run/phc2sys.*,
so pmc binds /run/phc2sys.owlnode (N1 finding). Keeping this on the host keeps the container unprivileged.

  owl-node-ptpstat.py [--uds /var/run/ptp4l] [--out /run/owl-node/ptp.json] [--interval 2] [--once]
"""
import argparse, json, os, re, subprocess, tempfile, time


def query(uds):
    cmd = ["pmc", "-u", "-b", "0", "-i", "/run/phc2sys.owlnode", "-s", uds,
           "GET PORT_DATA_SET", "GET TIME_STATUS_NP", "GET PARENT_DATA_SET"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout
    f = dict(re.findall(r"^\s+(\w+)\s+(\S+)\s*$", out, re.M))
    if "portState" not in f:
        return {"time": time.time(), "error": "ptp4l did not answer pmc", "port_state": None}
    gm = f.get("gmIdentity") or f.get("grandmasterIdentity")
    try:
        off = int(float(f.get("master_offset", "0")))
    except ValueError:
        off = None
    return {"time": time.time(), "port_state": f["portState"], "offset_ns": off,
            "gm": gm, "gm_present": f.get("gmPresent") == "true",
            "steps_removed": int(f.get("stepsRemoved", 0) or 0) if f.get("stepsRemoved", "0").isdigit() else None,
            "clock_class": f.get("gm.ClockClass") or f.get("grandmasterClockClass")}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--uds", default="/var/run/ptp4l")
    ap.add_argument("--out", default="/run/owl-node/ptp.json")
    ap.add_argument("--interval", type=float, default=2)
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args()
    while True:
        try:
            st = query(a.uds)
        except Exception as e:
            st = {"time": time.time(), "error": str(e), "port_state": None}
        if a.once:
            print(json.dumps(st))
            return
        d = os.path.dirname(a.out)
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".ptp-")
        with os.fdopen(fd, "w") as fh:
            json.dump(st, fh)
        os.chmod(tmp, 0o644)
        os.replace(tmp, a.out)
        time.sleep(a.interval)


if __name__ == "__main__":
    main()
