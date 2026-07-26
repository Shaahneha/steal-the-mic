# Steal the Mic

**Take what great speakers do. Use it yourself.**

Paste a talk you admire, ask how they hold a room, and watch short captioned clips cut from the video that prove it.

Great speakers make it look effortless, which is exactly the problem: you watch a brilliant talk,
feel the effect, and can't say *why* it worked. Give this the YouTube link of a speaker you admire
and it studies how they hold a room — then you can ask how any of it works and get back a
coaching answer with **short, captioned clips cut from the talk itself**.

Built for the **Global Media Intelligence Hackathon** (hackday.videodb.io) —
*"Unlock the footage. Build media intelligence with VideoDB."*

**One-line pitch:** Paste any talk's URL and learn public speaking from it — ask how they do it, watch captioned proof.

## Try it

| | |
|---|---|
| **Live app** | _deploying — link here_ |
| **Demo video** | _recording — link here_ |
| **Source** | https://github.com/Shaahneha/steal-the-mic |

Two talks are pre-analysed, so you can ask a question and watch a clip without waiting for
ingestion. Pasting your own YouTube link runs the full pipeline live.

---

## The loop: perceive → remember → act

| | |
|---|---|
| **Perceive** | Two indexes per talk, not one. `index_spoken_words` gives a word-level transcript; `index_scenes` runs every 6 seconds with a delivery-specific prompt that returns posture, gesture, gaze and energy. Semantic search runs over **both**, and `coll.search` runs across every studied talk. |
| **Remember** | Conversations persist per talk on disk. A follow-up like *"and the ending?"* resolves against earlier turns, moments already shown are deprioritised so answers stop repeating themselves, and reopening a talk restores the thread. |
| **Act** | Answers are compiled, not just written: cited moments become a short clip through the Editor timeline with the technique captioned over the footage — and clips are snapped away from slides so the speaker is actually on screen. |

## VideoDB primitives used

| Pillar | Primitive | Where |
|--------|-----------|-------|
| **Ingest** | `coll.upload(url=...)` — ingest any talk from a YouTube URL | `ingest.py` |
| **Ingest** | `conn.create_collection()` — a dedicated collection, so the app never touches unrelated media in the account | `ingest.py::get_project_collection` |
| **Index** | `video.index_spoken_words()` — word-level transcript | `ingest.py` |
| **Index** | `video.index_scenes(extraction_type=time_based)` — delivery-focused visual index every 6s | `ingest.py` |
| **Search** | `video.search(search_type=semantic)` — spoken-word semantic search for chat evidence | `analysis/chat.py` |
| **Search** | `video.search(index_type=IndexType.scene, scene_index_id=…)` — visual search for delivery questions | `analysis/chat.py` |
| **Search** | `coll.search(search_type=semantic)` — across every studied talk at once, merged with per-video results so one talk's abundance of matches can't crowd the others out | `analysis/chat.py::search_across_talks` |
| **Act** | `coll.generate_text(response_type="json")` — chat answers, rhetorical devices, structure, pause intent | `analysis/chat.py`, `analysis/semantic.py` |
| **Act** | `Timeline` / `Track` / `VideoAsset` / `TextAsset` / `generate_stream()` — captioned demonstration clips | `analysis/reel.py` |
| **Act** | `video.extract_scenes()` + frame URLs — used to visually verify rendered captions | `tools/test_overlay_positioning.py` |

Every LLM call runs through VideoDB's own `generate_text()`. No external LLM API is used anywhere
in the runtime path.

---

## How it works

1. **Paste a YouTube URL.** Everything after this runs in the background with real progress stages.
2. **The talk is ingested and indexed twice** — every spoken word, and the visual delivery
   (posture, gesture, gaze, energy) sampled every 6 seconds.
3. **A deterministic pass measures** pace, pauses and silence ratio straight from word-level
   timestamps. A semantic pass then names rhetorical devices and maps the talk's structure.
4. **You ask questions.** Answers are grounded in all three sources — what was said, how it was
   delivered, and what was measured — so the tool can say *"a 3.4-second silence is held before
   this line"* as a fact rather than a guess.
5. **The clip builds itself.** Every answer's cited moments compile automatically into one short
   clip with the technique captioned on screen — no extra click.

## The demonstration clips

The centrepiece. Rather than telling you "the speaker uses dramatic pauses", it cuts the actual
moments together and captions them:

```
┌──────────────────────────────┐
│      [ speaker, mid-pause ]  │
│                              │
│        RESET PAUSE           │   ← technique
│  Give the audience a beat    │   ← what to watch for
└──────────────────────────────┘
```

Three rules make these clips teach rather than confuse:

- **Short.** Capped at 10 seconds each, ~15s total for a 3-moment answer. A long run-up buries the
  thing you're supposed to notice.
- **The speaker is on screen.** Clips are snapped away from slides and audience cuts using the
  visual index — a pause demonstrated over a title card teaches nothing.
- **Pause clips carry setup → silence → payoff.** A silence with no context is just a gap.

---

## Two design rules worth stealing

**The model never emits a timestamp.** It names a technique and quotes the line verbatim; we locate
that quote in the word-level transcript to derive exact times. A hallucinated timestamp would
silently point a clip at the wrong moment — a hallucinated *quote* simply fails to match and gets
dropped.

Measured, not asserted. `tools/evaluate.py` runs a fixed question set across every studied talk and
checks each returned citation against the transcript at the timestamp it claims:

| | Latin-script (4 talks) | Devanagari (1 talk) |
|---|---|---|
| Citations returned | 87 | 10 |
| **Verified in transcript** | **87 (100%)** | 7 (70%) |
| Answers with no citation | 0 | 0 |
| Answers needing a fallback path | 0 | 2 |

Reproduce with `python tools/evaluate.py`.

**Technique questions don't go to text search.** A speaker demonstrating a pause never says the word
"pause", and nobody announces "here comes my rule of three". Searching for those terms finds people
*talking about* the concept. So when a question names something already located — a pause, a device,
the opening, the ending — the evidence is seeded from the analysis and search only supplements it.

---

## Honest limitations

- **Filler words ("um", "uh") are not reported.** VideoDB's speech-to-text normalises them away
  before we see the transcript — a 20-minute unscripted talk returned exactly zero, while
  "you know" (×21) survived. Reporting "0 filler words" would be a transcription artifact, not a
  compliment, so the metric is computed, flagged `measurable: false`, and never displayed.
- **Visual coverage is sampled, not continuous.** One frame every 6 seconds. That is enough to keep
  clips off slides in practice, but a cut shorter than the sampling interval can still slip through.
- **Some things are measured, some inferred.** Pace, pauses and silence ratio are arithmetic and
  exact. Which device a line uses, and whether a long silence was technique or applause, are model
  judgements. The UI keeps that distinction.
- **The tool cannot see how a speaker prepared.** No rehearsal footage exists. It reverse-engineers
  the craft visible in the finished talk.
- **Non-English degrades measurably, on two separate axes.** Tested on a 45-minute Hindi talk and
  measured, not guessed. Ingestion, transcription, pause detection and the visual delivery index all
  work — body-language analysis genuinely doesn't care what language you speak (418 of 459 frames
  scored). What degrades is **volume** (8 devices per talk against 32, and zero structure beats
  against 10) and **accuracy** (70% of citations verified against 100%). Sentence segmentation was
  one cause and is fixed: Devanagari ends sentences with a danda (`।`), which the splitter did not
  know, so a 45-minute talk collapsed into 12 sentences. The remainder is a real limit — the visual
  half is language-agnostic, the semantic half is effectively English-only. The pace figure for that
  talk (400 wpm) is also inflated by word-timing artifacts and should not be trusted.

---

## Setup

Four commands from a clean clone. A free VideoDB key (no card) is the only
prerequisite: https://console.videodb.io

```bash
pip install -r requirements.txt
cp .env.example .env            # paste your VIDEO_DB_API_KEY into it

python ingest.py                # bootstraps 5 starter talks — 25-35 min, safe to re-run
python analyze_talk.py          # measurements + technique extraction — ~10 min

uvicorn backend.main:app        # http://127.0.0.1:8000
```

**Why the wait:** ingestion transcribes every word and builds a visual delivery
index every 6 seconds. That's the work the whole product rests on, and it happens
once per talk. `ingest.py` skips talks it has already done, so an interrupted run
resumes rather than restarting.

**In a hurry?** Ingest one short talk instead — the 8-minute Toastmasters final is
the fastest way to a working app:

```bash
python ingest.py --url "https://www.youtube.com/watch?v=GTc7nbTFxa4"
python analyze_talk.py
uvicorn backend.main:app
```

Then paste any YouTube talk into the app itself; it runs the same pipeline in the
background with live progress.

Other commands:

```bash
python tools/evaluate.py        # reproduce the citation-accuracy table above
python make_reel.py             # compile a clip from the CLI
python reindex_scenes.py        # rebuild the scene index at a new sampling interval
```

## Project layout

```
steal-the-mic/
├── ingest.py                 # upload + spoken/scene indexing
├── analyze_talk.py           # full analysis -> data/analysis__<id>.json
├── compute_metrics.py        # deterministic metrics + transcript cache
├── make_reel.py              # CLI clip compilation
├── reindex_scenes.py         # rebuild the scene index at a new interval
├── analysis/
│   ├── transcript.py         # word-level transcript shaping
│   ├── metrics.py            # pace, pauses, crutch phrases
│   ├── semantic.py           # devices, structure, pause intent, quote location
│   ├── chat.py               # evidence gathering + cited answers
│   └── reel.py               # captioned clips via the Editor timeline
├── backend/main.py           # FastAPI portal
├── frontend/                 # landing + chat UI (no build step, no framework)
├── tools/                    # isolated overlay positioning test
└── data/                     # manifest, analyses, cache (gitignored)
```

## Guardrails

- Dedicated collection; the account's other media is never listed or searched.
- Client-supplied video IDs are validated against the local manifest, and clip bounds are clamped to
  the real video — the API can't be used as a proxy for arbitrary media.
- Submitted URLs must match a YouTube URL pattern; question and URL lengths are bounded.
- Per-IP rate limiting, with a tighter bucket on endpoints that cost credits.
- **No `innerHTML` anywhere in the frontend** — all model output and transcript text goes in via
  `textContent`.

## Notes on the footage

TED licenses TED and TEDx content **CC BY-NC-ND** — no derivative works. This repository therefore
contains **no video, audio, transcript, or derived clip**, and redistributes none. The app is
source-neutral: it analyses whatever talk its operator supplies, and responsibility for that choice
rests with them. Talks belong to their respective speakers and publishers. This is an independent
educational project, not affiliated with or endorsed by TED.

## License

Application code is MIT licensed. This does not extend to any video content processed with it.
