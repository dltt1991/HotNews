FROM python:3.11-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .
COPY config.example.json ./config.example.json
RUN mkdir -p /app/data

CMD ["hotnews", "--config", "/app/config.json", "serve"]

