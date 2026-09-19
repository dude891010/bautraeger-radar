# Bauträger-Radar (Schritt 7). Ein Image, zwei Dienste (siehe docker-compose.yml):
# "dashboard" (Streamlit) und "scheduler" (radar/scheduler.py, wöchentlicher Lauf ohne Cron -
# siehe Begründung im Docstring von radar/scheduler.py). Gepinnte Abhängigkeiten aus
# requirements.lock.txt (CLAUDE.md: "gepinnte Abhängigkeiten").
FROM python:3.12-slim

# TZ: radar/scheduler.py plant nach lokaler Wanduhrzeit (config/settings.yaml: scheduler.hour),
# das ist als deutsche Zeit gemeint (Ratssitzungen finden in deutscher Zeit statt).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Europe/Berlin

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        tzdata \
    && ln -sf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo "$TZ" > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

# Nicht-Root-Nutzer (CLAUDE.md: "Nicht-Root-Nutzer")
RUN groupadd --gid 1000 radar && useradd --uid 1000 --gid radar --create-home --shell /bin/bash radar

WORKDIR /app

COPY requirements.lock.txt ./
RUN pip install --no-cache-dir -r requirements.lock.txt

COPY radar/ ./radar/
COPY config/ ./config/
COPY app.py ./
COPY scripts/ ./scripts/

# data/ ist im Betrieb ein Volume (siehe docker-compose.yml); die Unterordner hier schon anlegen
# und dem Nicht-Root-Nutzer gehören lassen, damit ein frisches, leeres Volume sofort beschreibbar ist.
RUN mkdir -p data/raw data/llm_cache data/logs && chown -R radar:radar /app

USER radar

EXPOSE 8501

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl --fail http://localhost:8501/_stcore/health || exit 1

# Default: Dashboard. Der "scheduler"-Dienst überschreibt command in docker-compose.yml.
CMD ["streamlit", "run", "app.py", "--server.address=0.0.0.0", "--server.port=8501"]
