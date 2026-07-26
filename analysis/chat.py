"""Chat over a talk — ask how a speaker does something, get cited moments back.

What makes the answers worth reading is that they are grounded in three sources
at once, not just the transcript:

  1. semantic search over the spoken word (what was said)
  2. semantic search over the delivery/scene index (how it was performed)
  3. the precomputed analysis — measured pauses, pace, located rhetorical devices

So an answer can say "she holds a 3.8s silence before this line" as a measured
fact rather than as a plausible-sounding guess.

As everywhere else in this project, the model never emits a timestamp: it quotes
lines, and we locate those quotes in the word-level transcript.
"""

import re

from videodb import IndexType, SearchType
from videodb.exceptions import InvalidRequestError

from . import transcript as T
from .semantic import as_list, generate_json, locate_quote

# Questions that are really about physical delivery should also consult the
# visual index, not just the words.
DELIVERY_HINTS = (
    "body", "gesture", "hand", "posture", "stance", "move", "stage", "eye",
    "face", "expression", "look", "energy", "presence", "pace", "pause",
    "silence", "voice", "tone", "deliver", "confidence", "nervous",
)

SEARCH_WINDOW_CAP = 45.0   # a raw search shot can span 80-99s; we tighten it


def _wants_delivery(question):
    q = question.lower()
    return any(h in q for h in DELIVERY_HINTS)


def _tighten(shot_start, shot_end, sents, question):
    """Narrow a broad search window to the sentences that actually match.

    Semantic search returns a *window*, often 80-99 seconds, where only a line or
    two is on topic. Never widen beyond the returned bounds — only tighten.
    """
    inside = [s for s in sents if s["end"] > shot_start and s["start"] < shot_end]
    if not inside:
        return shot_start, shot_end, ""

    terms = {w for w in re.findall(r"[a-z]{4,}", question.lower())}
    scored = []
    for s in inside:
        words = set(re.findall(r"[a-z]{4,}", s["text"].lower()))
        scored.append((len(terms & words), s))

    best = max(scored, key=lambda pair: pair[0])[0]
    if best == 0:
        # Nothing lexically matched; keep the window but cap its length.
        end = min(shot_end, shot_start + SEARCH_WINDOW_CAP)
        text = " ".join(s["text"] for s in inside if s["start"] < end)
        return shot_start, end, text[:600]

    keep = [s for score, s in scored if score == best]
    start = min(s["start"] for s in keep)
    end = max(s["end"] for s in keep)
    # Give a little context either side, still inside the original window.
    start = max(shot_start, start - 4)
    end = min(shot_end, end + 4)
    text = " ".join(s["text"] for s in inside if s["end"] > start and s["start"] < end)
    return start, end, text[:600]


# Questions about technique cannot be answered by searching the transcript for
# the technique's name: a speaker demonstrating a pause does not say the word
# "pause", and nobody announces "here comes my rule of three". Searching for
# those terms finds people *talking about* the concept instead of doing it. So
# when a question names something we have already located, we seed the evidence
# straight from the analysis and let search supplement it.
TECHNIQUE_TRIGGERS = {
    "rule_of_three": ("rule of three", "three things", "triad", "list of three"),
    "anaphora": ("repeat", "repetition", "anaphora", "same phrase"),
    "rhetorical_question": ("question", "asks the audience", "rhetorical"),
    "contrast": ("contrast", "opposite", "juxtapos", "not x but"),
    "callback": ("callback", "call back", "refer back", "comes back"),
    "self_disclosure": ("vulnerab", "personal", "admit", "confess", "honest", "story about"),
    "direct_address": ("direct address", "speak to the audience", "says you", "connect"),
    "concrete_statistic": ("statistic", "number", "data", "research", "evidence"),
    "punchline": ("humour", "humor", "funny", "joke", "laugh", "comedy"),
}
PAUSE_TRIGGERS = ("pause", "silence", "beat", "stop", "breathe", "timing", "space")

# Positional questions are the other case text search gets wrong. Asking "how
# does the opening grab attention?" matches the *word* "opening" wherever it is
# spoken — which returned citations from 7-9 minutes into a talk whose opening
# is the first 90 seconds. When a question is about a place in the talk, the
# place is what decides the evidence, not lexical similarity.
OPENING_TRIGGERS = ("opening", "open ", "opens", "start", "begin", "first minute",
                    "hook", "introduc", "grab attention", "get attention")
ENDING_TRIGGERS = ("ending", "end ", "ends", "close", "closing", "finish",
                   "last minute", "final", "wrap up", "conclusion")
STRUCTURE_TRIGGERS = ("structure", "built", "build", "arc", "organis", "organiz",
                      "order", "flow", "shape", "outline", "sections")

OPENING_WINDOW = 150.0   # the first 2.5 minutes
ENDING_WINDOW = 150.0


def positional_range(analysis, question):
    """The slice of the talk a positional question is actually about."""
    q = question.lower()
    duration = float(analysis.get("duration") or 0)
    if any(t in q for t in OPENING_TRIGGERS):
        return 0.0, min(OPENING_WINDOW, duration or OPENING_WINDOW)
    if duration and any(t in q for t in ENDING_TRIGGERS):
        return max(0.0, duration - ENDING_WINDOW), duration
    return None


