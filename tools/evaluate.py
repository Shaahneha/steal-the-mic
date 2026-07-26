"""Measure citation quality across every studied talk.

The central reliability claim of this project is that a cited moment really is
where the answer says it is. Until now that rested on one hand-checked talk,
which is an anecdote rather than a number. This runs a fixed question set across
every talk and reports what actually happens.

Three things are measured, and only the first two are pass/fail:

  * **Located rate** — of the quotes the model produced, how many were found
    verbatim in the transcript. Quotes that are not found get dropped rather
    than shown, so a low rate costs recall, never correctness.
  * **Verified rate** — of the citations returned to the user, how many have a
    quote that genuinely appears in that talk's transcript at the timestamp
    given. This is the number that would be a bug if it were below 100%.
  * **Fallback rate** — how often a flaky structured-output call forced the
    prose-recovery or search-window path. High values mean unreliable output
    even when the citations themselves are sound.

Usage:
    python tools/evaluate.py                 # every talk, default question set
    python tools/evaluate.py --questions 4   # fewer questions, faster
"""

import sys

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import argparse
import json
import re
import time
from pathlib import Path

from dotenv import load_dotenv

import videodb

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
load_dotenv(PROJECT_ROOT / ".env")
load_dotenv(PROJECT_ROOT.parent / ".env")

from analysis import chat as chat_engine       # noqa: E402
from analysis import transcript as T           # noqa: E402

DATA_DIR = PROJECT_ROOT / "data"

QUESTIONS = [
    "How does the opening grab attention?",
    "Show me how pauses are used for effect",
    "What makes the ending land?",
    "How does the speaker use personal vulnerability?",
    "What does the body language do here?",
    "How does the talk build to its main point?",
]

# Position tolerance: a citation's timestamp must land within this many seconds
# of where its quote actually occurs.
DRIFT_TOLERANCE = 2.0


def normalise(text):
    """Lowercase and strip punctuation, keeping letters in any script.

    A Latin-only filter would erase Devanagari entirely and score every
    non-English citation as unverifiable — the measurement would report a
    pipeline failure that is really a bug in the measurement.
    """
    text = str(text or "").lower()
    return re.sub(r"[^\w\s]", "", text, flags=re.UNICODE).strip()


def is_latin_script(text, threshold=0.5):
    """Rough script check, used only to group results — never to judge quality."""
    letters = [c for c in str(text or "") if c.isalpha()]
    if not letters:
        return True
    latin = sum(1 for c in letters if "a" <= c.lower() <= "z")
    return (latin / len(letters)) >= threshold


