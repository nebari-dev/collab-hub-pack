FROM python:3.14-slim-trixie AS python
FROM ghcr.io/astral-sh/uv:0.12.7 AS uv

FROM python AS builder

WORKDIR /var/collab-hub-api

COPY pyproject.toml uv.lock ./
RUN --mount=from=uv,source=/uv,target=/bin/uv \
    uv sync \
    --no-progress --link-mode=copy --compile-bytecode \
    --frozen --python=$(which python) --no-default-groups --no-install-project

COPY src ./src
RUN --mount=from=uv,source=/uv,target=/bin/uv \
    --mount=type=bind,source=README.md,target=./README.md \
    --mount=type=bind,from=projectroot,source=LICENSE,target=./LICENSE \
    uv pip install \
    --no-progress --link-mode=copy --compile-bytecode \
    --no-deps .

FROM python AS runtime

RUN groupadd --gid 1000 nonroot && \
    useradd --shell "$(which bash)" --uid 1000 --gid 1000 nonroot
USER nonroot

WORKDIR /var/collab-hub-api
COPY --from=builder --chown=nonroot:nonroot /var/collab-hub-api/.venv /var/collab-hub-api/.venv
ENV PATH="/var/collab-hub-api/.venv/bin:${PATH}"

ENV COLLAB_HUB_API__SERVER__HOSTNAME=0.0.0.0

# The image ships pre-compiled bytecode (uv --compile-bytecode above), so
# nothing needs to write more at runtime. It matters for the read-only identity
# inventory (`python -m collab_hub_api.identity_inventory`, issue #65), which
# claims to write nothing to the filesystem: without this, importing it in a
# container whose site-packages happens to be writable would create __pycache__
# directories and make that claim false.
ENV PYTHONDONTWRITEBYTECODE=1

ENTRYPOINT ["/bin/bash"]
CMD ["-c", "python -m collab_hub_api"]
