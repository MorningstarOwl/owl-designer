"""AES67 SDP: what our senders publish as their IS-05 transport file, and what receivers accept."""
import re


def sender_sdp(s, src_ip, gmid=None, domain=0, version=1, rtp=None, localmac=None):
    """s = a sender dict from config.Node.senders plus its active transport params; rtp = the PipeWire sender
    node's real rtp.* properties (payload type, ts-offset), which the SDP must match."""
    rtp = rtp or {}
    # PTP nodes: the grandmaster (or 'traceable' before we know it). Test nodes on the system clock: localmac.
    refclk = (f"localmac={localmac.upper()}" if localmac else
              f"ptp=IEEE1588-2008:{gmid.upper()}:{domain}" if gmid else "ptp=IEEE1588-2008:traceable")
    pt = int(rtp.get("payload") or 127)
    tso = int(rtp.get("ts-offset") or 0)
    ch = s["channels"]
    sid = int(s["id"].replace("-", "")[:8], 16)
    return "\r\n".join([
        "v=0",
        f"o=- {sid} {version} IN IP4 {src_ip}",
        f"s={s['session']}",
        f"i={s['label']}",
        "t=0 0",
    ] + ([] if localmac else [f"a=clock-domain:PTPv2 {domain}"]) + [
        f"m=audio {s['port']} RTP/AVP {pt}",
        f"c=IN IP4 {s['group']}/32",
        f"a=rtpmap:{pt} L24/48000/{ch}",
        f"a=source-filter: incl IN IP4 {s['group']} {src_ip}",
        "a=recvonly",
        "a=ptime:1",
        "a=framecount:48",
        f"a=ts-refclk:{refclk}",
        f"a=mediaclk:direct={tso}",
        "",
    ])


class SDPError(ValueError):
    pass


def parse(sdp):
    """The parts of an SDP an AES67 receiver needs. Raises SDPError on anything we cannot play."""
    if not sdp or not sdp.strip().startswith("v=0"):
        raise SDPError("not an SDP (must start with v=0)")
    out = {"session": None, "group": None, "source_ip": None, "port": None, "encoding": None, "rate": None,
           "channels": None, "ptime": None, "payload": None}
    media = False
    for line in re.split(r"\r?\n", sdp):
        if line.startswith("s=") and not media:
            out["session"] = line[2:].strip()
        elif line.startswith("o="):
            f = line[2:].split()
            if len(f) >= 6:
                out["origin_ip"] = f[5]
        elif line.startswith("c="):
            f = line[2:].split()
            if len(f) >= 3 and f[1] == "IP4":
                out["group"] = f[2].split("/")[0]
        elif line.startswith("m="):
            if media:
                break                       # first media section only (no ST 2022-7 second leg yet)
            media = True
            f = line[2:].split()
            if f[0] != "audio":
                raise SDPError(f"media {f[0]!r} is not audio")
            out["port"] = int(f[1].split("/")[0])
            out["payload"] = int(f[3])
        elif line.startswith("a=rtpmap:"):
            m = re.match(r"a=rtpmap:(\d+) ([A-Za-z0-9]+)/(\d+)(?:/(\d+))?", line)
            if m and (out["payload"] is None or int(m.group(1)) == out["payload"]):
                out["encoding"], out["rate"] = m.group(2).upper(), int(m.group(3))
                out["channels"] = int(m.group(4) or 1)
        elif line.startswith("a=ptime:"):
            out["ptime"] = float(line.split(":", 1)[1])
        elif line.startswith("a=source-filter:"):
            f = line.split()
            if len(f) >= 6:
                out["source_ip"] = f[-1]
    if out["port"] is None:
        raise SDPError("no m=audio line")
    if out["encoding"] not in ("L24", "L16"):
        raise SDPError(f"encoding {out['encoding']!r} not supported (L24 or L16)")
    if out["rate"] not in (44100, 48000, 96000):
        raise SDPError(f"sample rate {out['rate']} not supported")
    if out["source_ip"] is None:
        out["source_ip"] = out.get("origin_ip")
    return out


