# Lifeline server: one stdlib Python process. No pip install; the image is the interpreter plus server/.
FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080

# Storm Watch and History need the IANA time zone database; fail the build, not the first storm check.
RUN python3 -c "from zoneinfo import ZoneInfo; ZoneInfo('America/Chicago')"

RUN useradd --system --uid 10001 lifeline
WORKDIR /app
COPY --chown=lifeline server/ ./
USER lifeline

EXPOSE 8080
CMD ["python3", "app.py"]
