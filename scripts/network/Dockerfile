# Test-only ingress; the shipped Atraxis image remains unchanged.
FROM atraxis-acceptance:ci
USER root
RUN apt-get update && apt-get install --no-install-recommends -y nginx \
    && rm -rf /var/lib/apt/lists/*
USER 10001:10001
ENTRYPOINT ["nginx"]
