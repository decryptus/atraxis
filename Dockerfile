FROM python:3.15.1-slim-trixie@sha256:533b0b2f9b5d46fd097091cf826f27902f1d6c3bca714b6dc74551356aa97a51
RUN apt-get update \
    && apt-get install --no-install-recommends -y libmagic1 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml README.md LICENSE /app/
COPY atraxis /app/atraxis
RUN pip install --no-cache-dir . \
    && python -c "import atraxis.server" \
    && useradd --uid 10001 --no-create-home atraxis
USER 10001:10001
EXPOSE 9810
ENTRYPOINT ["atraxis"]
CMD ["serve", "--config", "/etc/atraxis/atraxis.yml"]
