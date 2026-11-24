FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MOONMANAGER_DB=/app/data/moonmanager.sqlite3

WORKDIR /opt/moonmanager
COPY main.py ./
COPY moonmanager/ ./moonmanager/
RUN mkdir -p /app/data

CMD ["python", "/opt/moonmanager/main.py"]
