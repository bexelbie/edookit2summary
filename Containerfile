# ABOUTME: Container image for running the edookit summary gatherer.
# ABOUTME: Python image with beautifulsoup4 and markdown dependencies.

FROM python:3.13-slim

RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY edookit.py gather_updates.py entrypoint.sh ./
RUN chmod +x entrypoint.sh

# Config and state live on a bind-mounted volume
VOLUME /data

CMD ["/app/entrypoint.sh"]
