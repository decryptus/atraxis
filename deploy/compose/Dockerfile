# syntax=docker/dockerfile:1
# engine_source is a separate, clean Atropos source checkout, supplied by Compose.
FROM python:3.12-slim AS runtime
RUN apt-get update && apt-get install --no-install-recommends -y libmagic1 \
    && rm -rf /var/lib/apt/lists/* && useradd --uid 10001 --no-create-home service
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 10001:10001

FROM runtime AS web
USER root
WORKDIR /app
COPY pyproject.toml README.md LICENSE /app/
COPY atraxis /app/atraxis
COPY --from=engine_source /client /client
RUN pip install --no-cache-dir '/client[observations]' .
WORKDIR /
RUN rm -rf /app /client \
    && python -c "import atraxis.server, importlib.util; assert importlib.util.find_spec('atropos') is None"
USER 10001:10001
ENTRYPOINT ["atraxis"]
CMD ["serve", "--config", "/etc/atraxis/atraxis.yml"]

FROM runtime AS engine
USER root
WORKDIR /engine
COPY --from=engine_source /pyproject.toml /engine/
COPY --from=engine_source /README.md /LICENSE /engine/
COPY --from=engine_source /atropos /engine/atropos
COPY --from=engine_source /client /client
RUN pip install --no-cache-dir '/client[observations]' '.[service,redis,auton,exchange]'
WORKDIR /
RUN rm -rf /engine /client \
    && python -c "import atropos.rule_server, importlib.util; assert importlib.util.find_spec('atraxis') is None"
USER 10001:10001
ENTRYPOINT ["atropos"]
CMD ["rules-serve", "--config", "/etc/atropos/engine.yml", "--port", "8082"]

FROM runtime AS proxy
USER root
RUN apt-get update && apt-get install --no-install-recommends -y nginx \
    && rm -rf /var/lib/apt/lists/*
USER 10001:10001
ENTRYPOINT ["nginx"]
CMD ["-c", "/etc/nginx/nginx.conf", "-g", "daemon off;"]
