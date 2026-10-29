# syntax=docker/dockerfile:1@sha256:4edf897a3ffa55b89f906fc8cc78afdb3f1834cc9c7083565e611a8a7d5fe99e
# engine_source is a separate, clean Atropos source checkout, supplied by Compose.
FROM python:3.12.16-slim-trixie@sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159 AS runtime
COPY deploy/compose/locks/debian.sources /etc/apt/sources.list.d/debian.sources
RUN apt-get update && apt-get install --no-install-recommends -y libmagic1 \
    && rm -rf /var/lib/apt/lists/* && useradd --uid 10001 --no-create-home service
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 10001:10001

FROM runtime AS builder
USER root
COPY deploy/compose/locks /locks
RUN python -m pip download --no-cache-dir --only-binary=:all: --no-deps \
    --require-hashes -r /locks/build.txt -d /build-tools
RUN --network=none python -m pip install --no-index --no-deps /build-tools/*.whl

FROM builder AS web-build
WORKDIR /app
COPY pyproject.toml README.md LICENSE /app/
COPY atraxis /app/atraxis
COPY --from=engine_source /client /client
RUN python -m pip download --no-cache-dir --prefer-binary --no-deps --no-build-isolation \
    --require-hashes -r /locks/web.txt -d /sources
RUN --network=none python -m pip wheel --no-index --no-deps --no-build-isolation \
    --wheel-dir /wheels /sources/* /client /app \
    && python /locks/record-environment.py build web

FROM runtime AS web
USER root
COPY deploy/compose/locks /locks
COPY --from=web-build /wheels /wheels
COPY --from=web-build /build-record.json /build-record.json
RUN --network=none python -m pip install --no-index --no-deps /wheels/*.whl \
    && python -m pip check \
    && python /locks/record-environment.py installed web \
    && rm -rf /locks /wheels /build-record.json \
    && python -c "import atraxis.server, importlib.util; assert importlib.util.find_spec('atropos') is None"
USER 10001:10001
ENTRYPOINT ["atraxis"]
CMD ["serve", "--config", "/etc/atraxis/atraxis.yml"]

FROM builder AS engine-build
WORKDIR /engine
COPY --from=engine_source /pyproject.toml /engine/
COPY --from=engine_source /README.md /LICENSE /engine/
COPY --from=engine_source /atropos /engine/atropos
COPY --from=engine_source /client /client
RUN python -m pip download --no-cache-dir --prefer-binary --no-deps --no-build-isolation \
    --require-hashes -r /locks/engine.txt -d /sources
RUN --network=none python -m pip wheel --no-index --no-deps --no-build-isolation \
    --wheel-dir /wheels /sources/* /client /engine \
    && python /locks/record-environment.py build engine

FROM runtime AS engine
USER root
COPY deploy/compose/locks /locks
COPY --from=engine-build /wheels /wheels
COPY --from=engine-build /build-record.json /build-record.json
RUN --network=none python -m pip install --no-index --no-deps /wheels/*.whl \
    && python -m pip check \
    && python /locks/record-environment.py installed engine \
    && rm -rf /locks /wheels /build-record.json \
    && python -c "import atropos.rule_server, importlib.util; assert importlib.util.find_spec('atraxis') is None"
USER 10001:10001
ENTRYPOINT ["atropos"]
CMD ["rules-serve", "--config", "/etc/atropos/engine.yml", "--port", "8082"]

FROM runtime AS proxy
USER root
COPY deploy/compose/locks /locks
RUN apt-get update && apt-get install --no-install-recommends -y nginx \
    && rm -rf /var/lib/apt/lists/* \
    && python /locks/record-environment.py installed proxy \
    && rm -rf /locks
USER 10001:10001
ENTRYPOINT ["nginx"]
CMD ["-c", "/etc/nginx/nginx.conf", "-g", "daemon off;"]
