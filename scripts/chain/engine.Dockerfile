FROM python:3.12-slim
RUN apt-get update && apt-get install --no-install-recommends -y libmagic1 redis-server \
    && rm -rf /var/lib/apt/lists/* && useradd --uid 10001 --no-create-home acceptance
COPY .acceptance/engine /engine
RUN pip install --no-cache-dir '/engine/client[observations]' '/engine[service,redis,auton,exchange]' \
    && python -c "import importlib.util; assert importlib.util.find_spec('atraxis') is None"
USER 10001:10001
WORKDIR /tmp
ENTRYPOINT ["python"]
