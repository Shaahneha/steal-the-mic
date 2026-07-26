"""Three independent signal types, from one video.

The point this makes about the platform: a single upload yields three kinds of
evidence that answer different questions, and none of them can substitute for
another.

  TEMPORAL    when things happen — pace, pauses, rhythm. Derived from
              word-level transcript timestamps, so these are measurements:
              exact, reproducible, no model judgement involved.

  BEHAVIOURAL what the speaker does — gesture, gaze, expression, energy.
              Derived from the scene index, which describes each sampled frame.

  SPATIAL     how the speaker occupies space — stance, orientation, lean,
              whether they are anchored to a lectern or working the stage.
              Also from the scene index, but answering a different question.

Behavioural and spatial signals are classified from the scene descriptions by
pattern, not by a second model pass. That keeps them deterministic and instant,
and it means the number on screen can be traced to the frames that produced it.
Every rate is reported with the frame count behind it, because a percentage over
six observations is not the same claim as one over three hundred.
"""

import re

# Each signal: (name, positive patterns, negative patterns). A frame counts
# toward the rate when a positive pattern matches and no negative one does.
BEHAVIOURAL_SIGNALS = [
    ("gesturing", [r"\bgestur", r"\bhands? (?:are |is )?(?:mid-?|raised|extended|open|out)",
                   r"\bpalms?\b", r"\bpointing\b", r"\bmid-?swing"],
     [r"hands? (?:rest|clasped|together|down|still|at (?:his|her|their) sides?)"]),
    ("open hands", [r"\bopen (?:palm|hand)", r"\bpalms? (?:up|out|open)", r"\bopen gesture"], []),
    ("audience gaze", [r"gaz\w* (?:toward|to|at|out|across)", r"looking (?:toward|at|out|across)",
                       r"eye contact", r"facing the audience"],
     [r"looking down", r"gaz\w* down", r"reading", r"referencing notes", r"head (?:is )?angled down"]),
    ("animated face", [r"\bsmil", r"\bgrin", r"animated expression", r"expressive"],
     [r"neutral expression", r"serious", r"impassive"]),
    ("visible energy", [r"\benergetic", r"\banimated\b", r"\bemphatic", r"\bforceful",
                        r"\blively", r"high energy"], []),
]

SPATIAL_SIGNALS = [
    ("grounded stance", [r"\bplanted", r"\bbalanced stance", r"\bgrounded", r"\bsteady stance",
                         r"feet (?:are )?(?:planted|apart|balanced)", r"stable stance"],
     [r"shifting", r"weight (?:shift|moving)"]),
    ("upright posture", [r"stands? upright", r"\bupright\b", r"\berect\b", r"straight (?:back|posture)"],
     [r"slouch", r"hunch", r"stooped"]),
    ("forward lean", [r"forward lean", r"leans? (?:in|forward)", r"leaning (?:in|forward)",
                      r"slight forward"], []),
    ("squared to audience", [r"squared? (?:to|toward)", r"facing (?:the )?(?:audience|forward|front)",
                             r"torso (?:is )?(?:facing|square)", r"oriented toward"],
     [r"angled away", r"turned away", r"facing (?:slightly )?to one side"]),
    ("anchored to lectern", [r"\blectern\b", r"\bpodium\b", r"behind the (?:lectern|podium|stand)"], []),
    ("moving the stage", [r"\bsteps?\b", r"\bwalk", r"\bmoves? (?:across|toward|around)",
                          r"crossing the stage", r"paces?\b"], []),
]


# Below this, a rate says more about the vocabulary the description happened to
# use than about the speaker. "Audience gaze 1.3%" does not mean the speaker
# rarely looked at the room; it means the frames rarely described gaze at all.
# Absence of description is not description of absence.
WEAK_EVIDENCE_PCT = 15.0


def _rate(descriptions, positives, negatives):
    hits = 0
    for text in descriptions:
        low = text.lower()
        if any(re.search(p, low) for p in negatives):
            continue
        if any(re.search(p, low) for p in positives):
            hits += 1
    n = len(descriptions)
    pct = round(100.0 * hits / n, 1) if n else 0.0
    return {
        "hits": hits,
        "frames": n,
        "pct": pct,
        # Every one of these is "described in N% of frames", never "happened in
        # N% of the talk" — the UI must not round that distinction away.
        "weak": pct < WEAK_EVIDENCE_PCT,
    }


def _classify(descriptions, signals):
    return {name: _rate(descriptions, pos, neg) for name, pos, neg in signals}


def temporal(analysis):
    """When things happen. Measured from word-level timestamps, not inferred."""
    pauses = analysis.get("pauses", {})
    teachable = pauses.get("teachable", []) or []
    dramatic = [p for p in teachable if p.get("band") == "dramatic"]
    pace = analysis.get("pace", {}) or {}
    duration = analysis.get("duration") or 0
    minutes = duration / 60 if duration else 1

    longest = max((p["duration"] for p in dramatic), default=0.0)
    return {
        "measured": True,
        "silence_pct": round((analysis.get("silence_ratio") or 0) * 100, 1),
        "speaking_wpm": analysis.get("speaking_wpm"),
        "overall_wpm": analysis.get("overall_wpm"),
        "pace_range": [pace.get("min_wpm"), pace.get("max_wpm")],
        "pace_swing": (pace.get("max_wpm") or 0) - (pace.get("min_wpm") or 0),
        "held_pauses": len(dramatic),
        "held_pauses_per_min": round(len(dramatic) / minutes, 2),
        "longest_pause": round(longest, 1),
    }


def behavioural(analysis):
    """What the speaker does. From the scene index."""
    frames = [s.get("description", "") for s in
              (analysis.get("delivery", {}).get("timeline") or []) if s.get("level")]
    energy = [s.get("level") for s in
              (analysis.get("delivery", {}).get("timeline") or []) if s.get("level")]
    counts = {lvl: energy.count(lvl) for lvl in ("low", "moderate", "high") if energy.count(lvl)}
    return {
        "measured": False,
        "frames": len(frames),
        "signals": _classify(frames, BEHAVIOURAL_SIGNALS),
        "energy_mix": counts,
    }


def spatial(analysis):
    """How the speaker occupies space. From the scene index."""
    frames = [s.get("description", "") for s in
              (analysis.get("delivery", {}).get("timeline") or []) if s.get("level")]
    signals = _classify(frames, SPATIAL_SIGNALS)

    anchored = signals["anchored to lectern"]["pct"]
    moving = signals["moving the stage"]["pct"]
    if anchored >= 40 and moving < 10:
        style = "anchored"
    elif moving >= 15:
        style = "roaming"
    else:
        style = "planted, open stage"

    return {
        "measured": False,
        "frames": len(frames),
        "signals": signals,
        "stage_use": style,
    }


def compute(analysis):
    """All three dimensions, plus what the scene index could not see."""
    delivery = analysis.get("delivery", {}) or {}
    total = delivery.get("scenes_total") or 0
    seen = delivery.get("scenes_with_speaker") or 0
    return {
        "temporal": temporal(analysis),
        "behavioural": behavioural(analysis),
        "spatial": spatial(analysis),
        "coverage": {
            "frames_sampled": total,
            "frames_with_speaker": seen,
            "pct": round(100.0 * seen / total, 1) if total else 0.0,
            "note": "Frames showing a slide or the audience are excluded rather "
                    "than guessed at, so behavioural and spatial rates are over "
                    "frames where the speaker was actually visible.",
        },
    }
