"""Package the studied talks as deployable seed data.

A deployed demo has to be useful on the first click — "judgeable in under five
minutes" — so the analyses ship with the build rather than being recomputed on
boot, which would cost credits and make a judge wait.

What ships and what deliberately does not:

  ships       manifest + analysis JSON per talk (derived measurements, technique
              labels, and the short quotes needed to cite a moment)
  stays out   the transcript cache — full verbatim transcripts are the speaker's
              copyrighted words. The app fetches those from VideoDB at runtime
              instead, so a public image never redistributes them.

Usage:  python tools/build_seed.py
"""

import sys

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import json
import shutil
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
SEED_DIR = DATA_DIR / "seed"


def strip_verbatim(analysis):
    """Remove speech text that the runtime can re-derive from the transcript.

    Every pause carries the six seconds of speech either side of it. Across a
    few hundred pauses that adds up: measured on the shipped set, 12-27% of a
    talk was present as contiguous verbatim 8-word runs, which is real
    excerpting rather than the "no transcript" the README claims. Timings are
    kept — they are measurements, not the speaker's words — and the surrounding
    text is rebuilt on demand once the transcript is fetched.

    Deliberately kept: device and structure-beat quotes. Those are short, they
    are what a citation actually shows, and each is attributed and linked back
    to the original video.
    """
    for bucket in ("teachable", "needs_semantic_check"):
        for pause in analysis.get("pauses", {}).get(bucket, []) or []:
            pause.pop("before", None)
            pause.pop("after", None)

    longest = analysis.get("pauses", {}).get("longest_teachable")
    if isinstance(longest, dict):
        longest.pop("before", None)
        longest.pop("after", None)

    # Pace samples embed a long stretch of speech for the fastest/slowest minute.
    for key in ("slowest", "fastest"):
        sample = analysis.get("pace", {}).get(key)
        if isinstance(sample, dict):
            sample.pop("text", None)

    return analysis


def main():
    manifest_file = DATA_DIR / "talks.json"
    if not manifest_file.exists():
        print("No data/talks.json — nothing to package.")
        return

    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    talks = manifest.get("talks", {})

    ready = {vid: rec for vid, rec in talks.items()
             if (DATA_DIR / f"analysis__{vid}.json").exists()}
    if not ready:
        print("No analysed talks to package.")
        return

    if SEED_DIR.exists():
        shutil.rmtree(SEED_DIR)
    SEED_DIR.mkdir(parents=True)

    (SEED_DIR / "talks.json").write_text(
        json.dumps({"collection_id": manifest.get("collection_id"), "talks": ready},
                   indent=2, ensure_ascii=False),
        encoding="utf-8")

    total = before_total = 0
    for vid, rec in ready.items():
        src = DATA_DIR / f"analysis__{vid}.json"
        analysis = json.loads(src.read_text(encoding="utf-8"))
        before_total += src.stat().st_size

        strip_verbatim(analysis)

        dst = SEED_DIR / f"analysis__{vid}.json"
        dst.write_text(json.dumps(analysis, ensure_ascii=False), encoding="utf-8")
        size = dst.stat().st_size
        total += size
        print(f"  {rec.get('title','')[:52]:54} {size/1024:6.0f} KB")

    print(f"\n{len(ready)} talks packaged, {total/1024:.0f} KB "
          f"(from {before_total/1024:.0f} KB) -> {SEED_DIR}")
    print("Excluded: full transcripts, and the surrounding text stored on every")
    print("pause. Both are re-derived at runtime from VideoDB.")


if __name__ == "__main__":
    main()
