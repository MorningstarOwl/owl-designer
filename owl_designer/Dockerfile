# Owl Designer add-on (remote engine): the designer and Routing pages plus the thin NMOS controller behind them, all
# Python standard library. The audio engine (owl-node's dsp role, the registry, MediaMTX) runs on another machine.
# Built by the repository's GitHub Actions workflow from the REPOSITORY ROOT (the controller is copied from its source in
# owl-node/, so the add-on can never ship a stale copy):
#   docker build -f owl_designer/Dockerfile .
# Home Assistant pulls the prebuilt image (config.yaml `image:`) and never builds this itself.
ARG BUILD_FROM=python:3.13-alpine
FROM ${BUILD_FROM}
LABEL org.opencontainers.image.source="https://github.com/MorningstarOwl/owl-designer" \
      org.opencontainers.image.description="Owl Designer Home Assistant add-on (designer web UI; the engine runs elsewhere)" \
      org.opencontainers.image.licenses="MIT"
COPY owl-node/owlnode/__init__.py owl-node/owlnode/__main__.py owl-node/owlnode/controller.py /app/owlnode/
COPY owl-node/owlnode/web /app/owlnode/web
COPY owl_designer/run.py /run.py
WORKDIR /app
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s CMD wget -q -O /dev/null http://127.0.0.1:8361/api/info || exit 1
CMD ["python3", "-u", "/run.py"]
