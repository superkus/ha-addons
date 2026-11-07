# syntax=docker/dockerfile:1
#
# Remote Energy Meter add-on image.
#
# BUILD CONTEXT: the repository root, not this directory.
#
#   docker buildx build -f remote_energy_meter/Dockerfile --build-arg BUILD_ARCH=amd64 --build-arg BUILD_VERSION=dev .
#
# `pyproject.toml`, `README.md`, `requirements.txt`, and `src/remote_energy_meter/`
# live at the repository root so the package can be linted, type-checked, and
# tested on a developer machine without a container (see the design's Repository
# Layout).
#
# The base image's `/init` entry point is inherited unchanged: s6-overlay starts
# the `remote-energy-meter` longrun service defined under
# `remote_energy_meter/rootfs/etc/s6-overlay/`, and that service's `finish`
# script propagates the metering process exit status to the container exit
# status (Requirement 1.2).

# One multi-architecture base (amd64 + aarch64) pinned by digest: the tag says what
# it is, the digest says exactly which bytes. Supervisor has deprecated build.yaml
# and passes BUILD_FROM only to add-ons that still ship one; without it, this FROM
# line is what Supervisor builds from, so it is the single source of truth for every
# architecture. CI checks that the tag still resolves to this digest (a moved tag
# fails the build instead of silently changing the base); Dependabot proposes
# rebuilds of the same Python minor and Alpine. The base supplies Python 3.12,
# s6-overlay, bashio, and tzdata.
FROM ghcr.io/home-assistant/base-python:3.12-alpine3.22-2026.10.3@sha256:3d6d90b18cfb700c138b185e736a01a3ea049873bc3df6d8dbc725c31e7dc8ab

# Supervisor and the CI build always pass both. There are no defaults, and an ARG
# that is not passed expands to "" rather than failing, which would label the image
# with an empty architecture and version; the guard below fails the build instead.
ARG BUILD_ARCH
ARG BUILD_VERSION
RUN test -n "$BUILD_ARCH" && test -n "$BUILD_VERSION" \
    || { echo "BUILD_ARCH and BUILD_VERSION must be set (Supervisor and CI pass them)" >&2; exit 1; }

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

# Labels Supervisor reads (`io.hass.*`) plus OCI metadata; the latter used to come
# from the now-deprecated `build.yaml`, so they live here.
LABEL \
    io.hass.name="Remote Energy Meter" \
    io.hass.description="Meters power of network-attached devices and publishes it to Home Assistant" \
    io.hass.arch="${BUILD_ARCH}" \
    io.hass.type="addon" \
    io.hass.version="${BUILD_VERSION}" \
    org.opencontainers.image.title="Remote Energy Meter" \
    org.opencontainers.image.description="Home Assistant add-on that meters power of network-attached devices through per-family plugins and publishes power and energy over MQTT discovery." \
    org.opencontainers.image.licenses="MIT" \
    org.opencontainers.image.source="https://github.com/superkus/remote_energy_meter" \
    org.opencontainers.image.version="${BUILD_VERSION}"
