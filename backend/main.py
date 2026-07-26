"""Steal the Mic — web portal.

Submit the YouTube URL of a speaker you admire. The talk is ingested and indexed
in the background, then you can ask how they do what they do and get answers
backed by playable moments from the video itself.

Run from the project root:
    uvicorn backend.main:app --reload
"""

import sys

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

import json
import re
import threading
import time
import uuid
from collections import defaultdict, deque
from pathlib import Path

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import videodb

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

load_dotenv(PROJECT_ROOT / ".env")
load_dotenv(PROJECT_ROOT.parent / ".env")

from analysis import chat as chat_engine      # noqa: E402
from analysis import reel as reel_builder     # noqa: E402
from analysis import transcript as T          # noqa: E402
from ingest import load_manifest, save_manifest  # noqa: E402

DATA_DIR = PROJECT_ROOT / "data"
FRONTEND_DIR = PROJECT_ROOT / "frontend"

MAX_URL_LENGTH = 400
MAX_QUESTION_LENGTH = 400
MAX_DEMO_MOMENTS = 5
# Demonstration clips are deliberately short: the cited line is the whole point,
# and anything longer makes the learner hunt for the lesson inside the clip.
DEMO_LEAD_IN = 1.2
DEMO_TAIL = 1.0
DEMO_MAX_SECONDS = 10.0
# Rate limits are per bucket, not one shared pool. A single shared "expensive"
# bucket meant asking a few questions used up the budget for submitting a new
# talk, and a normal session hit "Too many requests" almost immediately. Each
# action now gets a limit matched to what it actually costs.
RATE_LIMITS = {
    "general": (60, 60),    # (window seconds, max) — cheap reads
    "analyse": (300, 6),    # ingest + full indexing: genuinely expensive
    "chat": (60, 25),       # a few LLM calls per question
    "demo": (60, 12),       # one timeline render
}
DEFAULT_LIMIT = RATE_LIMITS["general"]

YOUTUBE_RE = re.compile(
    r"^https?://(www\.)?(youtube\.com/(watch\?v=|live/|shorts/)|youtu\.be/)[\w\-]+",
    re.I,
)

app = FastAPI(title="Steal the Mic")

_jobs = {}
_jobs_lock = threading.Lock()
_hits = defaultdict(deque)
_sentence_cache = {}


# ---------------------------------------------------------------- guardrails

def rate_limit(request: Request, bucket="general"):
    """Per-IP, per-bucket limiter — analysis and clip renders cost real credits."""
    window, limit = RATE_LIMITS.get(bucket, DEFAULT_LIMIT)
    host = request.client.host if request.client else "unknown"
    key = f"{bucket}:{host}"
    now = time.time()
    hits = _hits[key]
    while hits and now - hits[0] > window:
        hits.popleft()
    if len(hits) >= limit:
        wait = int(window - (now - hits[0])) + 1
        raise HTTPException(
            429,
            f"That's {limit} in {window // 60 or 1} minute(s) — try again in about {wait}s.",
        )
    hits.append(now)


def manifest_talks():
    path = DATA_DIR / "talks.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8")).get("talks", {})


def require_known_video(video_id):
    """A client-supplied id must be one this app actually tracks.

    Otherwise the endpoint becomes a free proxy for arbitrary media in the account.
    """
    if not video_id or len(video_id) > 120:
        raise HTTPException(400, "Invalid video id.")
    talks = manifest_talks()
    if video_id not in talks:
        raise HTTPException(404, "Unknown talk.")
    return talks[video_id]


def analysis_path(video_id):
    return DATA_DIR / f"analysis__{video_id}.json"


def load_analysis(video_id):
    path = analysis_path(video_id)
    if not path.exists():
        raise HTTPException(404, "This talk is still being analysed.")
    return json.loads(path.read_text(encoding="utf-8"))


def sentences_for(video_id):
    """Sentence list rebuilt from the cached word-level transcript."""
    if video_id in _sentence_cache:
        return _sentence_cache[video_id]
    cached = DATA_DIR / "cache" / f"{video_id}__transcript.json"
    if not cached.exists():
        raise HTTPException(404, "Transcript not available yet.")
    raw = json.loads(cached.read_text(encoding="utf-8"))
    sents = T.sentences(T.word_segments(raw))
    _sentence_cache[video_id] = sents
    return sents