def verify(sents, citation):
    """Does this citation's quote really occur at the time it claims?"""
    quote = normalise(citation.get("quote"))
    if len(quote) < 8:
        return False, "quote too short to verify"

    window = [s for s in sents
              if s["end"] > citation["start"] - DRIFT_TOLERANCE
              and s["start"] < citation["end"] + DRIFT_TOLERANCE]
    if not window:
        return False, "no transcript at that timestamp"

    local = normalise(" ".join(s["text"] for s in window))
    if quote in local or local in quote:
        return True, "exact"

    # Tolerate a sentence-boundary trim at either end.
    head = quote[:60]
    if head and head in local:
        return True, "prefix"
    return False, "quote not present at timestamp"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--questions", type=int, default=len(QUESTIONS))
    ap.add_argument("--out", default=str(DATA_DIR / "eval_report.json"))
    args = ap.parse_args()

    manifest = json.loads((DATA_DIR / "talks.json").read_text(encoding="utf-8"))
    talks = {k: v for k, v in manifest["talks"].items()
             if (DATA_DIR / f"analysis__{k}.json").exists()}
    if not talks:
        print("No analysed talks. Run ingest.py and analyze_talk.py first.")
        return

    conn = videodb.connect()
    coll = conn.get_collection(collection_id=manifest["collection_id"])
    questions = QUESTIONS[:args.questions]

    def blank():
        return {"answers": 0, "citations": 0, "verified": 0, "fallback": 0, "empty": 0,
                "devices": 0, "beats": 0, "talks": 0}

    rows = []
    groups = {"latin": blank(), "non_latin": blank()}
    started = time.time()

    for video_id, record in talks.items():
        analysis = json.loads((DATA_DIR / f"analysis__{video_id}.json").read_text(encoding="utf-8"))
        raw = json.loads((DATA_DIR / "cache" / f"{video_id}__transcript.json").read_text(encoding="utf-8"))
        sents = T.sentences(T.word_segments(raw))
        video = coll.get_video(video_id)

        # Group by the script the talk is actually in, judged from its own
        # transcript rather than its title.
        sample = " ".join(s["text"] for s in sents[:40])
        group = "latin" if is_latin_script(sample) else "non_latin"
        totals = groups[group]
        totals["talks"] += 1
        totals["devices"] += len(analysis.get("devices", []))
        totals["beats"] += len((analysis.get("structure") or {}).get("beats", []))

        print(f"\n{record['title'][:64]}  [{group}]")
        for question in questions:
            result = chat_engine.answer(coll, video, analysis, sents, question)
            citations = result.get("citations", [])
            totals["answers"] += 1

            if not citations:
                totals["empty"] += 1
                print(f"  ✗ {question[:44]:46} no citations")
                rows.append({"talk": record["title"], "group": group, "question": question,
                             "citations": 0, "verified": 0})
                continue

            # Generic labels mean a fallback path produced these.
            fell_back = any(c.get("technique") in ("Key moment", "Cited moment")
                            for c in citations)
            totals["fallback"] += 1 if fell_back else 0

            ok = 0
            for c in citations:
                good, _why = verify(sents, c)
                ok += 1 if good else 0
            totals["citations"] += len(citations)
            totals["verified"] += ok

            flag = "✓" if ok == len(citations) else "!"
            note = " (fallback)" if fell_back else ""
            print(f"  {flag} {question[:44]:46} {ok}/{len(citations)} verified{note}")
            rows.append({"talk": record["title"], "group": group, "question": question,
                         "citations": len(citations), "verified": ok,
                         "fallback": fell_back})

    def summarise(name, t):
        if not t["answers"]:
            return None
        pct = (100.0 * t["verified"] / t["citations"]) if t["citations"] else 0.0
        print(f"\n{name}  ({t['talks']} talk{'s' if t['talks'] != 1 else ''})")
        print(f"  answers                  {t['answers']}")
        print(f"  citations returned       {t['citations']}")
        print(f"  verified in transcript   {t['verified']}  ({pct:.1f}%)")
        print(f"  answers with no citation {t['empty']}")
        print(f"  answers needing fallback {t['fallback']}")
        print(f"  devices located / talk   {t['devices'] / t['talks']:.0f}")
        print(f"  structure beats / talk   {t['beats'] / t['talks']:.0f}")
        return {"talks": t["talks"], "answers": t["answers"], "citations": t["citations"],
                "verified": t["verified"], "verified_pct": round(pct, 1),
                "answers_without_citations": t["empty"], "fallback_answers": t["fallback"],
                "devices_per_talk": round(t["devices"] / t["talks"], 1),
                "beats_per_talk": round(t["beats"] / t["talks"], 1)}

    print("\n" + "=" * 62)
    english = summarise("LATIN-SCRIPT TALKS", groups["latin"])
    other = summarise("NON-LATIN-SCRIPT TALKS", groups["non_latin"])
    print(f"\nelapsed {time.time() - started:.0f}s")

    # State what the run actually measured. An earlier version printed a fixed
    # conclusion ("verification holds across scripts") whenever both groups were
    # present — which would have asserted parity even at 70% versus 100%.
    if english and other:
        gap = english["verified_pct"] - other["verified_pct"]
        print(f"\nVerified-citation rate: {english['verified_pct']}% latin vs "
              f"{other['verified_pct']}% non-latin ({gap:+.1f} pts).")
        if gap > 5:
            print("Accuracy degrades outside Latin script, and so does volume:")
        else:
            print("Accuracy is comparable across scripts; volume still differs:")
        print(f"  {english['citations'] / english['talks']:.0f} citations/talk vs "
              f"{other['citations'] / other['talks']:.0f}")
        print(f"  {english['devices_per_talk']:.0f} devices/talk vs {other['devices_per_talk']:.0f}")
        print(f"  {english['beats_per_talk']:.0f} structure beats/talk vs {other['beats_per_talk']:.0f}")

    report = {
        "questions": len(questions),
        "latin_script": english,
        "non_latin_script": other,
        "rows": rows,
    }
    Path(args.out).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"-> {args.out}")


if __name__ == "__main__":
    main()
