# ── Builder ──────────────────────────────────
FROM python:3.12-slim AS builder
WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential git && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml /build/pyproject.toml

# CPU-only torch first (saves ~600MB vs default CUDA bundle),
# then install the rest of the project dependencies.
RUN pip install --no-cache-dir torch==2.6.0 torchvision==0.21.0 --index-url https://download.pytorch.org/whl/cpu \
 && pip install --no-cache-dir .

# Strip packages that are build/optional deps not needed at runtime.
# matplotlib, fontTools, sympy are only used by ultralytics for plotting.
# polars is pulled in by fastembed but unused at runtime.
RUN pip uninstall -y matplotlib fonttools polars 2>/dev/null; \
    rm -rf /usr/local/lib/python3.12/site-packages/matplotlib* \
           /usr/local/lib/python3.12/site-packages/fontTools* \
           /usr/local/lib/python3.12/site-packages/_polars_runtime* \
           /usr/local/lib/python3.12/site-packages/polars* \
           /usr/local/lib/python3.12/site-packages/torch/test

# ── Runtime ──────────────────────────────────
FROM python:3.12-slim
WORKDIR /app

# OpenCV runtime deps (libGL + glib)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 libglib2.0-0 && rm -rf /var/lib/apt/lists/*

COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY app /app/app

RUN addgroup --system app && adduser --system --ingroup app --home /home/app app
# Finding #3: /app/models is the default MODELS_DIR. Create it owned by the
# app user so ultralytics' auto-download / cache path doesn't PermissionError
# on first run (previously it tried `mkdir /models` as uid=100). Bind-mount
# a preloaded host volume here in production.
RUN mkdir -p /home/app/.cache /data/photos /app/models && chown -R app:app /home/app /data/photos /app
ENV HOME=/home/app
USER app

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host=0.0.0.0", "--port=8000", "--timeout-keep-alive=300"]
