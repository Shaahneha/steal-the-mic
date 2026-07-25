"""LLM-assisted analysis: structure, rhetorical devices, pause intent.

Two rules shape everything here.

1. **The LLM never supplies a timestamp.** It identifies a technique and quotes
   the line verbatim; we locate that quote in the word-level transcript to get
   exact times. A hallucinated number would silently mislabel the annotated reel,
   whereas a hallucinated quote simply fails to match and is dropped.

2. **Every structured call retries.** VideoDB's generate_text with JSON output is
   not reliable call-to-call (measured at 1 success in 3 identical calls on a
   previous project), so a single failure must not lose a whole analysis.
"""

import difflib
import json
import re

from . import transcript as T

MAX_RETRIES = 3


def generate_json(coll, prompt, retries=MAX_RETRIES, model_name=None):
    """Call generate_text expecting JSON, tolerating flaky structured output."""
    last_error = None
    for attempt in range(retries):
        try:
            kwargs = {"prompt": prompt, "response_type": "json"}
            if model_name:
                kwargs["model_name"] = model_name
            response = coll.generate_text(**kwargs)

            output = response.get("output") if isinstance(response, dict) else response
            if isinstance(output, (dict, list)):
                return output
            if isinstance(output, str):
                return json.loads(_strip_fences(output))
            raise ValueError(f"unexpected output type: {type(output)}")
        except Exception as e:  # noqa: BLE001 — any failure is worth one more try
            last_error = e
            if attempt < retries - 1:
                print(f"    (retry {attempt + 1}/{retries - 1} after: {str(e)[:80]})")
    print(f"    ! gave up after {retries} attempts: {str(last_error)[:120]}")
    return None


def _strip_fences(text):
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def as_list(result, key):
    """Coerce a generate_text JSON result into the list we asked for.

    VideoDB's JSON mode will not return a bare top-level array: asking for
    [{...}, {...}] yields a single flattened object, silently discarding every
    item but one. So every array is requested inside a named wrapper object and
    unwrapped here, with the loose shapes tolerated defensively.
    """
    if result is None:
        return []
    if isinstance(result, list):
        return [x for x in result if isinstance(x, dict)]
    if isinstance(result, dict):
        if isinstance(result.get(key), list):
            return [x for x in result[key] if isinstance(x, dict)]
        # Any single list value under another key.
        for value in result.values():
            if isinstance(value, list) and value and isinstance(value[0], dict):
                return value
        # A lone item that was meant to be the first element of the array.
        if any(k in result for k in ("device", "quote", "type", "n")):
            return [result]
    print(f"    ! unusable JSON shape for '{key}': {str(result)[:120]}")
    return []


def _normalise(text):
    return re.sub(r"[^a-z0-9 ]", "", text.lower()).strip()


def locate_quote(sents, quote, min_ratio=0.72):
    """Find a quoted line in the transcript and return its exact time span.

    Returns None when the quote does not really appear — which is exactly what
    should happen to a fabricated one, so bad extractions drop out on their own.
    """
    if not quote or not sents:
        return None

    target = _normalise(quote)
    if len(target) < 8:
        return None

    # Exact substring across a sliding window of consecutive sentences first.
    #
    # Both containment directions are allowed, but the matched window must be
    # substantial relative to the quote. Without that floor a one-word sentence
    # like "This." satisfies `joined in target` against almost any long quote,
    # which previously placed two different structural beats on the same
    # timestamp and would have pointed the annotated reel at the wrong moment.
    for width in (1, 2, 3, 4):
        for i in range(len(sents) - width + 1):
            window = sents[i:i + width]
            joined = _normalise(" ".join(s["text"] for s in window))
            if not joined:
                continue
            if target in joined or (joined in target and len(joined) >= 0.6 * len(target)):
                return {
                    "start": round(window[0]["start"], 2),
                    "end": round(window[-1]["end"], 2),
                    "text": " ".join(s["text"] for s in window),
                    "match": "exact",
                }

    # Fall back to closest fuzzy match on single sentences.
    best, best_ratio = None, 0.0
    for s in sents:
        ratio = difflib.SequenceMatcher(None, target, _normalise(s["text"])).ratio()
        if ratio > best_ratio:
            best, best_ratio = s, ratio

    if best and best_ratio >= min_ratio:
        return {
            "start": round(best["start"], 2),
            "end": round(best["end"], 2),
            "text": best["text"],
            "match": f"fuzzy:{best_ratio:.2f}",
        }
    return None


# --------------------------------------------------------------------------
# Pause intent — resolves what step 2 deliberately left unclassified
# --------------------------------------------------------------------------

PAUSE_PROMPT = """You are analysing silences in a recorded public talk to tell deliberate
speaking technique apart from audience reaction.

For each numbered silence below you get the words immediately BEFORE it and immediately AFTER it.

Classify each as exactly one of:
- "speaker_pause": the speaker deliberately held silence for effect (before a key line, after a
  weighty statement, to let an idea land).
- "audience_response": the room is laughing or applauding. Strong signals: the line before is a
  punchline, a joke, a visual gag, or a big applause-worthy declaration.
- "transition": the speaker is moving between sections, collecting notes, or changing topic.

Return ONLY a JSON object with a "classifications" array holding one entry per silence:
{"classifications": [{"n": <number>, "type": "<classification>", "why": "<max 12 words>"}]}

Include an entry for EVERY numbered silence listed.

Silences:
%s"""


