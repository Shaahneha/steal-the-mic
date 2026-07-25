"""Compile an annotated technique reel from an analysed talk.

Usage:
    python make_reel.py                       # mixed pauses + devices
    python make_reel.py --device rule_of_three
    python make_reel.py --only pause --limit 6
"""

import sys

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

import argparse
import glob
import json
from pathlib import Path

from dotenv import load_dotenv

import videodb

from analysis import reel

PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(PROJECT_ROOT / ".env")
load_dotenv(PROJECT_ROOT.parent / ".env")

DATA_DIR = PROJECT_ROOT / "data"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--id", help="video id (defaults to the first analysed talk)")
    parser.add_argument("--device", help="only this device type")
    parser.add_argument("--only", choices=["pause", "device"], help="restrict moment kind")
    parser.add_argument("--limit", type=int, default=8)
    args = parser.parse_args()

    pattern = f"analysis__{args.id}.json" if args.id else "analysis__*.json"
    matches = sorted(glob.glob(str(DATA_DIR / pattern)))
    if not matches:
        print("No analysis found. Run analyze_talk.py first.")
        return

    analysis = json.loads(Path(matches[0]).read_text(encoding="utf-8"))
    kinds = (args.only,) if args.only else ("pause", "device")
    moments = reel.select_moments(analysis, kinds=kinds,
                                  device_filter=args.device, limit=args.limit)
    if not moments:
        print("No matching moments.")
        return

    print(f"{analysis['title']}\nCompiling {len(moments)} moments:")
    for m in moments:
        print(f"  [{m['start']:7.1f}s] {m['label']}")

    conn = videodb.connect()
    stream_url, info = reel.build_reel(conn, analysis["videodb_id"], moments)

    print(f"\nReel: {info['total_seconds']}s across {len(info['clips'])} clips")
    print(f"player: https://console.videodb.io/player?url={stream_url}")

    out = DATA_DIR / f"reel__{analysis['videodb_id']}.json"
    out.write_text(json.dumps({"stream_url": stream_url, **info}, indent=2,
                              ensure_ascii=False), encoding="utf-8")
    print(f"  -> {out.name}")


if __name__ == "__main__":
    main()
