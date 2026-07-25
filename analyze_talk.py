"""Full analysis of an ingested talk: deterministic metrics + semantic layer.

Produces data/analysis__<video_id>.json — the single artifact the web app reads.

Usage:
    python analyze_talk.py                  # analyse every ingested talk
    python analyze_talk.py --id <video_id>
    python analyze_talk.py --skip-semantic  # deterministic metrics only (free, instant)
"""

import sys

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

import argparse
import json
from pathlib import Path

from dotenv import load_dotenv

import videodb

from analysis import metrics, semantic
from analysis import transcript as T
from compute_metrics import fetch_scenes, fetch_transcript

PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(PROJECT_ROOT / ".env")
load_dotenv(PROJECT_ROOT.parent / ".env")

DATA_DIR = PROJECT_ROOT / "data"
MANIFEST_FILE = DATA_DIR / "talks.json"


def merge_pause_verdicts(pause_data, verdicts):
    """Fold LLM verdicts into the pauses step 2 left unclassified."""
    pending = pause_data["needs_semantic_check"]
    promoted = 0

    for i, pause in enumerate(pending):
        verdict = verdicts.get(i)
        if not verdict:
            continue
        # Never overwrite a deterministic call with a guess.
        if pause["classification"] in ("closing_applause", "audience_response"):
            continue
        pause["classification"] = verdict["type"] or pause["classification"]
        pause["why"] = verdict["why"]
        if verdict["type"] == "speaker_pause":
            pause["teachable"] = True
            pause_data["teachable"].append(pause)
            promoted += 1

    pause_data["teachable"].sort(key=lambda p: p["at"])
    pause_data["counts"]["promoted_by_semantic"] = promoted
    pause_data["counts"]["audience_response"] = sum(
        1 for p in pending if p["classification"] in ("audience_response", "closing_applause")
    )
    return promoted


def analyse(coll, video, record, skip_semantic=False):
    raw = fetch_transcript(video)
    scenes = fetch_scenes(video, record.get("scene_index_id"))

    words = T.word_segments(raw)
    sents = T.sentences(words)
    chunks = T.chunk_sentences(sents, target_seconds=180)

    print(f"  {len(words)} words -> {len(sents)} sentences -> {len(chunks)} chunks")

    result = metrics.compute(raw, record["length"])
    result["title"] = record["title"]
    result["videodb_id"] = record["videodb_id"]
    result["source_url"] = record.get("source_url")
    result["kind"] = record.get("kind")
    # Chat needs this to search the visual index for delivery questions.
    result["scene_index_id"] = record.get("scene_index_id")

    # Visual energy timeline from the scene index (sentinels make this exact).
    import re as _re
    usable_scenes = [s for s in scenes if "NO_SPEAKER_VISIBLE" not in s.get("description", "")]
    energy = []
    for s in usable_scenes:
        m = _re.search(r"ENERGY:\s*(low|moderate|high)", s.get("description", ""), _re.I)
        energy.append({
            "start": round(s["start"], 1),
            "end": round(s["end"], 1),
            "level": m.group(1).lower() if m else None,
            "description": _re.sub(r"\s*ENERGY:.*$", "", s.get("description", ""),
                                   flags=_re.I | _re.S).strip(),
        })
    result["delivery"] = {
        "scenes_total": len(scenes),
        "scenes_with_speaker": len(usable_scenes),
        "coverage": round(len(usable_scenes) / len(scenes), 2) if scenes else 0,
        "timeline": energy,
    }

    if skip_semantic:
        result["semantic_skipped"] = True
        return result

    pending = result["pauses"]["needs_semantic_check"]
    print(f"  Classifying {len(pending)} ambiguous pauses...")
    verdicts = semantic.classify_pause_intent(coll, pending)
    promoted = merge_pause_verdicts(result["pauses"], verdicts)
    print(f"    -> {len(verdicts)} classified, {promoted} were real speaker pauses")

    print("  Extracting rhetorical devices...")
    result["devices"] = semantic.extract_devices(coll, chunks, sents)
    print(f"    -> {len(result['devices'])} devices located in the transcript")

    print("  Extracting talk structure...")
    result["structure"] = semantic.extract_structure(
        coll, sents, " ".join(s["text"] for s in sents)
    )
    if result["structure"]:
        print(f"    -> hook: {result['structure']['hook_type']}, "
              f"{len(result['structure']['beats'])} beats located")

    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--id")
    parser.add_argument("--skip-semantic", action="store_true")
    args = parser.parse_args()

    manifest = json.loads(MANIFEST_FILE.read_text(encoding="utf-8"))
    conn = videodb.connect()
    coll = conn.get_collection(collection_id=manifest["collection_id"])

    targets = {k: v for k, v in manifest["talks"].items() if not args.id or k == args.id}
    for video_id, record in targets.items():
        print(f"\n{'=' * 70}\n{record['title']}\n{'=' * 70}")
        video = coll.get_video(video_id)
        result = analyse(coll, video, record, args.skip_semantic)

        out = DATA_DIR / f"analysis__{video_id}.json"
        out.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\n  -> {out.name}")


if __name__ == "__main__":
    main()
