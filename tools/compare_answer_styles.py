"""Generate the same answer in two writing styles so one can be chosen.

Both variants run against the SAME gathered evidence, so any difference is the
prompt's doing rather than a different set of moments.

Usage:  python tools/compare_answer_styles.py [--question "..."] [--id <video_id>]
"""

import sys

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import argparse
import json
from pathlib import Path

from dotenv import load_dotenv

import videodb

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
load_dotenv(PROJECT_ROOT / ".env")
load_dotenv(PROJECT_ROOT.parent / ".env")

from analysis import chat as chat_engine          # noqa: E402
from analysis import transcript as T              # noqa: E402
from analysis.semantic import as_list, generate_json, locate_quote  # noqa: E402

DATA_DIR = PROJECT_ROOT / "data"

SHARED_TAIL = """
Refer to the person as "the speaker" or "they". Never use "he" or "she": nothing
in this evidence tells you their gender.

Return ONLY a JSON object:
{
  "answer": "<your coaching answer, paragraphs separated by a blank line>",
  "citations": [
    {
      "quote": "<exact line from the transcript above>",
      "technique": "<2-3 word name for what they do here>",
      "note": "<max 9 words telling the learner what to watch for>"
    }
  ],
  "practice": "<one concrete drill, max 25 words>"
}

Include 2-4 citations, quoting the transcript exactly so they can be located."""

HEADER = """You are a public-speaking coach helping someone learn from a talk they admire.

Their question: "%(question)s"

Below is evidence from the talk. Use ONLY this evidence — do not invent examples.

WHAT WAS SAID (from the transcript):
%(spoken)s

HOW IT WAS DELIVERED (from video analysis):
%(delivery)s

MEASURED FACTS (these are exact, not estimates):
%(measured)s
"""

# ---------------------------------------------------------------- variant A

STYLE_A = HEADER + """
Write ONE connected argument, not a list of separate observations.

Structure it as a single through-line:
- Open with the ONE underlying principle the speaker is using. A single sentence.
- Then show how that same principle plays out across the talk, moving from its
  simplest use to its most sophisticated. Connect each example to the last with
  real connective tissue ("the same instinct shows up when...", "the sharpest
  version comes later...").
- Close by naming what the learner should take from the pattern as a whole.

Do NOT begin consecutive sentences or paragraphs with the same subject
("The speaker uses... The speaker also uses..."). That reads as a list.

2-3 short paragraphs, 110-170 words total. Be direct. No preamble, no flattery.
""" + SHARED_TAIL

# ---------------------------------------------------------------- variant B

STYLE_B = HEADER + """
Write it as a short lesson with a clear spine.

- Open with a one-sentence rule the learner could write down and use tomorrow.
- Then give the evidence as a progression, each step earning the next. Make the
  ordering do work: what changes between the first example and the last?
- Finish with the condition under which this technique fails or backfires, so the
  learner knows when NOT to copy it.

Every paragraph must advance the argument. If a paragraph could be deleted
without losing anything, cut it and write a better one.

2-3 short paragraphs, 110-170 words total. Be direct. No preamble, no flattery.
""" + SHARED_TAIL


# ------------------------------------------------------------ blend (A + B)

STYLE_BLEND = HEADER + """
Write a short lesson with a clear spine, in connected prose.

- Open with a one-sentence rule the learner could write down and use tomorrow.
- Then trace that rule through the talk as a progression, simplest use first,
  sharpest last. Make the ordering do work: something must change between the
  first example and the last, and you should say what.
- Join the examples with real connective tissue — "the same instinct shows up
  when...", "the sharpest version comes later..." — so it reads as one argument
  rather than a list of observations.
- Close with the condition under which this technique fails or backfires, so the
  learner knows when NOT to copy it.

Never begin consecutive sentences or paragraphs with the same subject
("The speaker uses... The speaker also uses..."). That is what makes writing
read as a list.

CRITICAL: every quote you use must be copied EXACTLY, word for word, from the
transcript above. Do not paraphrase, trim, tidy grammar, or merge two lines.
Quotes that do not appear verbatim are discarded, and the learner then gets no
video clip at all.

2-3 short paragraphs, 110-170 words total. Be direct. No preamble, no flattery.
""" + SHARED_TAIL


def render(coll, template, ctx, sents):
    result = generate_json(coll, template % ctx)
    if not result or not isinstance(result, dict):
        return None
    cites = []
    for item in as_list(result.get("citations"), "citations") or []:
        loc = locate_quote(sents, item.get("quote", ""))
        if loc:
            cites.append((item.get("technique", "?"), loc["start"], item.get("note", "")))
    return result.get("answer", ""), result.get("practice", ""), cites


def show(title, payload):
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)
    if not payload:
        print("  (generation failed)")
        return
    answer, practice, cites = payload
    for para in [p.strip() for p in answer.split("\n") if p.strip()]:
        print("\n" + para)
    print(f"\nPRACTISE: {practice}")
    print("\nMoments:")
    for tech, start, note in cites:
        print(f"  [{int(start)//60}:{int(start)%60:02d}] {tech} — {note}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--question", default="Show me how pauses are used for effect")
    ap.add_argument("--id")
    ap.add_argument("--blend", action="store_true", help="test the blended style only")
    ap.add_argument("--runs", type=int, default=2, help="repeat count for --blend")
    args = ap.parse_args()

    manifest = json.loads((DATA_DIR / "talks.json").read_text(encoding="utf-8"))
    video_id = args.id or next(iter(manifest["talks"]))
    analysis = json.loads((DATA_DIR / f"analysis__{video_id}.json").read_text(encoding="utf-8"))

    raw = json.loads((DATA_DIR / "cache" / f"{video_id}__transcript.json").read_text(encoding="utf-8"))
    sents = T.sentences(T.word_segments(raw))

    conn = videodb.connect()
    coll = conn.get_collection(collection_id=manifest["collection_id"])
    video = coll.get_video(video_id)

    # Gather evidence ONCE so both variants are judged on writing, not retrieval.
    evidence = chat_engine.gather_context(video, analysis, sents, args.question)
    pauses, devices, energy = chat_engine._nearby_measurements(analysis, evidence)
    ctx = {
        "question": args.question[:400],
        "spoken": "\n".join(f'[{int(e["start"])}s] {e["text"]}'
                            for e in evidence if e["kind"] == "spoken")[:5000] or "(none)",
        "delivery": ("\n".join(f'[{int(e["start"])}s] {e["text"]}'
                               for e in evidence if e["kind"] == "delivery")
                     or "\n".join(energy) or "(none)")[:2000],
        "measured": "\n".join(pauses + devices)[:2000] or "(none)",
    }

    print(f'Talk:     {analysis["title"]}')
    print(f'Question: "{args.question}"')
    print(f"Evidence: {len(evidence)} moments (identical for both options)")

    if args.blend:
        # Run it more than once: the risk with extra instructions is that quote
        # fidelity degrades, and one good run would not prove it holds.
        for i in range(args.runs):
            show(f"BLEND — run {i + 1}/{args.runs}",
                 render(coll, STYLE_BLEND, ctx, sents))
        return

    show("OPTION A — single through-line (one connected argument)",
         render(coll, STYLE_A, ctx, sents))
    show("OPTION B — rule, progression, and when it fails",
         render(coll, STYLE_B, ctx, sents))


if __name__ == "__main__":
    main()
