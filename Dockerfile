# Agenda Watch — hosted job image.
#   podman build -t agendawatch .                      (OCR for scanned PDFs is on; --build-arg WITH_OCR=false for a smaller image)
#   podman run --rm -e AGENDAWATCH_STORAGE=local -v ./hosted:/data -e AGENDAWATCH_LOCAL_DIR=/data agendawatch
# Push the built image to your own registry: the runtime shouldn't pull from Docker Hub (anonymous
# pulls from cloud IPs get rate-limited; see docs/project-plan.md, Phase 0).
FROM python:3.12-slim

ARG WITH_OCR=true
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_ROOT_USER_ACTION=ignore \
    AGENDAWATCH_LOG_FORMAT=json \
    AGENDAWATCH_CONFIG=/app/config.yaml \
    TZ=America/Chicago

# tzdata: timestamps in the report ("updated", "first seen") are local time, so run on Central time.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata \
 && if [ "$WITH_OCR" = "true" ]; then apt-get install -y --no-install-recommends ocrmypdf; fi \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt requirements-hosted.txt ./
RUN pip install -r requirements.txt -r requirements-hosted.txt

COPY agendawatch/ agendawatch/
COPY agenda_watch.py config.yaml ./

RUN useradd --create-home --uid 10001 agendawatch
USER agendawatch

ENTRYPOINT ["python", "agenda_watch.py"]
CMD ["job"]
