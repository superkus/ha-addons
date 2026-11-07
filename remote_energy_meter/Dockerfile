# syntax=docker/dockerfile:1
#
# Remote Energy Meter add-on image.
#
# BUILD CONTEXT: the repository root, not this directory.
#
#   docker buildx build -f remote_energy_meter/Dockerfile \
#       --build-arg BUILD_FROM=ghcr.io/home-assistant/amd64-base-python:3.12-alpine3.22 .
#
# `pyproject.toml`, `README.md`, and `src/remote_energy_meter/` live at the
# repository root so the package can be linted, type-checked, and tested on a
# developer machine without a container (see the design's Repository Layout).
#
# The base image's `/init` entry point is inherited unchanged: s6-overlay starts
# the `remote-energy-meter` longrun service defined under
# `remote_energy_meter/rootfs/etc/s6-overlay/`, and that service's `finish`
# script propagates the metering process exit status to the container exit
# status (Requirement 1.2).

ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base-python:3.12-alpine3.22
FROM ${BUILD_FROM}

ARG BUILD_ARCH="amd64"
ARG BUILD_DATE
ARG BUILD_DESCRIPTION="Meters power of network-attached devices and publishes it to Home Assistant"
ARG BUILD_NAME="Remote Energy Meter"
ARG BUILD_REF
ARG BUILD_REPOSITORY
ARG BUILD_VERSION="0.1.2"

# PYTHONUNBUFFERED is load-bearing: the Diagnostics_Logger writes one record per
# line to stdout and Supervisor reads that stream live (Requirement 9.1).
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

WORKDIR /usr/src/app

COPY pyproject.toml README.md requirements.txt ./
COPY src/ ./src/

# The generic IPMI plugin invokes this read-only client directly (never through a
# shell). Install it before Python dependencies so packaging tests can pin the runtime
# prerequisite independently of pip.
RUN apk add --no-cache ipmitool=1.8.19-r1

# Dependencies come only from the hash-locked requirements.txt (every artifact pinned by
# sha256), then the package itself without resolving anything further. The image is built
# on the user's host, so this is what makes "what CI audited" and "what runs" the same set.
RUN \
    pip3 install --no-cache-dir --require-hashes -r requirements.txt \
    && pip3 install --no-cache-dir --no-deps . \
    && rm -rf /usr/src/app

# s6-overlay service definition and the `user` bundle entry that enables it.
COPY remote_energy_meter/rootfs /

# Re-assert the executable bit: a checkout on a filesystem without POSIX modes
# would otherwise produce a service that s6 cannot start.
RUN chmod a+x \
    /etc/s6-overlay/s6-rc.d/remote-energy-meter/run \
    /etc/s6-overlay/s6-rc.d/remote-energy-meter/finish

WORKDIR /

LABEL \
    io.hass.name="${BUILD_NAME}" \
    io.hass.description="${BUILD_DESCRIPTION}" \
    io.hass.arch="${BUILD_ARCH}" \
    io.hass.type="addon" \
    io.hass.version="${BUILD_VERSION}" \
    org.opencontainers.image.title="${BUILD_NAME}" \
    org.opencontainers.image.description="${BUILD_DESCRIPTION}" \
    org.opencontainers.image.licenses="MIT" \
    org.opencontainers.image.source="${BUILD_REPOSITORY}" \
    org.opencontainers.image.revision="${BUILD_REF}" \
    org.opencontainers.image.created="${BUILD_DATE}" \
    org.opencontainers.image.version="${BUILD_VERSION}"