def classify_pause_intent(coll, pauses, batch_size=12):
    """Ask the LLM to resolve pauses that duration alone cannot classify."""
    if not pauses:
        return {}

    verdicts = {}
    for offset in range(0, len(pauses), batch_size):
        batch = pauses[offset:offset + batch_size]
        listing = "\n".join(
            f'{offset + i + 1}. [{p["duration"]}s silence]\n'
            f'   BEFORE: "...{p["before"][-140:]}"\n'
            f'   AFTER:  "{p["after"][:140]}..."'
            for i, p in enumerate(batch)
        )
        result = generate_json(coll, PAUSE_PROMPT % listing)
        for item in as_list(result, "classifications"):
            try:
                verdicts[int(item["n"]) - 1] = {
                    "type": item.get("type"),
                    "why": item.get("why", ""),
                }
            except (KeyError, TypeError, ValueError):
                continue
    return verdicts


# --------------------------------------------------------------------------
# Rhetorical devices
# --------------------------------------------------------------------------

DEVICES_PROMPT = """You are a public-speaking coach identifying teachable technique in a talk transcript.

Find the clearest examples of these devices in the excerpt below:
- rule_of_three: three parallel items in a row
- anaphora: consecutive phrases starting with the same words
- rhetorical_question: a question asked without expecting an answer
- contrast: two opposing ideas set against each other ("not X, but Y")
- callback: referring back to an earlier moment in the talk
- self_disclosure: admitting a personal failure, fear, or vulnerability
- direct_address: speaking straight to the audience as "you"
- concrete_statistic: a specific number used as evidence
- punchline: a deliberate laugh line

Rules:
- Quote the line EXACTLY as it appears in the excerpt. Do not paraphrase or fix grammar.
- Only include genuinely clear examples. Five excellent ones beat twenty weak ones.
- Skip any device you cannot find a real example of.

Return ONLY a JSON object with a "devices" array:
{"devices": [{"device": "<name>", "quote": "<exact words from the excerpt>", "why_it_works": "<max 18 words>"}]}

Aim for 3-6 entries in the array for this excerpt.

Excerpt:
%s"""


def extract_devices(coll, chunks, sents):
    """Pull rhetorical devices per chunk, then locate each quote precisely."""
    found = []
    for i, chunk in enumerate(chunks, 1):
        print(f"    chunk {i}/{len(chunks)} ({chunk['start']:.0f}-{chunk['end']:.0f}s)")
        result = generate_json(coll, DEVICES_PROMPT % chunk["text"])
        items = as_list(result, "devices")
        dropped = 0

        for item in items:
            quote = item.get("quote", "")
            # Search within the chunk first for tighter, more accurate placement.
            location = locate_quote(chunk["sentences"], quote) or locate_quote(sents, quote)
            if not location:
                dropped += 1  # quote not really in the transcript — drop it
                continue
            found.append({
                "device": item.get("device", "unknown"),
                "quote": location["text"],
                "why_it_works": item.get("why_it_works", ""),
                "start": location["start"],
                "end": location["end"],
                "match": location["match"],
            })

        if dropped:
            print(f"      dropped {dropped}/{len(items)} (quote not found in transcript)")

    found.sort(key=lambda d: d["start"])
    return found


# --------------------------------------------------------------------------
# Talk structure
# --------------------------------------------------------------------------

STRUCTURE_PROMPT = """You are analysing how a public talk is built, so a learner can copy its shape.

Below is the full transcript with timestamps every couple of minutes.

Identify the talk's structural beats in order. Use these labels where they fit:
hook, context, tension, turning_point, evidence, insight, resolution, call_to_action

For each beat, quote the FIRST sentence of that beat EXACTLY as written in the transcript.

Also classify the opening hook as exactly one of:
story, question, statistic, provocation, humour, direct_promise

Return ONLY this JSON shape:
{
  "hook_type": "<one of the six>",
  "hook_analysis": "<max 30 words on why the opening earns attention>",
  "beats": [
    {"label": "<beat label>", "first_sentence": "<exact quote>", "purpose": "<max 15 words>"}
  ],
  "arc_summary": "<max 40 words describing the overall shape>"
}

Transcript:
%s"""


def extract_structure(coll, sents, full_text):
    result = generate_json(coll, STRUCTURE_PROMPT % full_text[:14000])
    if not result or not isinstance(result, dict):
        return None

    beats = []
    for beat in result.get("beats", []) or []:
        if not isinstance(beat, dict):
            continue
        location = locate_quote(sents, beat.get("first_sentence", ""))
        beats.append({
            "label": beat.get("label", "unknown"),
            "purpose": beat.get("purpose", ""),
            "quote": location["text"] if location else beat.get("first_sentence", ""),
            "start": location["start"] if location else None,
            "located": location is not None,
        })

    beats_with_time = [b for b in beats if b["start"] is not None]
    beats_with_time.sort(key=lambda b: b["start"])

    return {
        "hook_type": result.get("hook_type"),
        "hook_analysis": result.get("hook_analysis"),
        "arc_summary": result.get("arc_summary"),
        "beats": beats_with_time,
        "beats_unlocated": [b for b in beats if b["start"] is None],
    }
