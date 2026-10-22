FROM atraxis-acceptance:ci
USER root
COPY .acceptance/engine/client /client
RUN pip install --no-cache-dir '/client[observations]' \
    && python -c "import importlib.util; assert importlib.util.find_spec('atropos') is None"
USER 10001:10001
