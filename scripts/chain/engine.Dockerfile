FROM python:3.12.16-slim-trixie@sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159
RUN apt-get update && apt-get install --no-install-recommends -y libmagic1 redis-server \
    && rm -rf /var/lib/apt/lists/* && useradd --uid 10001 --no-create-home acceptance
COPY .acceptance/engine /engine
RUN pip install --no-cache-dir '/engine/client[observations]' '/engine[service,redis,auton,exchange]' \
    && python -c "import importlib.util; assert importlib.util.find_spec('atraxis') is None"
USER 10001:10001
WORKDIR /tmp
ENTRYPOINT ["python"]