def structure_evidence(analysis, question, limit=4):
    """Seed evidence from the talk's mapped beats for structural questions."""
    q = question.lower()
    structure = analysis.get("structure") or {}
    beats = [b for b in (structure.get("beats") or []) if b.get("start") is not None]
    if not beats:
        return []

    beats.sort(key=lambda b: b["start"])

    if any(t in q for t in OPENING_TRIGGERS):
        picked = [b for b in beats if b.get("label") in ("hook", "context")] or beats[:2]
        picked = picked[:2]
    elif any(t in q for t in ENDING_TRIGGERS):
        # Take the last beats by POSITION, not by label. A "resolution" beat can
        # sit in the middle of a talk (it does in the reference one, at 11:12),
        # so label-matching would answer a question about the ending with a
        # moment from halfway through.
        picked = beats[-limit:]
    elif any(t in q for t in STRUCTURE_TRIGGERS):
        step = max(1, len(beats) // limit)
        picked = beats[::step]
    else:
        return []

    return [
        {
            "kind": "spoken",
            "start": max(0.0, b["start"] - 1.0),
            "end": b["start"] + 14.0,
            "text": f'[{b.get("label", "beat")}] {b.get("quote", "")}',
        }
        for b in picked[:limit]
    ]


def technique_evidence(analysis, sents, question, limit=4):
    """Seed evidence from what we already located, spread across the talk."""
    q = question.lower()
    picked = []

    if any(t in q for t in PAUSE_TRIGGERS):
        dramatic = sorted(
            (p for p in analysis["pauses"]["teachable"] if p["band"] == "dramatic"),
            key=lambda p: -p["duration"],
        )[:limit]
        for p in dramatic:
            picked.append({
                "kind": "spoken",
                "start": max(0.0, p["at"] - 3.0),
                "end": p["at"] + p["duration"] + 3.0,
                "text": f'[{p["duration"]:.1f}s silence] …{p["before"][-90:]} ⟨silence⟩ {p["after"][:90]}',
            })

    wanted = {name for name, hints in TECHNIQUE_TRIGGERS.items() if any(h in q for h in hints)}
    if wanted:
        for device in analysis.get("devices", []):
            if device["device"] in wanted:
                picked.append({
                    "kind": "spoken",
                    "start": device["start"],
                    "end": device["end"],
                    "text": f'[{device["device"].replace("_", " ")}] {device["quote"]}',
                })

    # Spread across the talk rather than stacking up wherever matches are dense.
    picked.sort(key=lambda e: e["start"])
    spread, last = [], -999
    for e in picked:
        if e["start"] - last >= 25:
            spread.append(e)
            last = e["start"]
    return spread[:limit]


def gather_context(video, analysis, sents, question, max_hits=5):
    """Collect evidence for a question from words, visuals and measurements."""
    evidence = structure_evidence(analysis, question)
    evidence += technique_evidence(analysis, sents, question)

    try:
        results = video.search(question, search_type=SearchType.semantic)
        shots = results.get_shots()[:max_hits]
    except InvalidRequestError as e:
        if "No results found" not in str(e):
            raise
        shots = []
    except Exception:  # noqa: BLE001 — search must never break the whole answer
        shots = []

    for shot in shots:
        start, end, text = _tighten(shot.start, shot.end, sents, question)
        if text:
            evidence.append({"kind": "spoken", "start": start, "end": end, "text": text})

    scene_index_id = analysis.get("scene_index_id")
    if _wants_delivery(question) and scene_index_id:
        try:
            scene_results = video.search(
                query=question,
                search_type=SearchType.semantic,
                index_type=IndexType.scene,
                scene_index_id=scene_index_id,
                score_threshold=0.3,
            )
            for shot in scene_results.get_shots()[:3]:
                evidence.append({
                    "kind": "delivery",
                    "start": shot.start,
                    "end": shot.end,
                    "text": (getattr(shot, "text", "") or "")[:400],
                })
        except InvalidRequestError:
            pass
        except Exception:  # noqa: BLE001
            pass

    # For a positional question, discard anything outside the part of the talk
    # actually being asked about — otherwise a lexical match from minute nine
    # answers a question about the first ninety seconds.
    window = positional_range(analysis, question)
    if window:
        lo, hi = window
        inside = [e for e in evidence if e["start"] < hi and e["end"] > lo]
        if inside:
            evidence = inside
        else:
            # Nothing matched in range: fall back to the transcript there, so the
            # answer is still about the right part of the talk.
            text = " ".join(s["text"] for s in sents if lo <= s["start"] < hi)
            evidence = [{"kind": "spoken", "start": lo, "end": min(hi, lo + 60),
                         "text": text[:1200]}] if text else evidence

    evidence.sort(key=lambda e: e["start"])
    return evidence


def _nearby_measurements(analysis, evidence):
    """Measured facts that overlap the evidence windows — the credibility layer."""
    spans = [(e["start"], e["end"]) for e in evidence] or [(0, analysis.get("duration", 0))]

    def overlaps(t):
        return any(a - 12 <= t <= b + 12 for a, b in spans)

    pauses = [
        f'{p["duration"]:.1f}s silence at {int(p["at"])}s, just before: "{p["after"][:70]}"'
        for p in analysis["pauses"]["teachable"]
        if p["band"] == "dramatic" and overlaps(p["at"])
    ][:6]

    devices = [
        f'{d["device"].replace("_", " ")} at {int(d["start"])}s: "{d["quote"][:90]}"'
        for d in analysis.get("devices", []) if overlaps(d["start"])
    ][:8]

    energy = [
        f'{int(s["start"])}s: {s["level"]} energy — {s["description"][:110]}'
        for s in (analysis.get("delivery", {}).get("timeline") or [])
        if s.get("level") and overlaps(s["start"])
    ][:4]

    return pauses, devices, energy


ANSWER_PROMPT = """You are a public-speaking coach helping someone learn from a talk they admire.

Their question: "%(question)s"

Below is evidence from the talk. Use ONLY this evidence — do not invent examples.

WHAT WAS SAID (from the transcript):
%(spoken)s

HOW IT WAS DELIVERED (from video analysis):
%(delivery)s

MEASURED FACTS (these are exact, not estimates):
%(measured)s

Write a coaching answer that:
- explains the technique concretely, naming what the speaker actually does
- cites specific moments by quoting the exact line from the transcript
- uses the measured facts where they support the point (pauses, pace, energy)

Format it as 2 to 3 SHORT paragraphs, each 2-3 sentences, separated by a blank
line. One dense block is hard to read on screen. Lead each paragraph with the
point, then the evidence.

Refer to the person as "the speaker" or "they". Never use "he" or "she": nothing
in this evidence tells you their gender, and guessing it misgenders a real person
— the same talk has been described as both in different answers.

Be direct and practical. 110-170 words total. No preamble, no flattery.

Return ONLY a JSON object:
{
  "answer": "<your coaching answer, paragraphs separated by a blank line>",
  "citations": [
    {
      "quote": "<exact line from the transcript above>",
      "technique": "<2-3 word name for what they do here, e.g. 'Dramatic pause'>",
      "note": "<max 9 words telling the learner what to watch for>"
    }
  ],
  "practice": "<one concrete drill, max 25 words>"
}

Include 2-4 citations, quoting the transcript exactly so they can be located.
The "technique" and "note" are burned onto the video clip as captions, so keep
them short, concrete and instructive — "Watch the 4-second silence land" beats
"effective use of pausing"."""


def answer(coll, video, analysis, sents, question):
    """Answer a question about the talk with located, playable citations."""
    evidence = gather_context(video, analysis, sents, question)
    pauses, devices, energy = _nearby_measurements(analysis, evidence)

    spoken = "\n".join(
        f'[{int(e["start"])}s] {e["text"]}' for e in evidence if e["kind"] == "spoken"
    ) or "(no strong transcript matches)"

    delivery = "\n".join(
        f'[{int(e["start"])}s] {e["text"]}' for e in evidence if e["kind"] == "delivery"
    ) or "\n".join(energy) or "(no delivery observations for this moment)"

    measured = "\n".join(pauses + devices) or "(no measured events near these moments)"

    result = generate_json(coll, ANSWER_PROMPT % {
        "question": question[:400],
        "spoken": spoken[:5000],
        "delivery": delivery[:2000],
        "measured": measured[:2000],
    })

    if not result or not isinstance(result, dict):
        return {
            "answer": "I couldn't analyse that one — try rephrasing, or ask about a specific "
                      "moment or technique.",
            "citations": [],
            "practice": None,
        }

    citations = []
    for item in as_list(result.get("citations"), "citations") or []:
        location = locate_quote(sents, item.get("quote", ""))
        if not location:
            continue  # unlocatable quote is dropped rather than shown
        citations.append({
            "quote": location["text"],
            "technique": str(item.get("technique") or "Technique")[:40],
            "note": str(item.get("note") or "")[:90],
            "start": location["start"],
            "end": location["end"],
        })

    # Fall back to the search windows if the model quoted nothing locatable, so
    # an answer is never left without playable evidence.
    if not citations and evidence:
        for e in evidence[:3]:
            citations.append({
                "quote": e["text"][:160],
                "technique": "Key moment",
                "note": "Matched this part of your question",
                "start": e["start"],
                "end": min(e["end"], e["start"] + 12),
            })

    # Drop overlapping citations — two clips of the same instant teach nothing
    # twice and waste a slot in the demonstration reel.
    citations.sort(key=lambda c: c["start"])
    deduped = []
    for c in citations:
        if deduped and c["start"] < deduped[-1]["end"] + 1.5:
            continue
        deduped.append(c)
    citations = deduped

    return {
        "answer": str(result.get("answer", "")).strip(),
        "citations": citations,
        "practice": (str(result.get("practice", "")).strip() or None),
    }


SUGGESTIONS = [
    "How does the opening grab attention?",
    "Show me how pauses are used for effect",
    "What does the body language do here?",
    "How is humour used without losing authority?",
    "How does the talk build to its main point?",
    "What makes the ending land?",
]