# ------------------------------------------------------------- ingest + job

def _set_job(job_id, **fields):
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id].update(fields)


@app.post("/api/analyse")
def start_analysis(request: Request, background: BackgroundTasks, payload: dict):
    """Submit a talk URL. Returns a job id; everything happens in the background."""
    rate_limit(request, "analyse")

    url = (payload or {}).get("url", "")
    if not isinstance(url, str) or not url.strip():
        raise HTTPException(400, "Paste the YouTube URL of a talk.")
    url = url.strip()
    if len(url) > MAX_URL_LENGTH:
        raise HTTPException(400, "That URL is too long.")
    if not YOUTUBE_RE.match(url):
        raise HTTPException(400, "That doesn't look like a YouTube video URL.")

    # Already analysed? Skip straight to it rather than paying to redo the work.
    for video_id, record in manifest_talks().items():
        if record.get("source_url") == url and analysis_path(video_id).exists():
            job_id = uuid.uuid4().hex
            with _jobs_lock:
                _jobs[job_id] = {"status": "done", "stage": "Already analysed",
                                 "progress": 100, "video_id": video_id}
            return {"job_id": job_id, "cached": True}

    job_id = uuid.uuid4().hex
    with _jobs_lock:
        _jobs[job_id] = {"status": "queued", "stage": "Queued", "progress": 0}
    background.add_task(_process, job_id, url)
    return {"job_id": job_id, "cached": False}


