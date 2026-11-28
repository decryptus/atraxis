# Same runtime as the repository Dockerfile, installed from the exact candidate
# wheel. The build context contains only that wheel.
ARG BASE
FROM ${BASE}
RUN apt-get update \
    && apt-get install --no-install-recommends -y libmagic1 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY atraxis-*-py3-none-any.whl /tmp/candidate/
RUN pip install --no-cache-dir /tmp/candidate/atraxis-*-py3-none-any.whl \
    && rm -rf /tmp/candidate \
    && python -c "import atraxis.server" \
    && useradd --uid 10001 --no-create-home atraxis
USER 10001:10001
EXPOSE 9810
ENTRYPOINT ["atraxis"]
CMD ["serve", "--config", "/etc/atraxis/atraxis.yml"]
