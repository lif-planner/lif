FROM python:3.14-slim

ARG BUILD_VERSION=dev
ARG BUILD_ARCH=amd64
ARG LIF_REPOSITORY=https://github.com/lif-planner/lif.git
ARG LIF_REF=main

LABEL \
    io.hass.name="LiF Planner" \
    io.hass.description="Local-first financial life planning for German households." \
    io.hass.version="${BUILD_VERSION}" \
    io.hass.type="app" \
    io.hass.arch="${BUILD_ARCH}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIPENV_NOSPIN=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --upgrade pip pipenv

RUN git clone --depth 1 --branch "$LIF_REF" "$LIF_REPOSITORY" /app

RUN pipenv install --system --deploy

COPY run.sh /run.sh
RUN chmod a+x /run.sh

EXPOSE 8000

CMD ["/run.sh"]