# ---------------------------------------------------------------- N3 video over SRT (Matrox "NMOS With SRT")
VIDEO_PT = 96
ENC = {"h265": "H265", "h264": "H264"}


def video_sender_sdp(s, ip, port, version=1):
    """The IS-05 transport file of an SRT video sender (a listener at ip:port).
    encap rtp (urn:x-matrox:transport:srt.rtp): an ordinary RTP video SDP (RFC 7798 / RFC 6184) whose c=/m= give the
    SRT listener. encap ts (srt.mp2t): `m=application <port> UDP mp2t`, as the Matrox spec requires."""
    src = s["source"]
    sid = int(s["id"].replace("-", "")[:8], 16)
    head = ["v=0", f"o=- {sid} {version} IN IP4 {ip}", f"s={s['label']}", "t=0 0"]
    owl = [f"a=x-owl-srt:listener latency={s['latency_ms']}",
           f"a=x-owl-video:{ENC[s['codec']]} {src['width']}x{src['height']}@{src['fps']} {s['bitrate_kbps']}kbps",
           f"a=x-owl-source:{src['kind']}" + (f" {src.get('pattern', 'smpte')}" if src["kind"] == "testsrc" else "")]
    if s["encap"] == "ts":
        media = [f"m=application {port} UDP mp2t", f"c=IN IP4 {ip}"]
    else:
        fmtp = "profile-id=1" if s["codec"] == "h265" else "packetization-mode=1;profile-level-id=640028"
        media = [f"m=video {port} RTP/AVP {VIDEO_PT}", f"c=IN IP4 {ip}", f"a=rtpmap:{VIDEO_PT} {ENC[s['codec']]}/90000",
                 f"a=fmtp:{VIDEO_PT} {fmtp}", f"a=framerate:{src['fps']}"]
    return "\r\n".join(head + media + owl + [""])


def parse_video(sdp, codecs=None):
    """What an SRT video receiver needs: the listener's address and how the stream is wrapped. Raises SDPError."""
    if not sdp or not sdp.strip().startswith("v=0"):
        raise SDPError("not an SDP (must start with v=0)")
    out = {"encap": None, "source_ip": None, "port": None, "encoding": None, "payload": None, "ref_pattern": None,
           "session": None, "video": None, "streamid": None}
    media = False
    for line in re.split(r"\r?\n", sdp):
        if line.startswith("s=") and not media:
            out["session"] = line[2:].strip()
        elif line.startswith("o="):
            f = line[2:].split()
            if len(f) >= 6:
                out["origin_ip"] = f[5]
        elif line.startswith("c="):
            f = line[2:].split()
            if len(f) >= 3 and f[1] == "IP4":
                out["source_ip"] = f[2].split("/")[0]
        elif line.startswith("m="):
            if media:
                break
            media = True
            f = line[2:].split()
            if len(f) < 4:
                raise SDPError("malformed m= line")
            out["port"] = int(f[1].split("/")[0])
            if f[0] == "application" and f[3].lower() == "mp2t":
                out["encap"] = "ts"
            elif f[0] == "video" and f[2].startswith("RTP"):
                out["encap"], out["payload"] = "rtp", int(f[3])
            else:
                raise SDPError(f"m={f[0]} {f[2]} {f[3]} is neither RTP video nor MPEG-TS (mp2t)")
        elif line.startswith("a=rtpmap:"):
            m = re.match(r"a=rtpmap:(\d+) ([A-Za-z0-9]+)/(\d+)", line)
            if m and (out["payload"] is None or int(m.group(1)) == out["payload"]):
                out["encoding"] = m.group(2).upper()
        elif line.startswith("a=x-owl-source:"):
            f = line.split(":", 1)[1].split()
            if f and f[0] == "testsrc":
                out["ref_pattern"] = f[1] if len(f) > 1 else "smpte"
        elif line.startswith("a=x-owl-video:"):
            out["video"] = line.split(":", 1)[1].strip()
        elif line.startswith("a=x-owl-srt:"):
            m = re.search(r"streamid=(\S+)", line)       # an SRT listener that names its streams (MediaMTX read:<path>)
            if m:
                out["streamid"] = m.group(1)
    if out["port"] is None:
        raise SDPError("no m= line")
    if out["source_ip"] is None:
        out["source_ip"] = out.get("origin_ip")
    if out["encap"] == "ts" and not out["encoding"] and out["video"]:
        enc = out["video"].split()[0].upper()            # MPEG-TS carries no rtpmap: the codec is in x-owl-video
        if enc in ("H264", "H265"):
            out["encoding"] = enc
    if out["encap"] == "rtp" and out["encoding"] not in ("H265", "H264"):
        raise SDPError(f"RTP encoding {out['encoding']!r} not supported (H265, H264)")
    if codecs and out["encap"] == "rtp" and out["encoding"].lower() not in codecs:
        raise SDPError(f"{out['encoding']} is not in this receiver's codecs {codecs}")
    return out


