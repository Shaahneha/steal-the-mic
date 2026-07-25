"""Compute deterministic delivery metrics for ingested talks.

Caches the raw transcript and scene index locally so repeated analysis runs do
not re-hit VideoDB — iteration on the metric logic is then instant and free.

Usage:
    python compute_metrics.py                 # all ingested talks
    python compute_metrics.py --id <video_id> # one talk
    python compute_metrics.py --refresh       # ignore local cache
"""

import sys

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

import argparse
import json
from pathlib import Path

from dotenv import load_dotenv

import videodb

from analysis import metrics

PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(PROJECT_ROOT / ".env")
load_dotenv(PROJECT_ROOT.parent / ".env")

DATA_DIR = PROJECT_ROOT / "data"
CACHE_DIR = DATA_DIR / "cache"
MANIFEST_FILE = DATA_DIR / "talks.json"


def _cache_path(video_id, kind):
    return CACHE_DIR / f"{video_id}__{kind}.json"


def fetch_transcript(video, refresh=False):
    path = _cache_path(video.id, "transcript")
    if path.exists() and not refresh:
        return json.loads(path.read_text(encoding="utf-8"))
    data = video.get_transcript()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return data


def fetch_scenes(video, scene_index_id, refresh=False):
    if not scene_index_id:
        return []
    path = _cache_path(video.id, "scenes")
    if path.exists() and not refresh:
        return json.loads(path.read_text(encoding="utf-8"))
    data = video.get_scene_index(scene_index_id) or []
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return data


def summarise(record, result):
    pace, pauses, fillers = result["pace"], result["pauses"], result["fillers"]
    print(f"\n{'=' * 70}\n{record['title']}\n{'=' * 70}")
    print(f"  {result['duration']:.0f}s | {result['word_count']} words | "
          f"{result['overall_wpm']} WPM overall, {result['speaking_wpm']} while speaking")
    print(f"  Silence: {result['silence_ratio'] * 100:.0f}% of runtime")

    print(f"\n  PACE  {pace['min_wpm']}-{pace['max_wpm']} WPM (spread {pace['spread']})")
    print(f"    slowest @ {pace['slowest']['start']:.0f}s: \"{pace['slowest']['text'][:90]}...\"")
    print(f"    fastest @ {pace['fastest']['start']:.0f}s: \"{pace['fastest']['text'][:90]}...\"")

    c = pauses["counts"]
    print(f"\n  PAUSES  {pauses['total_detected']} detected | "
          f"{c['beat']} beats, {c['dramatic']} dramatic, {c['pending_check']} pending semantic check")
    print(f"    teachable rate: {pauses['rate_per_min']}/min ({pauses['dramatic_per_min']} dramatic/min)")
    if pauses["longest_teachable"]:
        p = pauses["longest_teachable"]
        print(f"    longest teachable: {p['duration']}s @ {p['at']:.0f}s")
        print(f"       ...{p['before'][-60:]} ⟨{p['duration']}s⟩ {p['after'][:60]}...")

    d, m = fillers["disfluencies"], fillers["discourse_markers"]
    print(f"\n  FILLERS  disfluencies {d['total']} ({d['per_min']}/min) {d['by_word'] or ''}")
    print(f"           markers      {m['total']} ({m['per_min']}/min) "
          f"{dict(list(m['by_phrase'].items())[:4])}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--id", help="Only analyse this video id")
    parser.add_argument("--refresh", action="store_true", help="Bypass local cache")
    args = parser.parse_args()

    manifest = json.loads(MANIFEST_FILE.read_text(encoding="utf-8"))
    conn = videodb.connect()
    coll = conn.get_collection(collection_id=manifest["collection_id"])

    targets = {k: v for k, v in manifest["talks"].items() if not args.id or k == args.id}
    if not targets:
        print("No matching talks in the manifest. Run ingest.py first.")
        return

    for video_id, record in targets.items():
        video = coll.get_video(video_id)
        raw = fetch_transcript(video, args.refresh)
        fetch_scenes(video, record.get("scene_index_id"), args.refresh)  # cache for later steps

        result = metrics.compute(raw, record["length"])
        out = DATA_DIR / f"metrics__{video_id}.json"
        out.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")

        summarise(record, result)
        print(f"\n  -> {out.name}")


if __name__ == "__main__":
    main()
