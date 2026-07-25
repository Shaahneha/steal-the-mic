"""Ingest a talk into the project's dedicated VideoDB collection.

Step 1 of the build: upload -> spoken-word index -> time-based scene index,
persisting the manifest after every mutation so a mid-run failure never loses
work (see DESIGN_DECISIONS.md, locked decision #7).

Usage:
    python ingest.py                      # ingest the configured reference talk
    python ingest.py --url <youtube_url>  # ingest another talk as a reference
    python ingest.py --file <path>        # ingest a local file (e.g. your own practice clip)
"""

import sys

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

import argparse
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

import videodb
from videodb import SceneExtractionType

# Resolve every path from __file__ — never a bare relative path, which silently
# breaks when the server is launched from backend/ (DESIGN_DECISIONS.md #8).
PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(PROJECT_ROOT / ".env")
load_dotenv(PROJECT_ROOT.parent / ".env")  # shared key across the video projects

DATA_DIR = PROJECT_ROOT / "data"
MANIFEST_FILE = DATA_DIR / "talks.json"

COLLECTION_NAME = "tedx-speaking-coach"

# The reference talk learners study. Swap freely — nothing downstream is
# hardcoded to this particular speaker.
REFERENCE_TALK = {
    "slug": "brene-brown-vulnerability",
    "url": "https://www.youtube.com/watch?v=X4Qm9cGRub0",
    "title": "The power of vulnerability — Brené Brown (TEDxHouston)",
}

# Seconds per sampled scene. Shot-based extraction proved far too coarse on a
# talk (21 shots / 20 min, a third of them slides) — see DESIGN_DECISIONS.md.
#
# 15s was also too coarse for a second job this index does: deciding whether the
# speaker is actually on screen for a given demonstration clip. One frame per
# 15s marked a whole window "speaker visible" when the footage cut to a slide
# seconds later, and a clip built on that shows a graphic instead of the speaker.
# 6s roughly triples the resolution for ~0.7 credits on a 20-minute talk.
SCENE_INTERVAL_SECONDS = 6

# Delivery-focused prompt. The generic "describe this scene" prompt returns
# setting and clothing; this one returns coachable technique. The two structured
# sentinels (NO_SPEAKER_VISIBLE / ENERGY:) make downstream parsing deterministic
# instead of us regexing prose like "I can't reliably describe...".
SCENE_PROMPT = """You are analyzing a public speaker's delivery technique from a single video frame.

If no speaker is visible in this frame (a slide, title card, audience-only shot, or black frame),
respond with exactly this and nothing else:
NO_SPEAKER_VISIBLE

Otherwise, describe ONLY the speaker's delivery in 2-4 sentences:
- posture and stance
- hand gestures, and what they convey
- facial expression and gaze direction
- apparent energy and openness

Do not describe clothing, background, stage decor, or other people present.
End your response with a final line in exactly this format:
ENERGY: <low|moderate|high>"""


def load_manifest() -> dict:
    if MANIFEST_FILE.exists():
        with open(MANIFEST_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"collection_id": None, "talks": {}}


def save_manifest(manifest: dict) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    with open(MANIFEST_FILE, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)


def get_project_collection(conn, manifest: dict):
    """Return the project's dedicated collection, creating it only if needed.

    Everything this project does is scoped to this collection so the account's
    unrelated videos are never picked up by a search or listing.
    """
    collection_id = os.getenv("TEDX_COLLECTION_ID") or manifest.get("collection_id")

    if collection_id:
        coll = conn.get_collection(collection_id=collection_id)
        print(f"Using collection {coll.id} ({getattr(coll, 'name', '?')})")
        return coll

    for existing in conn.get_collections():
        if getattr(existing, "name", None) == COLLECTION_NAME:
            print(f"Found existing collection {existing.id} ({COLLECTION_NAME})")
            return existing

    coll = conn.create_collection(
        name=COLLECTION_NAME,
        description="TEDx Learning — public speaking technique analysis",
    )
    print(f"Created collection {coll.id} ({COLLECTION_NAME})")
    return coll


