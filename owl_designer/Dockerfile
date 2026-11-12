# TEMPORARY diagnostic (reverted next commit): list what Home Assistant's builder sends as the build context
ARG BUILD_FROM
FROM ${BUILD_FROM}
COPY . /ctx
RUN echo "=== owl-designer build context ===" && ls -la /ctx && find /ctx | sort | head -60 && echo "=== end ===" && exit 1
