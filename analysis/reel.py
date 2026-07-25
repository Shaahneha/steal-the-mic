"""Annotated technique reel — the compiled, labelled highlight clip.

Stitches the moments our analysis identified into one playable video with the
technique named on screen as it happens, so a learner sees the craft rather than
having to take our word for it.

Positioning rules verified in tools/test_overlay_positioning.py:
  * overlay clips must use Fit.none (the Fit.crop default stretches to fill the
    canvas and ignores `position` entirely)
  * TextAsset needs explicit width/height or its anchor miscomputes
"""

from videodb.editor import (
    Timeline, Track, Clip, VideoAsset, TextAsset,
    Fit, Position, Offset, Font, Background, Alignment,
    HorizontalAlignment, VerticalAlignment,
)

RESOLUTION = "1280x720"

# Two stacked chips: the technique name, and what to actually watch for. A bare
# name ("ANAPHORA") tells a learner nothing if they don't already know the term.
TITLE_W, TITLE_H = 760, 64
NOTE_W, NOTE_H = 980, 50

# Offset is RELATIVE, not pixels: y=-0.05 means 5% of frame height upward.
# A pixel value fails the render outright ("Input should be >= -1").
# Raised clear of a video player's control bar, which sits over the bottom ~8%
# and otherwise obscures the note line exactly when someone is watching.
TITLE_OFFSET = Offset(0, -0.185)
NOTE_OFFSET = Offset(0, -0.095)

MAX_NOTE_CHARS = 68

DEVICE_LABELS = {
    "rule_of_three": "RULE OF THREE",
    "anaphora": "ANAPHORA · REPETITION",
    "rhetorical_question": "RHETORICAL QUESTION",
    "contrast": "CONTRAST",
    "callback": "CALLBACK",
    "self_disclosure": "SELF-DISCLOSURE",
    "direct_address": "DIRECT ADDRESS",
    "concrete_statistic": "CONCRETE STATISTIC",
    "punchline": "PUNCHLINE",
}

# Padding is deliberately tight. A clip that runs long buries the thing it is
# meant to demonstrate — the learner watches 14 seconds and cannot tell which
# part was the lesson. Just enough setup to make the moment make sense.
#
# For a pause the shape must be: the words that set it up -> the silence ->
# the line it was holding for. That is the whole lesson, and nothing else.
PAUSE_LEAD_IN = 2.8
PAUSE_TAIL = 2.2
DEVICE_LEAD_IN = 0.8
DEVICE_TAIL = 1.0

MAX_CLIP_SECONDS = 10
MIN_CLIP_SECONDS = 2.5


def select_moments(analysis, kinds=("pause", "device"), device_filter=None, limit=8):
    """Pick the most instructive moments, balanced across kinds and techniques."""
    pauses = []
    if "pause" in kinds:
        pauses = sorted(
            (
                {
                    "kind": "pause",
                    "label": f"DRAMATIC PAUSE  ·  {p['duration']:.1f}s",
                    "start": max(0, p["at"] - PAUSE_LEAD_IN),
                    "end": p["at"] + p["duration"] + PAUSE_TAIL,
                    "detail": p["after"][:90],
                }
                for p in analysis["pauses"]["teachable"] if p["band"] == "dramatic"
            ),
            key=lambda m: -(m["end"] - m["start"]),
        )

    devices = []
    if "device" in kinds:
        candidates = [
            d for d in analysis.get("devices", [])
            if not device_filter or d["device"] == device_filter
        ]
        devices = [
            {
                "kind": "device",
                "label": DEVICE_LABELS.get(d["device"], d["device"].replace("_", " ").upper()),
                "start": max(0, d["start"] - DEVICE_LEAD_IN),
                "end": d["end"] + DEVICE_TAIL,
                "detail": d["why_it_works"],
            }
            for d in _round_robin_by_type(candidates)
        ]

    # Allocate slots evenly rather than ranking the two kinds against each other.
    # Pause "weight" is a duration in seconds and device weight was a constant, so
    # a single sorted list handed every slot to pauses and no technique was ever
    # shown.
    chosen = _interleave(pauses, devices, limit)
    chosen.sort(key=lambda m: m["start"])
    return chosen


def _round_robin_by_type(devices):
    """Order devices so distinct techniques appear before repeats of one type."""
    buckets = {}
    for d in devices:
        buckets.setdefault(d["device"], []).append(d)
    ordered, exhausted = [], False
    while not exhausted:
        exhausted = True
        for bucket in buckets.values():
            if bucket:
                ordered.append(bucket.pop(0))
                exhausted = False
    return ordered


def _interleave(a, b, limit):
    """Take alternately from both lists, skipping moments too close together."""
    chosen = []
    ia = ib = 0
    while len(chosen) < limit and (ia < len(a) or ib < len(b)):
        for source, idx in ((a, "a"), (b, "b")):
            if len(chosen) >= limit:
                break
            i = ia if idx == "a" else ib
            if i >= len(source):
                continue
            candidate = source[i]
            if idx == "a":
                ia += 1
            else:
                ib += 1
            if any(abs(candidate["start"] - c["start"]) < 6 for c in chosen):
                continue
            chosen.append(candidate)
    return chosen


