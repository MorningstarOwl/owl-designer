# Owl Designer add-on (remote engine): the designer and Routing pages plus the thin NMOS controller behind them, all
# Python standard library. The audio engine (owl-node's dsp role, the registry, MediaMTX) runs on another machine.
ARG BUILD_FROM
FROM ${BUILD_FROM}
# the controller and its web pages, as one archive (build-repo.sh makes it; see there why not a folder)
ADD owlnode.tar.gz /app/
COPY run.py /run.py
WORKDIR /app
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s CMD wget -q -O /dev/null http://127.0.0.1:8361/api/info || exit 1
CMD ["python3", "-u", "/run.py"]
