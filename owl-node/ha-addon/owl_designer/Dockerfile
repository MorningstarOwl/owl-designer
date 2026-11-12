# Owl Designer add-on (remote engine): the designer and Routing pages plus the thin NMOS controller behind them, all
# Python standard library. The audio engine (owl-node's dsp role, the registry, MediaMTX) runs on another machine.
# Built by the repository's GitHub Actions workflow (multi-arch, python:3.13-alpine); a local `docker build` works too.
ARG BUILD_FROM=python:3.13-alpine
FROM ${BUILD_FROM}
LABEL org.opencontainers.image.source="https://github.com/MorningstarOwl/owl-designer" \
      org.opencontainers.image.description="Owl Designer Home Assistant add-on (designer web UI; the engine runs elsewhere)" \
      org.opencontainers.image.licenses="MIT"
# the controller and its web pages, as one archive (build-repo.sh makes it; see there why not a folder)
ADD owlnode.tar.gz /app/
COPY run.py /run.py
WORKDIR /app
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s CMD wget -q -O /dev/null http://127.0.0.1:8361/api/info || exit 1
CMD ["python3", "-u", "/run.py"]
