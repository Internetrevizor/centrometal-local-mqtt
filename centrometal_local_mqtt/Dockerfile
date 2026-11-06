FROM python:3.12-alpine
WORKDIR /app
COPY gateway.py /app/gateway.py
CMD ["python3", "-u", "/app/gateway.py", "--config", "/data/options.json"]
