FROM python:3.12-slim
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
