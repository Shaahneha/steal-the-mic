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

<!-- Homepage screenshot: add the actual image file at assets/screenshot.png -->
<img width="1614" height="905" alt="image" src="https://github.com/user-attachments/assets/31f21e3f-d2b2-4396-ace3-e3f22771caef" />

## Try it

| | | |
|---|---|---|
| **Demo video** | https://www.loom.com/share/c51b4c2f04e84f8ca3aa3104ccee9ed5 |
| **Run it yourself** | `docker run -p 8000:7860 -e VIDEO_DB_API_KEY=… ghcr.io/shaahneha/steal-the-mic` |
| **Source** | https://github.com/Shaahneha/steal-the-mic |

Five talks ship pre-analysed — two Toastmasters world champions, a presidential address, a
conference talk, and one in Hindi that deliberately shows where the pipeline degrades. Running it
locally takes four commands; pasting your own YouTube link then runs the full pipeline live.

---

## The loop: perceive → remember → act

| | | |
|---|---|
| **Perceive** | Two indexes per talk, not one. `index_spoken_words` gives a word-level transcript; `index_scenes` runs every 6 seconds with a delivery-specific prompt that returns posture, gestur[...]
| **Remember** | Conversations persist per talk on disk. A follow-up like *"and the ending?"* resolves against earlier turns, moments already shown are deprioritised so answers stop repeating them[...]
| **Act** | Answers are compiled, not just written: cited moments become a short clip through the Editor timeline with the technique captioned over the footage — and clips are snapped away from [...]

## VideoDB primitives used

| Pillar | Primitive | Where |
|--------|-----------|-------|
| **Ingest** | `coll.upload(url=...)` — ingest any talk from a YouTube URL | `ingest.py` |
| **Ingest** | `conn.create_collection()` — a dedicated collection, so the app never touches unrelated media in the account | `ingest.py::get_project_collection` |
| **Index** | `video.index_spoken_words()` — word-level transcript | `ingest.py` |
| **Index** | `video.index_scenes(extraction_type=time_based)` — delivery-focused visual index every 6s | `ingest.py` |
| **Search** | `video.search(search_type=semantic)` — spoken-word semantic search for chat evidence | `analysis/chat.py` |
| **Search** | `video.search(index_type=IndexType.scene, scene_index_id=…)` — visual search for delivery questions | `analysis/chat.py` |
| **Search** | `coll.search(search_type=semantic)` — across every studied talk at once, merged with per-video results so one talk's abundance of matches can't crowd the others out | `analysis/ch[...]
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

Measur[...]