# ---------------------------------------------------------------- N4 JPEG XS in RTP (RFC 9134) over plain UDP
JXS_PT = 96


def _is_mcast(ip):
    try:
        return 224 <= int(ip.split(".")[0]) <= 239
    except (ValueError, AttributeError):
        return False


def jxsv_sender_sdp(s, src_ip, dst_ip, dst_port, version=1, localmac=None):
    """The IS-05 transport file of a JPEG XS sender, as BCP-006-01 asks: `b=AS` = the sender's IS-04 bit_rate, the
    fmtp parameters = its flow's attributes. No SSN/TP: the payloader sends each frame as a burst, which is not
    ST 2110-21 (narrow or wide) traffic shaping, so it does not claim ST 2110-22. The RTP clock is the sender's own
    (not PTP-derived), hence `mediaclk:sender`."""
    src = s["source"]
    sid = int(s["id"].replace("-", "")[:8], 16)
    mc = _is_mcast(dst_ip)
    fmtp = (f"packetmode=0;transmode=1;profile={s['profile']};level={s['level']};sublevel={s['sublevel']};"
            f"depth={s['depth']};width={src['width']};height={src['height']};exactframerate={src['fps']};"
            f"sampling={s['sampling']};colorimetry=BT709;TCS=SDR;RANGE=NARROW")
    lines = ["v=0", f"o=- {sid} {version} IN IP4 {src_ip}", f"s={s['label']}", f"i={s['description']}", "t=0 0",
             f"m=video {dst_port} RTP/AVP {JXS_PT}", f"c=IN IP4 {dst_ip}" + (f"/{s['ttl']}" if mc else ""),
             f"b=AS:{s['sender_kbps']}"]
    if mc:
        lines.append(f"a=source-filter: incl IN IP4 {dst_ip} {src_ip}")
    lines += [f"a=rtpmap:{JXS_PT} jxsv/90000", f"a=fmtp:{JXS_PT} {fmtp}",
              f"a=ts-refclk:localmac={(localmac or '00-00-00-00-00-00').upper()}", "a=mediaclk:sender",
              f"a=x-owl-video:JXS {src['width']}x{src['height']}@{src['fps']} {s['bpp']:g}bpp {s['bitrate_kbps']}kbps",
              f"a=x-owl-source:{src['kind']}" + (f" {src.get('pattern', 'smpte')}" if src["kind"] == "testsrc" else "")]
    if s.get("echo_port"):
        lines.append(f"a=x-owl-echo:{s['echo_port']}")       # receivers send each decoded frame's stamp back here
    return "\r\n".join(lines + [""])