def ingest_talk(coll, manifest, *, url=None, file_path=None, title=None,
                kind="reference", slug=None) -> dict:
    """Upload and fully index one talk, saving progress after each stage."""
    if not url and not file_path:
        raise ValueError("ingest_talk needs either url or file_path")

    # Skip re-uploading a reference talk we already have.
    if slug:
        for record in manifest["talks"].values():
            if record.get("slug") == slug and record.get("scene_index_id"):
                print(f"'{slug}' already fully ingested ({record['videodb_id']}) — skipping.")
                return record

    source_desc = url or file_path
    print(f"\nUploading: {source_desc}")

    if url:
        video = coll.upload(url=url, name=title)
    else:
        video = coll.upload(file_path=str(file_path), name=title)

    record = {
        "videodb_id": video.id,
        "slug": slug,
        "kind": kind,  # "reference" (a studied talk) or "user_upload" (personal, delete after session)
        "title": video.name or title,
        "source_url": url,
        "length": video.length,
        "spoken_indexed": False,
        "scene_index_id": None,
        "scene_interval": SCENE_INTERVAL_SECONDS,
        "ingested_at": datetime.now(timezone.utc).isoformat(),
    }
    manifest["talks"][video.id] = record
    manifest["collection_id"] = coll.id
    save_manifest(manifest)
    print(f"  id={video.id}  length={video.length:.0f}s (~{video.length / 60:.1f} min)")

    print("  Indexing spoken words...")
    video.index_spoken_words(force=True)
    record["spoken_indexed"] = True
    save_manifest(manifest)
    print("  ✓ transcript indexed")

    print(f"  Indexing scenes (time_based, every {SCENE_INTERVAL_SECONDS}s)...")
    try:
        scene_index_id = video.index_scenes(
            extraction_type=SceneExtractionType.time_based,
            extraction_config={
                "time": SCENE_INTERVAL_SECONDS,
                "select_frames": ["middle"],
            },
            prompt=SCENE_PROMPT,
            name="delivery-analysis",
        )
    except Exception as e:
        # index_scenes has no force= parameter; it errors if an index exists.
        match = re.search(r"id\s+([a-f0-9]+)", str(e))
        if not match:
            raise
        scene_index_id = match.group(1)
        print(f"  (reusing existing scene index {scene_index_id})")

    record["scene_index_id"] = scene_index_id
    save_manifest(manifest)
    print(f"  ✓ scene index {scene_index_id}")

    return record


def main():
    parser = argparse.ArgumentParser(description="Ingest a talk into VideoDB.")
    parser.add_argument("--url", help="Video URL (e.g. YouTube) to ingest")
    parser.add_argument("--file", help="Local video file to ingest")
    parser.add_argument("--title", help="Display title for the talk")
    parser.add_argument(
        "--kind",
        default="reference",
        choices=["reference", "user_upload"],
        help="reference = a talk to study; user_upload = personal practice clip",
    )
    args = parser.parse_args()

    conn = videodb.connect()
    manifest = load_manifest()
    coll = get_project_collection(conn, manifest)
    manifest["collection_id"] = coll.id
    save_manifest(manifest)

    if args.url or args.file:
        ingest_talk(
            coll, manifest,
            url=args.url,
            file_path=args.file,
            title=args.title,
            kind=args.kind,
        )
    else:
        ingest_talk(
            coll, manifest,
            url=REFERENCE_TALK["url"],
            title=REFERENCE_TALK["title"],
            kind="reference",
            slug=REFERENCE_TALK["slug"],
        )

    print(f"\n✓ Done. Manifest: {MANIFEST_FILE}")
    print(f"  Collection: {coll.id}")
    print(f"  Talks tracked: {len(manifest['talks'])}")


if __name__ == "__main__":
    main()