def _process(job_id, url):
    """Upload, index and analyse — reporting real stages, not a fake spinner."""
    try:
        from analysis import metrics, semantic
        from analyze_talk import merge_pause_verdicts
        from compute_metrics import fetch_scenes, fetch_transcript
        from ingest import ingest_talk

        _set_job(job_id, status="running", stage="Fetching the video from YouTube", progress=8)

        conn = videodb.connect()
        manifest = load_manifest()
        coll = conn.get_collection(collection_id=manifest["collection_id"])

        record = ingest_talk(coll, manifest, url=url, kind="reference")
        _set_job(job_id, stage="Reading the transcript", progress=50)

        video = coll.get_video(record["videodb_id"])
        raw = fetch_transcript(video)
        scenes = fetch_scenes(video, record.get("scene_index_id"))

        _set_job(job_id, stage="Measuring pace and pauses", progress=62)
        result = metrics.compute(raw, record["length"])
        result.update({
            "title": record["title"],
            "videodb_id": record["videodb_id"],
            "source_url": record.get("source_url"),
            "scene_index_id": record.get("scene_index_id"),
        })

        usable = [s for s in scenes if "NO_SPEAKER_VISIBLE" not in s.get("description", "")]
        result["delivery"] = {
            "scenes_total": len(scenes),
            "scenes_with_speaker": len(usable),
            "timeline": [
                {
                    "start": round(s["start"], 1),
                    "end": round(s["end"], 1),
                    "level": (m.group(1).lower() if (m := re.search(
                        r"ENERGY:\s*(low|moderate|high)", s.get("description", ""), re.I)) else None),
                    "description": re.sub(r"\s*ENERGY:.*$", "", s.get("description", ""),
                                          flags=re.I | re.S).strip(),
                }
                for s in usable
            ],
        }

        _set_job(job_id, stage="Identifying speaking technique", progress=76)
        words = T.word_segments(raw)
        sents = T.sentences(words)
        chunks = T.chunk_sentences(sents, target_seconds=180)

        verdicts = semantic.classify_pause_intent(coll, result["pauses"]["needs_semantic_check"])
        merge_pause_verdicts(result["pauses"], verdicts)
        result["devices"] = semantic.extract_devices(coll, chunks, sents)

        _set_job(job_id, stage="Mapping how the talk is built", progress=92)
        result["structure"] = semantic.extract_structure(
            coll, sents, " ".join(s["text"] for s in sents))

        analysis_path(record["videodb_id"]).write_text(
            json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
        save_manifest(manifest)

        _set_job(job_id, status="done", stage="Ready", progress=100,
                 video_id=record["videodb_id"])
    except Exception as e:  # noqa: BLE001 — surface the real reason
        _set_job(job_id, status="error", stage="Failed", error=str(e)[:300])


@app.get("/api/job/{job_id}")
def job_status(job_id: str, request: Request):
    rate_limit(request)
    if len(job_id) > 64:
        raise HTTPException(400, "Invalid job id.")
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Unknown job.")
    return job


# ------------------------------------------------------------------- talks

@app.get("/api/talks")
def list_talks(request: Request):
    rate_limit(request)
    out = []
    for video_id, record in manifest_talks().items():
        if analysis_path(video_id).exists():
            out.append({
                "videodb_id": video_id,
                "title": record.get("title"),
                "length": record.get("length"),
                "source_url": record.get("source_url"),
            })
    return out


@app.get("/api/analysis/{video_id}")
def get_analysis(video_id: str, request: Request):
    rate_limit(request)
    require_known_video(video_id)
    return JSONResponse(load_analysis(video_id))


@app.get("/api/suggestions")
def suggestions(request: Request):
    rate_limit(request)
    return chat_engine.SUGGESTIONS


# -------------------------------------------------------------------- chat

@app.post("/api/chat/{video_id}")
def chat(video_id: str, request: Request, payload: dict):
    """Ask how the speaker does something; get an answer with playable moments."""
    rate_limit(request, "chat")
    require_known_video(video_id)

    question = (payload or {}).get("question", "")
    if not isinstance(question, str) or not question.strip():
        raise HTTPException(400, "Ask a question first.")
    question = question.strip()[:MAX_QUESTION_LENGTH]

    analysis = load_analysis(video_id)
    sents = sentences_for(video_id)

    conn = videodb.connect()
    manifest = load_manifest()
    coll = conn.get_collection(collection_id=manifest["collection_id"])
    video = coll.get_video(video_id)

    return chat_engine.answer(coll, video, analysis, sents, question)


@app.post("/api/demo/{video_id}")
def demo_clip(video_id: str, request: Request, payload: dict):
    """Compile the cited moments into one labelled demonstration clip."""
    rate_limit(request, "demo")
    require_known_video(video_id)

    raw_moments = (payload or {}).get("moments")
    if not isinstance(raw_moments, list) or not raw_moments:
        raise HTTPException(400, "Nothing to demonstrate.")

    analysis = load_analysis(video_id)
    duration = analysis.get("duration") or 0
    visible_spans = reel_builder.speaker_visible_spans(analysis)

    moments = []
    for item in raw_moments[:MAX_DEMO_MOMENTS]:
        if not isinstance(item, dict):
            continue
        try:
            start = max(0.0, float(item.get("start", 0)))
            end = float(item.get("end", start + 6))
        except (TypeError, ValueError):
            continue

        # Clamp to the real video: never trust client-supplied bounds.
        if duration:
            start = min(start, max(0.0, duration - 2))
            end = min(end, duration)
        if end <= start:
            end = start + 6

        # Tight padding only. The cited line is the lesson; a long run-up buries it.
        start = max(0.0, start - DEMO_LEAD_IN)
        end = min(end + DEMO_TAIL, duration or (end + DEMO_TAIL))
        if end - start > DEMO_MAX_SECONDS:
            end = start + DEMO_MAX_SECONDS

        # Prefer footage where the speaker is actually on screen — a delivery
        # technique shown over a slide teaches nothing.
        start, end, visible = reel_builder.snap_to_speaker(start, end, visible_spans)

        moments.append({
            "kind": "citation",
            "label": str(item.get("label") or "Technique")[:44].upper(),
            "start": start,
            "end": end,
            "detail": str(item.get("note", ""))[:120],
            "speaker_visible": visible,
        })

    if not moments:
        raise HTTPException(400, "Those moments couldn't be used.")
    moments.sort(key=lambda m: m["start"])

    conn = videodb.connect()
    stream_url, info = reel_builder.build_reel(conn, video_id, moments)
    return {"stream_url": stream_url, **info}


@app.get("/")
def index():
    return FileResponse(FRONTEND_DIR / "index.html")


if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")
