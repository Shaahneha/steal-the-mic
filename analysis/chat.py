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

from . import memory
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


# Evidence must never be cut mid-word. The model is instructed to quote it
# verbatim, so any truncation we introduce comes straight back as a broken quote
# ("a career that takes m") and lands in the learner's answer. Always cut on a
# sentence boundary, or failing that a word boundary — never a character count.

def _join_sentences(sentences, max_chars=700):
    """Whole sentences up to a budget — never a partial one."""
    out, total = [], 0
    for s in sentences:
        text = s["text"].strip()
        if out and total + len(text) > max_chars:
            break
        out.append(text)
        total += len(text) + 1
    return " ".join(out)


def _trim_words(text, max_chars):
    """Last-resort truncation that still lands on a word boundary."""
    text = " ".join(str(text or "").split())
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rsplit(" ", 1)[0]


def _sentence_ending_by(sents, t, slack=0.5):
    candidates = [s for s in sents if s["end"] <= t + slack]
    return candidates[-1] if candidates else None


def _sentence_starting_after(sents, t, slack=0.5):
    candidates = [s for s in sents if s["start"] >= t - slack]
    return candidates[0] if candidates else None


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
        return shot_start, end, _join_sentences([s for s in inside if s["start"] < end])

    keep = [s for score, s in scored if score == best]
    start = min(s["start"] for s in keep)
    end = max(s["end"] for s in keep)
    # Give a little context either side, still inside the original window.
    start = max(shot_start, start - 4)
    end = min(shot_end, end + 4)
    return start, end, _join_sentences(
        [s for s in inside if s["end"] > start and s["start"] < end])


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
            # Use the actual sentences either side of the silence rather than the
            # character-sliced before/after fields, which cut mid-word.
            before = _sentence_ending_by(sents, p["at"])
            after = _sentence_starting_after(sents, p["at"] + p["duration"])
            if not (before or after):
                continue
            picked.append({
                "kind": "spoken",
                "start": max(0.0, (before["start"] if before else p["at"]) - 0.5),
                "end": (after["end"] if after else p["at"] + p["duration"]) + 0.5,
                "text": (f'[{p["duration"]:.1f}s silence] '
                         f'{before["text"] if before else ""} '
                         f'⟨silence⟩ {after["text"] if after else ""}').strip(),
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
            window_sents = [s for s in sents if lo <= s["start"] < hi]
            text = _join_sentences(window_sents, max_chars=1200)
            evidence = [{"kind": "spoken", "start": lo, "end": min(hi, lo + 60),
                         "text": text}] if text else evidence

    evidence.sort(key=lambda e: e["start"])
    return evidence


def rehydrate_pauses(analysis, sents):
    """Restore the speech either side of each pause, if it was stripped.

    A deployed build ships pause timings without their surrounding text, so that
    a public image is not carrying long verbatim stretches of the talk. The text
    is regenerated here from the transcript once it has been fetched, which
    keeps the deployed and local behaviour identical.
    """
    if not sents:
        return
    for bucket in ("teachable", "needs_semantic_check"):
        for pause in analysis.get("pauses", {}).get(bucket, []) or []:
            if pause.get("before") is not None and pause.get("after") is not None:
                continue
            at = pause.get("at")
            if at is None:
                continue
            before = _sentence_ending_by(sents, at)
            after = _sentence_starting_after(sents, at + pause.get("duration", 0))
            pause["before"] = before["text"] if before else ""
            pause["after"] = after["text"] if after else ""


def _nearby_measurements(analysis, evidence):
    """Measured facts that overlap the evidence windows — the credibility layer."""
    spans = [(e["start"], e["end"]) for e in evidence] or [(0, analysis.get("duration", 0))]

    def overlaps(t):
        return any(a - 12 <= t <= b + 12 for a, b in spans)

    pauses = [
        f'{p["duration"]:.1f}s silence at {int(p["at"])}s, just before: "{_trim_words(p["after"], 70)}"'
        for p in analysis["pauses"]["teachable"]
        if p["band"] == "dramatic" and overlaps(p["at"])
    ][:6]

    devices = [
        f'{d["device"].replace("_", " ")} at {int(d["start"])}s: "{_trim_words(d["quote"], 90)}"'
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
%(memory)s
Below is evidence from the talk. Use ONLY this evidence — do not invent examples.

WHAT WAS SAID (from the transcript):
%(spoken)s

HOW IT WAS DELIVERED (from video analysis):
%(delivery)s

MEASURED FACTS (these are exact, not estimates):
%(measured)s

Write a short lesson with a clear spine, in connected prose.

- Open with a one-sentence rule the learner could write down and use tomorrow.
- Then trace that rule through the talk as a progression, simplest use first,
  sharpest last. Make the ordering do work: something must change between the
  first example and the last, and you should say what.
- Join the examples with real connective tissue — "the same instinct shows up
  when...", "the sharpest version comes later..." — so it reads as one argument
  rather than a list of separate observations.
- Close with the condition under which this technique fails or backfires, so the
  learner knows when NOT to copy it.

Never begin consecutive sentences or paragraphs with the same subject
("The speaker uses... The speaker also uses..."). That is what makes writing
read as a list.

Refer to the person as "the speaker" or "they". Never use "he" or "she": nothing
in this evidence tells you their gender, and guessing it misgenders a real person
— the same talk has been described as both in different answers.

CRITICAL — quoting: copy every quote EXACTLY, word for word, from the transcript
above. Do not paraphrase, tidy grammar, or merge two lines. A quote that does not
appear verbatim is discarded and the learner gets no video clip.

Choose quotes that read as a complete thought. The transcript is machine-made, so
some stretches break mid-sentence or mid-word — skip those and quote a clean line
nearby instead.

Format as 2-3 short paragraphs separated by a blank line; one dense block is hard
to read on screen. 110-170 words total. Be direct. No preamble, no flattery.

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


def answer(coll, video, analysis, sents, question, video_id=None, collection=None):
    """Answer a question about the talk with located, playable citations.

    `video_id` enables conversation memory: earlier turns shape this answer and
    already-shown moments are deprioritised. `collection` enables cross-talk
    search for comparative questions.
    """
    rehydrate_pauses(analysis, sents)
    evidence = gather_context(video, analysis, sents, question)

    # Prefer moments this learner has not been shown yet — repeating the same
    # pause in every answer teaches nothing new. Only reorders; never discards,
    # so a genuinely best-fitting moment can still win when nothing else exists.
    if video_id:
        seen = memory.seen_moments(video_id)
        if seen:
            evidence.sort(key=lambda e: memory.is_seen(seen, e["start"]))

    memory_block = ""
    if video_id:
        prior = memory.conversation_context(video_id)
        if prior:
            memory_block = (
                "\nEarlier in this conversation:\n" + prior
                + "\nIf this question follows on from those, connect to them rather than "
                  "repeating ground already covered.\n"
            )

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
        "memory": memory_block,
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

    # Structured output is unreliable call to call: sometimes the citations array
    # comes back unusable while the prose itself quotes the transcript correctly.
    # Recovering quotes from the answer text salvages real, locatable moments
    # instead of dropping to generic "Key moment" labels.
    if not citations:
        prose = str(result.get("answer", ""))
        for quoted in re.findall(r"[“\"]([^“”\"]{12,200})[”\"]", prose):
            location = locate_quote(sents, quoted)
            if not location:
                continue
            if any(abs(location["start"] - c["start"]) < 2 for c in citations):
                continue
            citations.append({
                "quote": location["text"],
                "technique": "Cited moment",
                "note": "Quoted in the answer above",
                "start": location["start"],
                "end": location["end"],
            })
            if len(citations) >= 4:
                break

    # Last resort: the search windows themselves, so an answer is never left
    # without playable evidence.
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


def search_across_talks(collection, manifest_talks, query, per_talk=2, max_talks=6):
    """Search every studied talk at once, via collection-level semantic search.

    Per-video search answers "how does THIS speaker do it". This answers "who
    does it best", which is a different and more interesting question once more
    than one talk has been studied — and it is the only thing `coll.search()`
    can do that per-video search cannot.

    Falls back to per-video search and merges, because a single collection-wide
    ranked list lets one talk's abundance of strong matches crowd the others out
    entirely.
    """
    hits = []

    try:
        results = collection.search(query=query, search_type=SearchType.semantic)
        for shot in results.get_shots():
            hits.append({
                "video_id": getattr(shot, "video_id", None),
                "start": float(getattr(shot, "start", 0)),
                "end": float(getattr(shot, "end", 0)),
                "text": (getattr(shot, "text", "") or "")[:300],
            })
    except InvalidRequestError:
        pass
    except Exception:  # noqa: BLE001 — fall through to the per-video path
        pass

    # Guarantee representation: search each talk directly and merge, so a talk
    # that simply matches less strongly still gets a voice.
    for video_id in list(manifest_talks)[:max_talks]:
        try:
            video = collection.get_video(video_id)
            results = video.search(query, search_type=SearchType.semantic)
            for shot in results.get_shots()[:per_talk]:
                hits.append({
                    "video_id": video_id,
                    "start": float(shot.start),
                    "end": float(shot.end),
                    "text": (getattr(shot, "text", "") or "")[:300],
                })
        except InvalidRequestError:
            continue
        except Exception:  # noqa: BLE001
            continue

    # De-duplicate: the collection search and the per-video search overlap.
    seen, merged = set(), []
    for hit in hits:
        key = (hit["video_id"], round(hit["start"] / 5))
        if key in seen or not hit["video_id"]:
            continue
        seen.add(key)
        merged.append(hit)

    merged.sort(key=lambda h: (h["video_id"], h["start"]))
    return merged


SUGGESTIONS = [
    "How does the opening grab attention?",
    "Show me how pauses are used for effect",
    "What does the body language do here?",
    "How is humour used without losing authority?",
    "How does the talk build to its main point?",
    "What makes the ending land?",
]
