# Built by .github/workflows/image.yml, which first copies parameters.py from custom_components/centrometal_local.
FROM python:3.12-alpine@sha256:e064671ca452259bab50e8e2c7bd896e39e9a343bb06ca25234adf6fe18d0fc7
WORKDIR /app
COPY commands.py gateway.py parameters.py protocol.py session.py store.py /app/
LABEL org.opencontainers.image.title="Centrometal Local MQTT Protocol" \
      org.opencontainers.image.source="https://github.com/Internetrevizor/centrometal-local-mqtt" \
      org.opencontainers.image.licenses="Apache-2.0"
CMD ["python3", "-u", "/app/gateway.py", "--config", "/data/options.json"]