def parse_jxsv(sdp):
    """What a JPEG XS RTP receiver needs. Accepts RFC 9134 SDPs from anyone (unicast or multicast, AMWA's test SDPs
    included); refuses what the depayloader cannot do (slice packetization, packetmode=1). Raises SDPError."""
    if not sdp or not sdp.strip().startswith("v=0"):
        raise SDPError("not an SDP (must start with v=0)")
    out = {"encap": "rtp-udp", "encoding": None, "payload": None, "port": None, "group": None, "dest": None,
           "source_ip": None, "fmtp": {}, "ref_pattern": None, "echo_port": None, "session": None, "video": None,
           "bit_rate": None}
    media = False
    for line in re.split(r"\r?\n", sdp):
        if line.startswith("s=") and not media:
            out["session"] = line[2:].strip()
        elif line.startswith("o="):
            f = line[2:].split()
            if len(f) >= 6:
                out["origin_ip"] = f[5]
        elif line.startswith("c="):
            f = line[2:].split()
            if len(f) >= 3 and f[1] == "IP4":
                out["dest"] = f[2].split("/")[0]
        elif line.startswith("b=AS:"):
            try:
                out["bit_rate"] = int(line[5:])
            except ValueError:
                pass
        elif line.startswith("m="):
            if media:
                break                       # first media section only (no ST 2022-7 second leg)
            media = True
            f = line[2:].split()
            if len(f) < 4 or f[0] != "video" or not f[2].startswith("RTP"):
                raise SDPError(f"m={' '.join(f[:3])} is not RTP video")
            out["port"], out["payload"] = int(f[1].split("/")[0]), int(f[3])
        elif line.startswith("a=rtpmap:"):
            m = re.match(r"a=rtpmap:(\d+) ([A-Za-z0-9-]+)/(\d+)", line)
            if m and (out["payload"] is None or int(m.group(1)) == out["payload"]):
                out["encoding"] = m.group(2).upper()
        elif line.startswith("a=fmtp:"):
            m = re.match(r"a=fmtp:(\d+) (.*)$", line)
            if m and (out["payload"] is None or int(m.group(1)) == out["payload"]):
                for p in m.group(2).split(";"):
                    k, eq, v = p.strip().partition("=")
                    if k:
                        out["fmtp"][k] = v.strip() if eq else True
        elif line.startswith("a=source-filter:"):
            f = line.split()
            if len(f) >= 6:
                out["source_ip"] = f[-1]
        elif line.startswith("a=x-owl-source:"):
            f = line.split(":", 1)[1].split()
            if f and f[0] == "testsrc":
                out["ref_pattern"] = f[1] if len(f) > 1 else "smpte"
        elif line.startswith("a=x-owl-echo:"):
            out["echo_port"] = int(line.split(":", 1)[1])
        elif line.startswith("a=x-owl-video:"):
            out["video"] = line.split(":", 1)[1].strip()
    if out["port"] is None:
        raise SDPError("no m=video line")
    if out["encoding"] != "JXSV":
        raise SDPError(f"RTP encoding {out['encoding']!r} is not jxsv (RFC 9134 JPEG XS)")
    if str(out["fmtp"].get("packetmode", "0")) != "0":
        raise SDPError("packetmode=1 (slice packetization) is not supported; codestream mode (0) only")
    for k in ("width", "height"):
        if k not in out["fmtp"]:
            raise SDPError(f"fmtp has no {k} (RFC 9134 receivers need the frame size)")
    if _is_mcast(out["dest"]):
        out["group"] = out["dest"]
    if out["source_ip"] is None:
        out["source_ip"] = out.get("origin_ip")
    return out


def jxsv_caps(f, payload=None):
    """udpsrc caps for rtpjxsvdepay from parse_jxsv()'s fmtp (it derives its output caps from them)."""
    c = ["application/x-rtp", "media=video", "clock-rate=90000", "encoding-name=JXSV", f"payload={payload or f['payload']}"]
    for k, v in sorted(f["fmtp"].items()):
        if v is True or not re.fullmatch(r"[A-Za-z0-9.:/_+-]+", str(v)):
            continue                         # flags (interlace, segmented) and odd values stay out of the caps
        c.append(f'{k.lower()}=(string)"{v}"')
    return ",".join(c)
