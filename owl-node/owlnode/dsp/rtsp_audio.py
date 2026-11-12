"""An RTSP stream's audio as a PipeWire source node: the `rtsp` block's audio output (a camera's microphone, a network
radio, anything that speaks RTSP). Run and restarted (with backoff) by the dsp deployer like a block process:

  python3 -m owlnode.dsp.rtsp_audio --url URL --node owl-<id>-audio --desc "<label>: audio" [--latency 200] [--block id]

rtspsrc asks the server for one audio track (select-stream), decodebin plugs the depayloader and decoder (AAC, G.711,
Opus, ...), and the PCM goes through a leaky queue into a FIFO. The source node itself is PipeWire's pipe-tunnel
module in a `pipewire -c` child process (like every other block process): it reads the FIFO without ever blocking the
graph and plays silence when nothing has arrived. (A first version fed `pw-play` through a pipe: pw-play reads its
input in the real-time thread, so a camera that was slow to start stalled the whole graph for ~10 s, 2026-11-11.)
Exit 1 = the stream failed or ended (restart), 3 = the stream has no audio track."""
import argparse, os, re, stat, subprocess, sys, threading, time

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

RATE, CH = 48000, 2


def log(*a):
    print(time.strftime("%H:%M:%S"), "rtsp-audio:", *a, flush=True)


def redact(url):
    """scheme://host:port only: RTSP URLs carry passwords and access tokens (UniFi Protect's path is the token)."""
    m = re.match(r"^(\w+)://(?:[^@/]*@)?([^/]+)", url or "")
    return f"{m.group(1)}://{m.group(2)}/..." if m else "(url)"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--node", required=True)
    ap.add_argument("--desc", default="RTSP audio")
    ap.add_argument("--block", default="")
    ap.add_argument("--latency", type=int, default=200)
    a = ap.parse_args()
    Gst.init(None)
    from .blocks import COMMON
    base = f"/tmp/owl-rtsp-{a.block or a.node}"
    fifo, conf = base + ".fifo", base + ".conf"
    if os.path.exists(fifo) and not stat.S_ISFIFO(os.stat(fifo).st_mode):
        os.unlink(fifo)
    if not os.path.exists(fifo):
        os.mkfifo(fifo, 0o600)
    esc = lambda v: str(v).replace('"', "'")
    with open(conf, "w") as f:
        f.write(COMMON + f"""    {{ name = libpipewire-module-pipe-tunnel
        args = {{
            tunnel.mode = source
            pipe.filename = "{fifo}"
            audio.format = S16LE
            audio.rate = {RATE}
            audio.channels = {CH}
            audio.position = [ FL FR ]
            stream.props = {{
                node.name = "{esc(a.node)}"
                node.description = "{esc(a.desc)}"
                media.class = Audio/Source
                node.autoconnect = false
                owl.dsp.block = "{esc(a.block)}"
            }}
        }}
    }}
]
""")
    pw = subprocess.Popen(["pipewire", "-c", conf], start_new_session=True)
    wfd = os.open(fifo, os.O_RDWR)          # O_RDWR: never blocks on open, whether or not the tunnel has it open yet
    loop = GLib.MainLoop()
    code = [1]
    pipe = Gst.Pipeline.new("rtsp-audio")
    src = Gst.ElementFactory.make("rtspsrc", "src")
    url = a.url
    if url.startswith("rtsps://") and re.search(r"[?&]enableSrtp\b", url):
        # UniFi Protect's "?enableSrtp": GStreamer's SRTP (MIKEY) never yields audio from it, and inside rtsps:// the TLS
        # connection already encrypts every packet (TCP interleaved), so the audio pull asks without it. MediaMTX keeps
        # the URL as given for the video relay (it handles SRTP).
        url = re.sub(r"([?&])enableSrtp(=[^&]*)?&?", r"\1", url).rstrip("?&")
        log("rtsps with enableSrtp: pulling the audio without SRTP (the TLS connection still encrypts it)")
    src.set_property("location", url)
    src.set_property("latency", a.latency)
    Gst.util_set_object_arg(src, "protocols", "tcp")       # cameras behind Wi-Fi / NAT: interleaved TCP is reliable
    if a.url.startswith("rtsps://"):
        src.set_property("tls-validation-flags", 0)         # cameras and NVRs present self-signed certificates
    dec = Gst.ElementFactory.make("decodebin", "dec")
    chain = Gst.parse_bin_from_description(
        "queue leaky=downstream max-size-time=300000000 max-size-buffers=0 max-size-bytes=0 ! audioconvert ! audioresample ! "
        f"audio/x-raw,format=S16LE,rate={RATE},channels={CH} ! fdsink name=out sync=false", True)
    chain.get_by_name("out").set_property("fd", wfd)
    for e in (src, dec, chain):
        pipe.add(e)
    linked = {"rtsp": False, "dec": False}

    chosen = []

    def select_stream(_src, num, caps):
        # exactly one audio track: UniFi Protect offers Opus and AAC; an unlinked second track would fail the stream
        media = caps.get_structure(0).get_string("media") if caps and caps.get_size() else None
        if media == "audio" and not chosen:
            chosen.append(num)
            return True
        return False

    def on_rtsp_pad(_src, pad):
        caps = pad.get_current_caps() or pad.query_caps(None)
        media = caps.get_structure(0).get_string("media") if caps.get_size() else None
        if media == "audio" and not linked["rtsp"]:
            if pad.link(dec.get_static_pad("sink")) == Gst.PadLinkReturn.OK:
                linked["rtsp"] = True
                log(f"audio track: {caps.get_structure(0).get_string('encoding-name')} from {redact(a.url)}")

    def on_dec_pad(_dec, pad):
        caps = pad.get_current_caps() or pad.query_caps(None)
        if caps.get_structure(0).get_name().startswith("audio/x-raw") and not linked["dec"]:
            if pad.link(chain.get_static_pad("sink")) == Gst.PadLinkReturn.OK:
                linked["dec"] = True
                log(f"playing into PipeWire as {a.node}")

    src.connect("select-stream", select_stream)
    src.connect("pad-added", on_rtsp_pad)
    dec.connect("pad-added", on_dec_pad)

    def on_msg(_bus, m):
        if m.type == Gst.MessageType.ERROR:
            err, dbg = m.parse_error()
            log(f"error from {m.src.get_name()}: {err.message}")
            loop.quit()
        elif m.type == Gst.MessageType.EOS:
            log("stream ended")
            loop.quit()
        return True

    bus = pipe.get_bus()
    bus.add_signal_watch()
    bus.connect("message", on_msg)

    def no_audio():
        if not linked["rtsp"]:
            log(f"no audio track after 15 s from {redact(a.url)} (the stream may be video only)")
            code[0] = 3
            loop.quit()
        return False

    def watch_pw():
        pw.wait()
        log("the pipe-tunnel process exited")
        GLib.idle_add(loop.quit)

    GLib.timeout_add_seconds(15, no_audio)
    threading.Thread(target=watch_pw, daemon=True).start()
    log(f"connecting to {redact(a.url)} (latency {a.latency} ms)")
    pipe.set_state(Gst.State.PLAYING)
    try:
        loop.run()
    except KeyboardInterrupt:
        code[0] = 0
    pipe.set_state(Gst.State.NULL)
    try:
        os.close(wfd)
    except OSError:
        pass
    pw.terminate()
    try:
        pw.wait(3)
    except subprocess.TimeoutExpired:
        pw.kill()
    sys.exit(code[0])


if __name__ == "__main__":
    main()
