"""Deterministic delivery metrics — pace, pauses, fillers.

Everything here is arithmetic over the word-level transcript. No LLM is involved,
so these numbers are exactly reproducible and safe to show a learner as fact.
Anything requiring judgement (is this pause a dramatic beat or is the audience
laughing?) is deliberately left unclassified here and resolved in the semantic
pass, so we never teach a guess as if it were measured.
"""

import re

from . import transcript as T

# Below this, a gap is just the space between words, not a pause.
MIN_PAUSE = 0.6
BEAT_MAX = 1.5
DRAMATIC_MAX = 3.5

# Unambiguous disfluencies — these are always filler.
DISFLUENCIES = {"um", "uh", "erm", "er", "ah", "mm", "hmm", "uhh", "umm"}

# Habitual crutch phrases. Legitimate in moderation, which is why they are
# reported separately from disfluencies rather than lumped into one "filler" count.
DISCOURSE_MARKERS = {
    "you know", "i mean", "kind of", "sort of", "or something",
    "basically", "actually", "literally", "obviously",
}

# Words with real grammatical uses that are only *sometimes* filler. Counted but
# always labelled ambiguous — never presented as a defect on its own.
AMBIGUOUS_FILLERS = {"like", "right", "so", "well", "just"}


def pace_curve(words, duration, bucket_seconds=60):
    """Words-per-minute over time, normalised so a partial final bucket is fair."""
    buckets = []
    n = int(duration // bucket_seconds) + (1 if duration % bucket_seconds else 0)

    for i in range(n):
        start = i * bucket_seconds
        end = min(start + bucket_seconds, duration)
        span = end - start
        if span <= 0:
            continue
        count = sum(1 for w in words if start <= w["start"] < end)
        buckets.append({
            "start": round(start, 1),
            "end": round(end, 1),
            # Normalise by the real span — a 44s final bucket must not look slow
            # merely because it is short.
            "wpm": round(count / (span / 60)),
            "word_count": count,
            "partial": span < bucket_seconds,
        })

    full = [b for b in buckets if not b["partial"]]
    ref = full or buckets
    slowest = min(ref, key=lambda b: b["wpm"])
    fastest = max(ref, key=lambda b: b["wpm"])

    return {
        "bucket_seconds": bucket_seconds,
        "buckets": buckets,
        "min_wpm": slowest["wpm"],
        "max_wpm": fastest["wpm"],
        "spread": fastest["wpm"] - slowest["wpm"],
        "slowest": {**slowest, "text": T.text_between(words, slowest["start"], slowest["end"])[:400]},
        "fastest": {**fastest, "text": T.text_between(words, fastest["start"], fastest["end"])[:400]},
    }


def detect_pauses(words, duration):
    """Every silence between spoken words, with the line on each side."""
    pauses = []
    for prev, cur in zip(words, words[1:]):
        gap = round(cur["start"] - prev["end"], 2)
        if gap < MIN_PAUSE:
            continue
        pauses.append({
            "at": round(prev["end"], 2),
            "duration": gap,
            "before": T.text_between(words, max(0, prev["end"] - 6), prev["end"])[-120:],
            "after": T.text_between(words, cur["start"], cur["start"] + 6)[:120],
            "band": _band(gap),
        })
    return pauses


def _band(gap):
    if gap <= BEAT_MAX:
        return "beat"
    if gap <= DRAMATIC_MAX:
        return "dramatic"
    return "extended"


def classify_pauses(pauses, words, duration):
    """Split pauses into what we can teach and what needs a semantic check.

    The ASR marks all non-speech identically, so a long gap is ambiguous: it may
    be a deliberate beat before a key line, or the audience laughing. Duration
    cannot separate them — in the reference talk a 3.8s gap precedes the thesis
    statement while a 5.0s gap is laughter after a joke. So extended gaps are
    flagged for the semantic pass instead of being taught as technique.
    """
    last_word_end = words[-1]["end"] if words else duration

    teachable, needs_check = [], []
    for p in pauses:
        # Deterministic call: the final silence of the talk is the closing ovation.
        if p["at"] >= last_word_end - 1.0 or p["at"] + p["duration"] >= duration - 1.0:
            p["classification"] = "closing_applause"
            p["teachable"] = False
            needs_check.append(p)
            continue

        # Deterministic call: thanking the room straight after a gap means the
        # room was making noise.
        if re.match(r"^\s*thank(s| you)?\b", p["after"], re.I):
            p["classification"] = "audience_response"
            p["teachable"] = False
            needs_check.append(p)
            continue

        if p["band"] == "extended":
            p["classification"] = "unclassified_extended"
            p["teachable"] = False
            needs_check.append(p)
        else:
            p["classification"] = "speaker_pause"
            p["teachable"] = True
            teachable.append(p)

    minutes = duration / 60
    dramatic = [p for p in teachable if p["band"] == "dramatic"]

    return {
        "total_detected": len(pauses),
        "teachable": teachable,
        "needs_semantic_check": needs_check,
        "counts": {
            "beat": sum(1 for p in teachable if p["band"] == "beat"),
            "dramatic": len(dramatic),
            "pending_check": len(needs_check),
        },
        "rate_per_min": round(len(teachable) / minutes, 1),
        "dramatic_per_min": round(len(dramatic) / minutes, 2),
        "longest_teachable": max(dramatic, key=lambda p: p["duration"]) if dramatic else None,
        "note": (
            "Pauses over %.1fs are not counted as technique until the semantic pass "
            "confirms they are the speaker's choice rather than audience reaction."
            % DRAMATIC_MAX
        ),
    }


def filler_analysis(words, duration):
    """Count crutch words, separated by how defensible each category is."""
    tokens = [re.sub(r"[^\w']", "", w["text"]).lower() for w in words]
    joined = " ".join(tokens)
    minutes = duration / 60

    disfluency_hits = {}
    for t in tokens:
        if t in DISFLUENCIES:
            disfluency_hits[t] = disfluency_hits.get(t, 0) + 1

    marker_hits = {}
    for phrase in DISCOURSE_MARKERS:
        n = len(re.findall(r"\b" + re.escape(phrase) + r"\b", joined))
        if n:
            marker_hits[phrase] = n

    ambiguous_hits = {}
    for t in tokens:
        if t in AMBIGUOUS_FILLERS:
            ambiguous_hits[t] = ambiguous_hits.get(t, 0) + 1

    disfluency_total = sum(disfluency_hits.values())
    marker_total = sum(marker_hits.values())

    # VideoDB's ASR normalises disfluencies out of the transcript: a 20-minute
    # unscripted talk returned exactly zero um/uh while preserving "you know"
    # (x21) and "like" (x23). A zero here therefore means "not measurable from
    # this transcript", NOT "the speaker never hesitated" — reporting it as a
    # score would flatter the reference speaker and unfairly shame a learner
    # whose own upload is cleaned the same way. Never render this as a metric.
    return {
        "disfluencies": {
            "measurable": disfluency_total > 0,
            "total": disfluency_total,
            "per_min": round(disfluency_total / minutes, 2),
            "by_word": dict(sorted(disfluency_hits.items(), key=lambda kv: -kv[1])),
            "note": None if disfluency_total else (
                "Not measurable: the speech-to-text layer removes um/uh before we "
                "ever see the transcript. Do not display or compare this."
            ),
        },
        "discourse_markers": {
            "total": marker_total,
            "per_min": round(marker_total / minutes, 2),
            "by_phrase": dict(sorted(marker_hits.items(), key=lambda kv: -kv[1])),
        },
        "ambiguous": {
            "by_word": dict(sorted(ambiguous_hits.items(), key=lambda kv: -kv[1])),
            "note": "These have legitimate grammatical uses; high counts are a "
                    "prompt to review in context, not a defect on their own.",
        },
    }


def compute(raw_transcript, duration, bucket_seconds=60):
    """Full deterministic metric set for one talk."""
    words = T.word_segments(raw_transcript)
    sents = T.sentences(words)
    speaking_time = sum(w["end"] - w["start"] for w in words)

    pauses = detect_pauses(words, duration)

    return {
        "duration": round(duration, 1),
        "word_count": len(words),
        "sentence_count": len(sents),
        "overall_wpm": round(len(words) / (duration / 60)),
        "speaking_wpm": round(len(words) / (speaking_time / 60)) if speaking_time else None,
        "silence_ratio": round(1 - (speaking_time / duration), 3),
        "pace": pace_curve(words, duration, bucket_seconds),
        "pauses": classify_pauses(pauses, words, duration),
        "fillers": filler_analysis(words, duration),
    }
