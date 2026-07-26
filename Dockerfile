# Steal the Mic — container for Hugging Face Spaces.
#
# Spaces expects the app on port 7860 and runs as a non-root user, so writable
# paths have to be owned by that user or the first request that caches anything
# fails with a permission error.

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=7860 \
    HOME=/home/app

RUN useradd -m -u 1000 app

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=app:app analysis/     ./analysis/
COPY --chown=app:app backend/      ./backend/
COPY --chown=app:app frontend/     ./frontend/
COPY --chown=app:app tools/        ./tools/
COPY --chown=app:app ingest.py analyze_talk.py compute_metrics.py reindex_scenes.py ./

# Seed analyses ship with the image so the app is useful on the first click.
# Transcripts are NOT included — they are fetched from VideoDB at runtime, so a
# public image never redistributes a speaker's words.
COPY --chown=app:app data/seed/ ./data/seed/

# data/ is written at runtime (cache, memory, analyses of newly submitted talks).
RUN mkdir -p /app/data/cache /app/data/memory && chown -R app:app /app/data

USER app

EXPOSE 7860

# On boot, copy seed analyses into the live data directory if it is empty. Doing
# this at start rather than build time means a Space with persistent storage
# keeps anything added later instead of having it overwritten on redeploy.
CMD ["sh", "-c", "python -c \"\
import shutil, pathlib; \
seed = pathlib.Path('data/seed'); live = pathlib.Path('data'); \
[shutil.copyfile(f, live / f.name) for f in seed.glob('*.json') if not (live / f.name).exists()]; \
print('seeded', len(list(seed.glob('*.json'))), 'files')\" && \
exec uvicorn backend.main:app --host 0.0.0.0 --port ${PORT:-7860}"]
