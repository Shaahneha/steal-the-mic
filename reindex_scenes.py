"""Rebuild a talk's scene index at the current sampling interval.

Needed when SCENE_INTERVAL_SECONDS changes: index_scenes has no force flag, so
this creates a fresh index, points the manifest at it, and deletes the old one
rather than leaving an orphan behind.

Usage:  python reindex_scenes.py [--id <video_id>]
"""

import sys

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

import argparse
import json
import re
from pathlib import Path

from dotenv import load_dotenv

import videodb
from videodb import SceneExtractionType

PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(PROJECT_ROOT / ".env")
load_dotenv(PROJECT_ROOT.parent / ".env")

from ingest import SCENE_INTERVAL_SECONDS, SCENE_PROMPT  # noqa: E402

DATA_DIR = PROJECT_ROOT / "data"
MANIFEST_FILE = DATA_DIR / "talks.json"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--id")
    args = parser.parse_args()

    manifest = json.loads(MANIFEST_FILE.read_text(encoding="utf-8"))
    conn = videodb.connect()
    coll = conn.get_collection(collection_id=manifest["collection_id"])

    targets = {k: v for k, v in manifest["talks"].items() if not args.id or k == args.id}
    for video_id, record in targets.items():
        if record.get("scene_interval") == SCENE_INTERVAL_SECONDS:
            print(f"{record['title']}: already at {SCENE_INTERVAL_SECONDS}s — skipping.")
            continue

        print(f"{record['title']}: re-indexing at {SCENE_INTERVAL_SECONDS}s "
              f"(was {record.get('scene_interval')}s)...")
        video = coll.get_video(video_id)
        old_index = record.get("scene_index_id")

        try:
            new_index = video.index_scenes(
                extraction_type=SceneExtractionType.time_based,
                extraction_config={"time": SCENE_INTERVAL_SECONDS,
                                   "select_frames": ["middle"]},
                prompt=SCENE_PROMPT,
                name=f"delivery-analysis-{SCENE_INTERVAL_SECONDS}s",
            )
        except Exception as e:  # noqa: BLE001
            match = re.search(r"id\s+([a-f0-9]+)", str(e))
            if not match:
                raise
            new_index = match.group(1)
            print(f"  (reusing existing index {new_index})")

        record["scene_index_id"] = new_index
        record["scene_interval"] = SCENE_INTERVAL_SECONDS
        MANIFEST_FILE.write_text(json.dumps(manifest, indent=2, ensure_ascii=False),
                                 encoding="utf-8")
        print(f"  ✓ new index {new_index}")

        # Drop the stale cache so the next analysis reads the new index.
        cache = DATA_DIR / "cache" / f"{video_id}__scenes.json"
        cache.unlink(missing_ok=True)

        if old_index and old_index != new_index:
            try:
                video.delete_scene_index(old_index)
                print(f"  ✓ removed old index {old_index}")
            except Exception as e:  # noqa: BLE001
                print(f"  (could not delete old index: {str(e)[:80]})")

    print("\nNow re-run: python analyze_talk.py")


if __name__ == "__main__":
    main()