def _chip(text, *, size, width, height, bg, text_color, opacity):
    """A centred caption chip. Explicit width/height keeps the anchor correct."""
    return TextAsset(
        text=text,
        font=Font(family="Clear Sans", size=size, color=text_color),
        background=Background(color=bg, opacity=opacity, width=width, height=height),
        alignment=Alignment(horizontal=HorizontalAlignment.center,
                            vertical=VerticalAlignment.center),
        width=width,
        height=height,
    )


def _title_chip(text):
    return _chip(text, size=38, width=TITLE_W, height=TITLE_H,
                 bg="#2A78D6", text_color="#FFFFFF", opacity=0.94)


def _note_chip(text):
    return _chip(text, size=27, width=NOTE_W, height=NOTE_H,
                 bg="#101010", text_color="#F2F2EF", opacity=0.84)


# The caption renderer drops characters outside its font's range — a curly
# apostrophe in "audience's" rendered as "audiences", which reads as a typo
# burned into the video. Normalise smart punctuation to ASCII before drawing.
_PUNCT_MAP = {
    "’": "'", "‘": "'", "“": '"', "”": '"',
    "—": "-", "–": "-", "…": "...", " ": " ",
}


def _ascii_punct(text):
    for fancy, plain in _PUNCT_MAP.items():
        text = text.replace(fancy, plain)
    return text


def _shorten(text, limit=MAX_NOTE_CHARS):
    """Keep the note to one line — a wrapped chip renders badly."""
    text = _ascii_punct(" ".join(str(text or "").split()))
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return f"{cut}..."


def speaker_visible_spans(analysis):
    """Windows where the speaker is actually on screen.

    The scene index marks slide/audience frames with NO_SPEAKER_VISIBLE, which
    the analysis step already filtered out, so anything left in the delivery
    timeline is a window where the speaker was visible.
    """
    return [
        (s["start"], s["end"])
        for s in (analysis.get("delivery", {}).get("timeline") or [])
        if s.get("level")
    ]


def _visible_fraction(start, end, spans):
    """How much of a window is backed by a speaker-visible sample.

    Each span is a window the scene index sampled ONE frame from, so this is
    evidence rather than proof — a cut to a slide between two samples is
    invisible to us. Requiring near-total coverage keeps that risk low.
    """
    if end <= start or not spans:
        return 1.0  # no data to judge by — don't penalise the clip
    covered = sum(
        max(0.0, min(end, b) - max(start, a))
        for a, b in spans
    )
    return covered / (end - start)


def snap_to_speaker(start, end, spans, search=9.0, min_fraction=0.95):
    """Nudge a clip so it shows the speaker rather than a slide.

    A pause demonstrated over a title card teaches nothing — the learner cannot
    see the pause being performed. Shifts the window by a few seconds either way
    to find speaker-visible footage, keeping the same duration, and gives up
    (returning the original) rather than drifting far from the cited moment.
    """
    if _visible_fraction(start, end, spans) >= min_fraction:
        return start, end, True

    duration = end - start
    best, best_score = None, _visible_fraction(start, end, spans)
    step = 1.5
    offset = -search
    while offset <= search:
        candidate = max(0.0, start + offset)
        score = _visible_fraction(candidate, candidate + duration, spans)
        if score > best_score:
            best, best_score = candidate, score
        offset += step

    if best is not None and best_score >= min_fraction:
        return best, best + duration, True
    return start, end, best_score >= min_fraction


def build_reel(conn, video_id, moments, resolution=RESOLUTION):
    """Compile moments into one labelled stream. Returns (stream_url, manifest)."""
    if not moments:
        raise ValueError("no moments to compile")

    timeline = Timeline(conn)
    timeline.resolution = resolution

    video_track = Track()
    title_track = Track()
    note_track = Track()

    cursor = 0.0
    compiled = []
    for m in moments:
        start = max(0.0, float(m["start"]))
        duration = min(MAX_CLIP_SECONDS,
                       max(MIN_CLIP_SECONDS, float(m["end"]) - start))

        video_track.add_clip(cursor, Clip(
            asset=VideoAsset(id=video_id, start=start),
            duration=duration,
        ))
        title_track.add_clip(cursor, Clip(
            asset=_title_chip(m["label"]),
            duration=duration,
            fit=Fit.none,               # never Fit.crop for an overlay
            position=Position.bottom,
            offset=TITLE_OFFSET,
            z_index=10,
        ))

        note = _shorten(m.get("detail", ""))
        if note:
            note_track.add_clip(cursor, Clip(
                asset=_note_chip(note),
                duration=duration,
                fit=Fit.none,
                position=Position.bottom,
                offset=NOTE_OFFSET,
                z_index=11,
            ))

        compiled.append({
            "label": m["label"],
            "kind": m["kind"],
            "source_start": round(start, 2),
            "reel_start": round(cursor, 2),
            "duration": round(duration, 2),
            "detail": note,
        })
        cursor += duration

    timeline.add_track(video_track)
    timeline.add_track(title_track)   # later tracks render on top
    timeline.add_track(note_track)

    stream_url = timeline.generate_stream()
    return stream_url, {"total_seconds": round(cursor, 1), "clips": compiled}
