# Built by .github/workflows/image.yml, which first copies parameters.py from custom_components/centrometal_local.
FROM python:3.14-alpine@sha256:5862d971d1f0ae26b258cefa4e893556b1d3aba81bd2ff30888704ad378bab67
WORKDIR /app
COPY commands.py gateway.py parameters.py protocol.py session.py store.py /app/
LABEL org.opencontainers.image.title="Centrometal Local MQTT Protocol" \
      org.opencontainers.image.source="https://github.com/Internetrevizor/centrometal-local-mqtt" \
      org.opencontainers.image.licenses="Apache-2.0"
CMD ["python3", "-u", "/app/gateway.py", "--config", "/data/options.json"]
