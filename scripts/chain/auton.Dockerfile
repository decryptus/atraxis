FROM python:3.12.16-slim-trixie@sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159
RUN apt-get update && apt-get install --no-install-recommends -y libmagic1 \
    && rm -rf /var/lib/apt/lists/* && useradd --uid 10001 --no-create-home acceptance
COPY .auton-source /auton
RUN AUTON_PACKAGE=autond pip install --no-cache-dir /auton
USER 10001:10001
WORKDIR /tmp
ENTRYPOINT ["python"]
