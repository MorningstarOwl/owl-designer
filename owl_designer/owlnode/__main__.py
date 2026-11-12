"""owl-node <role>: agent | audio | video-enc | video-dec | controller | dsp | version"""
import sys


def main():
    role = sys.argv[1] if len(sys.argv) > 1 else "agent"
    if role == "agent":
        from .agent import main as m
    elif role == "audio":
        from .audio import main as m
    elif role in ("video-enc", "video-dec"):
        from .video import main as vm
        m = lambda: vm(role.split("-")[1])   # noqa: E731
    elif role == "controller":
        from .controller import main as m
    elif role == "dsp":
        from .dsp.server import main as m
    elif role == "version":
        from . import VERSION
        print(VERSION)
        return
    else:
        sys.exit(f"unknown role {role!r} (agent, audio, video-enc, video-dec, controller, dsp)")
    m()


if __name__ == "__main__":
    main()
