"""Per-talk conversation memory — the "remember" leg of perceive → remember → act.

Two things are remembered, and both change what the next answer looks like:

  1. **What was asked and answered.** A follow-up like "what about the ending?"
     is meaningless without the previous turn, and a learner returning to a talk
     should find their thread where they left it rather than a blank page.

  2. **Which moments have already been shown.** Citing the same 3.4-second pause
     in answer after answer teaches nothing new, so previously-shown moments are
     deprioritised when fresh evidence exists.

Stored on disk rather than in process memory so it survives a restart — a cache
that evaporates is not a memory.
"""

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MEMORY_DIR = PROJECT_ROOT / "data" / "memory"

MAX_TURNS_KEPT = 40          # per talk, on disk
MAX_TURNS_IN_PROMPT = 3      # only the most recent shape a new answer
MOMENT_BUCKET = 5.0          # seconds; moments within this are "the same moment"

_lock = threading.Lock()


def _path(video_id):
    return MEMORY_DIR / f"{video_id}.json"


def load(video_id):
    """Every remembered turn for a talk, oldest first."""
    path = _path(video_id)
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("turns", [])
    except (json.JSONDecodeError, OSError):
        return []


def remember(video_id, question, answer, citations, practice=None):
    """Append a turn. Safe to call concurrently."""
    turn = {
        "asked_at": datetime.now(timezone.utc).isoformat(),
        "question": question,
        "answer": answer,
        "practice": practice,
        "moments": [
            {
                "start": c.get("start"),
                "technique": c.get("technique"),
                "quote": (c.get("quote") or "")[:200],
            }
            for c in (citations or [])
        ],
    }
    with _lock:
        turns = load(video_id)
        turns.append(turn)
        turns = turns[-MAX_TURNS_KEPT:]
        MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        _path(video_id).write_text(
            json.dumps({"video_id": video_id, "turns": turns}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    return turn


def forget(video_id):
    """Drop a talk's conversation entirely."""
    with _lock:
        _path(video_id).unlink(missing_ok=True)


def seen_moments(video_id):
    """Rounded start times already shown, so answers can avoid repeating them."""
    seen = set()
    for turn in load(video_id):
        for moment in turn.get("moments", []):
            start = moment.get("start")
            if isinstance(start, (int, float)):
                seen.add(round(start / MOMENT_BUCKET))
    return seen


def is_seen(seen, start):
    return round(float(start) / MOMENT_BUCKET) in seen


def conversation_context(video_id, limit=MAX_TURNS_IN_PROMPT):
    """Recent turns rendered for the prompt, so follow-ups actually follow on."""
    turns = load(video_id)[-limit:]
    if not turns:
        return ""
    lines = []
    for turn in turns:
        techniques = ", ".join(
            m["technique"] for m in turn.get("moments", []) if m.get("technique")
        )
        lines.append(f'They asked: "{turn["question"]}"')
        lines.append(f'You covered: {techniques or "general points"}')
    return "\n".join(lines)


def topics_covered(video_id):
    """Distinct techniques already discussed for this talk."""
    out = []
    for turn in load(video_id):
        for moment in turn.get("moments", []):
            name = (moment.get("technique") or "").strip().lower()
            if name and name not in out:
                out.append(name)
    return out
