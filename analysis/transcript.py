"""Transcript loading and shaping.

VideoDB returns a *word-level* transcript for talks: one segment per word, each
with its own start/end. That precision is what makes exact pause measurement and
tight clip boundaries possible, but it means anything that needs sentences (LLM
chunking, quoting a line) has to rebuild them here first.

Non-speech is marked by segments whose text is "-". Those are silence *or*
audience noise — the ASR does not distinguish them, which is why pause
classification needs a semantic pass (see metrics.classify_pauses).
"""

NON_SPEECH = "-"
SENTENCE_END = (".", "!", "?")


def word_segments(raw_transcript):
    """Real spoken words only, in time order."""
    words = [
        s for s in raw_transcript
        if s.get("text", "").strip() not in ("", NON_SPEECH)
    ]
    return sorted(words, key=lambda s: s["start"])


def non_speech_segments(raw_transcript):
    """Gaps the ASR marked as non-speech (silence or audience noise)."""
    return [
        s for s in raw_transcript
        if s.get("text", "").strip() == NON_SPEECH
    ]


def sentences(words):
    """Group word segments into sentences with accurate start/end times."""
    out, current = [], []
    for w in words:
        current.append(w)
        if w["text"].strip().endswith(SENTENCE_END):
            out.append(_build_sentence(current))
            current = []
    if current:
        out.append(_build_sentence(current))
    return out


def _build_sentence(words):
    return {
        "text": " ".join(w["text"].strip() for w in words),
        "start": words[0]["start"],
        "end": words[-1]["end"],
        "word_count": len(words),
    }


def chunk_sentences(sents, target_seconds=120):
    """Group sentences into ~target_seconds chunks for LLM processing."""
    chunks, current = [], []
    for s in sents:
        if current and s["end"] - current[0]["start"] > target_seconds:
            chunks.append(_build_chunk(current))
            current = []
        current.append(s)
    if current:
        chunks.append(_build_chunk(current))
    return chunks


def _build_chunk(sents):
    return {
        "text": " ".join(s["text"] for s in sents),
        "start": sents[0]["start"],
        "end": sents[-1]["end"],
        "sentences": sents,
    }


def text_between(words, start, end):
    """Spoken text within a time window."""
    return " ".join(
        w["text"].strip() for w in words
        if w["start"] >= start and w["end"] <= end
    )
