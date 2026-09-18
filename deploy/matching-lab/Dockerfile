FROM python:3.12-slim-bookworm

LABEL org.opencontainers.image.title="SKY TV EPG Matching Lab"
LABEL org.opencontainers.image.description="Private, proposal-only EPG matching shadow runner"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONHASHSEED=0 \
    PYTHONIOENCODING=utf-8 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_NO_INPUT=1 \
    OMP_NUM_THREADS=1 \
    OPENBLAS_NUM_THREADS=1 \
    MKL_NUM_THREADS=1 \
    NUMEXPR_NUM_THREADS=1 \
    MALLOC_ARENA_MAX=2

WORKDIR /app

# Keep the runtime aligned with the versions exercised by the existing
# workflows.  --only-binary avoids an accidental compiler/toolchain path.
COPY requirements.txt ./
RUN python -m pip install --only-binary=:all: \
        -r requirements.txt \
    && python -m pip check

# The lab adapter intentionally reuses the frozen matcher and the current
# streaming/catalog safety code.  No workflow file or credential is copied.
COPY matching_lab/ ./matching_lab/
COPY scripts/ ./scripts/
COPY src/ ./src/
COPY knowledge/ ./knowledge/
COPY config/ ./config/

# Fail the image build if the package or its compatibility imports are broken.
RUN python -c "import matching_lab.pipeline" >/dev/null \
    && python -m matching_lab --help >/dev/null \
    && groupadd --gid 10001 matching-lab \
    && useradd --uid 10001 --gid 10001 --create-home \
        --home-dir /home/matching-lab --shell /usr/sbin/nologin matching-lab \
    && chown -R 10001:10001 /app

USER 10001:10001

# Cloud Run and local Docker supply only the shadow command's file arguments.
# Compatibility modules are included, so runtime IAM remains a mandatory
# boundary; the default entry point itself exposes only proposal generation.
ENTRYPOINT ["python", "-m", "matching_lab", "shadow"]
CMD ["--help"]
