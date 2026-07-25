"""Isolated Editor-overlay positioning test — run this before touching the app.

Compiles ~16 seconds with two labelled clips so overlay placement can be checked
in seconds rather than by driving the whole UI for every attempt.

Known footguns this script exists to verify:
  * Clip defaults to Fit.crop, which stretches an asset to fill the canvas and
    silently ignores `position`. Overlays must use Fit.none.
  * TextAsset needs explicit width/height or its anchor miscomputes and a
    corner-anchored label renders centred or in the wrong corner.

Usage:  python tools/test_overlay_positioning.py
"""

import sys

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

import json
from pathlib import Path

from dotenv import load_dotenv

import videodb
from videodb.editor import (
    Timeline, Track, Clip, VideoAsset, TextAsset,
    Fit, Position, Font, Background, Alignment,
    HorizontalAlignment, VerticalAlignment,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")
load_dotenv(PROJECT_ROOT.parent / ".env")

RESOLUTION = (1280, 720)
LABEL_W, LABEL_H = 900, 90

# Two real moments from the analysed talk.
SAMPLES = [
    {"start": 581.5, "duration": 8, "label": "DRAMATIC PAUSE  ·  3.8s"},
    {"start": 280.8, "duration": 8, "label": "RHETORICAL QUESTION"},
]


def build_label(text):
    """A bottom-anchored caption chip that stays at its natural size."""
    return TextAsset(
        text=text,
        font=Font(family="Clear Sans", size=42, color="#FFFFFF"),
        background=Background(color="#111111", opacity=0.78,
                              width=LABEL_W, height=LABEL_H),
        alignment=Alignment(horizontal=HorizontalAlignment.center,
                            vertical=VerticalAlignment.center),
        # Explicit dimensions: without these the anchor miscomputes.
        width=LABEL_W,
        height=LABEL_H,
    )


def main():
    manifest = json.loads((PROJECT_ROOT / "data" / "talks.json").read_text(encoding="utf-8"))
    video_id = next(iter(manifest["talks"]))

    conn = videodb.connect()
    timeline = Timeline(conn)
    timeline.resolution = f"{RESOLUTION[0]}x{RESOLUTION[1]}"

    video_track = Track()
    label_track = Track()

    cursor = 0.0
    for sample in SAMPLES:
        video_track.add_clip(cursor, Clip(
            asset=VideoAsset(id=video_id, start=sample["start"]),
            duration=sample["duration"],
        ))
        label_track.add_clip(cursor, Clip(
            asset=build_label(sample["label"]),
            duration=sample["duration"],
            fit=Fit.none,               # never Fit.crop for an overlay
            position=Position.bottom,
            z_index=10,
        ))
        cursor += sample["duration"]

    timeline.add_track(video_track)
    timeline.add_track(label_track)   # later track renders on top

    print("Rendering...")
    stream_url = timeline.generate_stream()
    print(f"\nstream: {stream_url}")
    print(f"player: https://console.videodb.io/player?url={stream_url}")


if __name__ == "__main__":
    main()
