# Hugging Face Space (Docker SDK). Also works on any container host.
#
# HF routes traffic to the port named by `app_port` in README.md front matter -
# 7860 here - so the server must listen on that port on 0.0.0.0, not on the
# 5000/127.0.0.1 the laptop build uses. Both come from the environment, so this
# file and the local one run the same code.
FROM python:3.13-slim

# lxml and pdfplumber ship manylinux wheels, so no build toolchain is needed.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# HF runs the container as uid 1000. Files copied as root would leave the
# SQLite databases read-only, and the app writes to them on every run.
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH
WORKDIR $HOME/app

COPY --chown=user requirements.txt .
RUN pip install --user -r requirements.txt

COPY --chown=user . .

# The cache and run-output folders are gitignored, so they do not exist in a
# fresh clone. The app creates them on demand, but creating them up front keeps
# the first request from racing on directory creation.
RUN mkdir -p data/cache data/runs

ENV PORT=7860 \
    HOST=0.0.0.0
EXPOSE 7860

# One worker: a run's progress lives in memory on the process that started it,
# so a second worker would answer half the progress polls with "no such job".
# Concurrency comes from threads - the pipeline already runs one per agency.
# The long timeout is for the full 36-entity run, which is 252 HTTP lookups.
CMD ["gunicorn", "--chdir", "webapp", "app:app", \
     "--bind", "0.0.0.0:7860", \
     "--workers", "1", "--threads", "8", "--timeout", "600"]
