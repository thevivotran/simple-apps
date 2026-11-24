# Standard library only: no pip install, so the base image is the only thing to patch.
FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 DATA_DIR=/data PORT=8080
WORKDIR /srv

COPY simple_apps ./simple_apps
RUN mkdir /data && chown 1000:1000 /data
USER 1000:1000
VOLUME /data
EXPOSE 8080

CMD ["python", "-m", "simple_apps"]
