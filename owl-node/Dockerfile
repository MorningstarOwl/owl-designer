# owl-node: one image, roles chosen at run time (agent | audio | video-enc | video-dec | controller | dsp). Multi-arch (amd64 + arm64).
# Ubuntu 26.04 pins PipeWire 1.6.2 on every node (the box's version; pinode's Debian 12 host has 1.2.7). The AES67
# processes run this PipeWire as *clients* of the host's daemon through its mounted socket.
# owlpoolfix: a ~100-line GStreamer element that lets upstream v4l2slh265dec's SAND128 output (Pi 5) negotiate
FROM ubuntu:26.04 AS gstbuild
ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends gcc libc6-dev pkg-config libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev ladspa-sdk \
 && rm -rf /var/lib/apt/lists/*
COPY gst/gstowlpoolfix.c gst/gstowlrtpstat.c ladspa/owl_ducker.c /src/
RUN gcc -O2 -Wall -fPIC -shared -o /src/libgstowlpoolfix.so /src/gstowlpoolfix.c \
      $(pkg-config --cflags --libs gstreamer-1.0 gstreamer-base-1.0 gstreamer-video-1.0) \
 && gcc -O2 -Wall -fPIC -shared -o /src/libgstowlrtpstat.so /src/gstowlrtpstat.c \
      $(pkg-config --cflags --libs gstreamer-1.0 gstreamer-base-1.0) \
 && gcc -O2 -Wall -fPIC -shared -o /src/owl_ducker.so /src/owl_ducker.c -lm

# N4 video tier 2 (JPEG XS). Ubuntu 26.04 packages neither piece, so both are built here, pinned:
#  - libSvtJpegxs (OpenVisualCloud SVT-JPEG-XS v0.10.0; x86-64 SIMD with runtime dispatch, aarch64 NEON) and GStreamer's
#    svtjpegxsenc/svtjpegxsdec, compiled from gst-plugins-bad 1.28.2 (the runtime's version; Ubuntu builds it without them)
#  - rtpjxsvpay/rtpjxsvdepay (RFC 9134), only on gst-plugins-rs main (MR 3166, merged 2026-08-12; not in any 0.15.x or
#    gstreamer-1.28.x tag), built against the system GStreamer from a pinned commit with its Cargo.lock
FROM ubuntu:26.04 AS jxsbuild
ARG DEBIAN_FRONTEND=noninteractive
ARG SVTJXS_REF=01bc7cc7123d2e4a3e330632fabec873f3cee62b
ARG GSTBAD_SHA256=6467e3964828f4d7d08bfe1fbb4d76287a1c8fa76674e59e101a149c020fefd7
ARG GSTRS_REF=a579eba513409d6ac4e4f29479dd61074fbb73e9
# parallel compile jobs (0 = all cores); build.sh passes 2 for the Pi, so its audio and video keep their cores
ARG JOBS=0
RUN apt-get update \
 && apt-get install -y --no-install-recommends gcc g++ make cmake nasm pkg-config git ca-certificates curl xz-utils \
      libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev cargo rustc binutils \
 && rm -rf /var/lib/apt/lists/*
RUN git init -q /src/svtjxs && cd /src/svtjxs \
 && git fetch -q --depth 1 https://github.com/OpenVisualCloud/SVT-JPEG-XS.git "$SVTJXS_REF" && git checkout -q FETCH_HEAD \
 && cmake -S . -B b -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/opt/owl-node -DCMAKE_INSTALL_LIBDIR=lib \
      -DBUILD_APPS=ON -DBUILD_TESTING=OFF \
 && J=$([ "$JOBS" = 0 ] && nproc || echo "$JOBS") && cmake --build b -j"$J" && cmake --install b
RUN cd /src && curl -fsSLO https://gstreamer.freedesktop.org/src/gst-plugins-bad/gst-plugins-bad-1.28.2.tar.xz \
 && echo "$GSTBAD_SHA256  gst-plugins-bad-1.28.2.tar.xz" | sha256sum -c - \
 && tar xf gst-plugins-bad-1.28.2.tar.xz gst-plugins-bad-1.28.2/ext/svtjpegxs \
 && cd gst-plugins-bad-1.28.2/ext/svtjpegxs \
 && export PKG_CONFIG_PATH=/opt/owl-node/lib/pkgconfig && echo "SvtJpegxs: $(pkg-config --modversion --cflags --libs SvtJpegxs)" \
 && gcc -O2 -Wall -fPIC -shared -o /src/libgstsvtjpegxs.so \
      -DVERSION='"1.28.2"' -DPACKAGE='"gst-plugins-bad"' -DPACKAGE_NAME='"GStreamer Bad Plug-ins (owl-node build)"' \
      -DGST_PACKAGE_ORIGIN='"https://gstreamer.freedesktop.org"' -DGETTEXT_PACKAGE='"gst-plugins-bad-1.0"' \
      gstsvtjpegxs.c gstsvtjpegxsenc.c gstsvtjpegxsdec.c -Wl,-rpath,/opt/owl-node/lib \
      $(pkg-config --cflags --libs gstreamer-1.0 gstreamer-base-1.0 gstreamer-video-1.0 SvtJpegxs)
RUN git init -q /src/gstrs && cd /src/gstrs \
 && git fetch -q --depth 1 https://gitlab.freedesktop.org/gstreamer/gst-plugins-rs.git "$GSTRS_REF" && git checkout -q FETCH_HEAD \
 && J=$([ "$JOBS" = 0 ] && nproc || echo "$JOBS") && CARGO_PROFILE_RELEASE_DEBUG=false nice cargo build -j"$J" --release --locked -p gst-plugin-rtp \
 && strip -o /src/libgstrsrtp.so target/release/libgstrsrtp.so && rm -rf target ~/.cargo/registry ~/.cargo/git

FROM ubuntu:26.04
ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      pipewire-bin libpipewire-0.3-modules libspa-0.2-modules \
      python3 python3-yaml python3-zeroconf python3-numpy python3-paho-mqtt tini ca-certificates \
      swh-plugins lsp-plugins-ladspa alsa-utils \
 && rm -rf /var/lib/apt/lists/*
# D2 dsp blocks: LADSPA plugins for filter-chain (Ubuntu's PipeWire has no LV2 loader: libspa-filter-graph-plugin-lv2 is
# not built, so LSP comes as its LADSPA build and x42/calf, LV2-only, are out until PipeWire is built with lilv).
# swh-plugins: sc4 compressor, fastLookaheadLimiter, parametric EQs, delays. owl_ducker: the house's own sidechain ducker.
# N3 video roles: GStreamer 1.28 (va: vah264enc/vah265enc on AMD/Intel; v4l2codecs: v4l2slh265dec on the Pi 5; srt;
# mpegts; pango overlays; libav for software H.264 decode on the Pi), Mesa's VA driver, and the Python bindings the
# pipeline processes are written in. GStreamer's va/v4l2codecs plugins only register elements when the device is there.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-bad \
      gstreamer1.0-plugins-ugly gstreamer1.0-libav gstreamer1.0-x mesa-va-drivers \
      python3-gi python3-gst-1.0 gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 \
 && rm -rf /var/lib/apt/lists/*
COPY --from=gstbuild /src/libgstowlpoolfix.so /src/libgstowlrtpstat.so /opt/owl-node/gst/
COPY --from=gstbuild /src/owl_ducker.so /opt/owl-node/ladspa/
COPY --from=jxsbuild /opt/owl-node/lib/libSvtJpegxs.so.0 /opt/owl-node/lib/
COPY --from=jxsbuild /opt/owl-node/bin/SvtJpegxsEncApp /opt/owl-node/bin/SvtJpegxsDecApp /opt/owl-node/bin/
COPY --from=jxsbuild /src/libgstsvtjpegxs.so /src/libgstrsrtp.so /opt/owl-node/gst/
COPY owlnode /opt/owl-node/owlnode
COPY dsp /opt/owl-node/dsp
COPY entrypoint.sh /usr/local/bin/owl-node
ENV PYTHONPATH=/opt/owl-node PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 GST_PLUGIN_PATH=/opt/owl-node/gst \
    LADSPA_PATH=/usr/lib/ladspa:/opt/owl-node/ladspa
LABEL org.opencontainers.image.title="owl-node" \
      org.opencontainers.image.description="AES67 audio, H.264/HEVC-over-SRT and JPEG XS RTP video endpoint with an AMWA NMOS IS-04/IS-05 agent; hub: NMOS controller, DSP designer and compiler" \
      org.opencontainers.image.source="https://github.com/MorningstarOwl"
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/owl-node"]
CMD ["agent"]
