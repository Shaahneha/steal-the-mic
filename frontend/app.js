/* TEDx Learning — front end.
 *
 * Security note: every string rendered here (talk titles, transcript quotes,
 * model answers, error text) is untrusted as far as the browser is concerned —
 * a model answer is shaped by the user's own question. Everything goes in via
 * textContent. There is no innerHTML assignment in this file, by design.
 */

const $ = (id) => document.getElementById(id);

let currentTalk = null;   // { videodb_id, title, ... }
let pollTimer = null;

/* ------------------------------------------------------------------ utils */

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

function mmss(seconds) {
  const s = Math.max(0, Math.round(Number(seconds) || 0));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

async function api(path, options) {
  const res = await fetch(path, options);
  if (!res.ok) {
    let detail = `Request failed (${res.status})`;
    try {
      const body = await res.json();
      if (body && body.detail) detail = body.detail;
    } catch (_) { /* keep generic */ }
    throw new Error(detail);
  }
  return res.json();
}

function postJSON(path, body) {
  return api(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

function showView(name) {
  ["landing", "processing", "talk"].forEach((v) => {
    $(`view-${v}`).hidden = v !== name;
  });
  $("talk-select").hidden = name !== "talk";
  $("new-talk").hidden = name !== "talk";
}

/* ------------------------------------------------------------------ theme */

$("theme-toggle").addEventListener("click", () => {
  const root = document.documentElement;
  const current = root.getAttribute("data-theme");
  root.setAttribute("data-theme",
    current === "dark" ? "light"
      : current === "light" ? "dark"
        : (window.matchMedia("(prefers-color-scheme: dark)").matches ? "light" : "dark"));
});

/* ---------------------------------------------------------------- landing */

$("url-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const input = $("url-input");
  const errorBox = $("url-error");
  errorBox.hidden = true;

  const url = input.value.trim();
  if (!url) return;

  try {
    const { job_id, cached } = await postJSON("/api/analyse", { url });
    showView("processing");
    $("proc-stage").textContent = cached ? "Already studied — loading…" : "Getting started…";
    pollJob(job_id);
  } catch (e) {
    clear(errorBox);
    errorBox.appendChild(el("div", null, e.message));
    errorBox.hidden = false;
  }
});

$("new-talk").addEventListener("click", () => {
  $("url-input").value = "";
  showView("landing");
  loadExisting();
});

async function loadExisting() {
  try {
    const talks = await api("/api/talks");
    const wrap = $("existing");
    const list = $("existing-list");
    clear(list);
    if (!talks.length) { wrap.hidden = true; return; }
    wrap.hidden = false;
    talks.forEach((t) => {
      const chip = el("button", "chip", t.title || t.videodb_id);
      chip.type = "button";
      chip.addEventListener("click", () => openTalk(t.videodb_id));
      list.appendChild(chip);
    });

    const select = $("talk-select");
    clear(select);
    talks.forEach((t) => {
      const option = el("option", null, t.title || t.videodb_id);
      option.value = t.videodb_id;
      select.appendChild(option);
    });
  } catch (_) { /* landing still works without the list */ }
}

$("talk-select").addEventListener("change", (ev) => openTalk(ev.target.value));

/* ------------------------------------------------------------- processing */

function pollJob(jobId) {
  if (pollTimer) clearInterval(pollTimer);
  const errorBox = $("proc-error");

  // An elapsed clock is the cheapest possible proof that something is still
  // happening — a progress bar that sits still for 40s looks broken without it.
  const started = Date.now();
  const clock = $("proc-elapsed");
  const elapsedTimer = setInterval(() => {
    const secs = Math.round((Date.now() - started) / 1000);
    clock.textContent = secs < 60
      ? `${secs}s elapsed`
      : `${Math.floor(secs / 60)}m ${secs % 60}s elapsed`;
  }, 1000);

  const stop = () => {
    clearInterval(pollTimer);
    clearInterval(elapsedTimer);
  };

  pollTimer = setInterval(async () => {
    let job;
    try {
      job = await api(`/api/job/${encodeURIComponent(jobId)}`);
    } catch (e) {
      stop();
      clear(errorBox);
      errorBox.appendChild(el("div", null, e.message));
      errorBox.hidden = false;
      return;
    }

    $("proc-stage").textContent = `${job.stage}…`;
    $("proc-fill").style.width = `${Math.max(4, job.progress || 0)}%`;

    [...$("stage-list").children].forEach((li) => {
      const at = Number(li.dataset.at);
      li.classList.toggle("done", (job.progress || 0) > at);
      li.classList.toggle("active", (job.progress || 0) >= at && (job.progress || 0) <= at + 13);
    });

    if (job.status === "error") {
      stop();
      clear(errorBox);
      errorBox.appendChild(el("div", null, job.error || "Analysis failed."));
      const back = el("button", "ghost", "Try another talk");
      back.addEventListener("click", () => { showView("landing"); loadExisting(); });
      errorBox.appendChild(back);
      errorBox.hidden = false;
    }
    if (job.status === "done") {
      stop();
      openTalk(job.video_id);
    }
  }, 2000);
}

/* -------------------------------------------------------------- talk view */

async function openTalk(videoId) {
  const analysis = await api(`/api/analysis/${encodeURIComponent(videoId)}`);
  currentTalk = analysis;

  showView("talk");
  $("talk-select").value = videoId;
  $("talk-title").textContent = analysis.title || "This talk";

  const dramatic = analysis.pauses.teachable.filter((p) => p.band === "dramatic").length;
  $("talk-meta").textContent =
    `${mmss(analysis.duration)} · ${analysis.word_count} words · analysed`;

  const source = $("talk-source");
  if (analysis.source_url) {
    source.href = analysis.source_url;
    source.hidden = false;
  } else {
    source.hidden = true;
  }

  renderQuickStats(analysis, dramatic);
  clear($("chat-thread"));
  addSystemCard(analysis);
  await renderSuggestions();
  await loadExisting();
  $("chat-input").focus();
}

function renderQuickStats(a, dramatic) {
  const host = $("quickstats");
  clear(host);
  const stats = [
    [`${Math.round((a.silence_ratio || 0) * 100)}%`, "of the talk is silence"],
    [a.speaking_wpm ?? "—", "words/min while speaking"],
    [dramatic, "dramatic pauses"],
    [(a.devices || []).length, "techniques found"],
  ];
  stats.forEach(([value, label]) => {
    const tile = el("div", "qstat");
    tile.appendChild(el("div", "qstat-value", value));
    tile.appendChild(el("div", "qstat-label", label));
    host.appendChild(tile);
  });
}

function addSystemCard(a) {
  const card = el("div", "msg system");
  card.appendChild(el("p", null,
    "I've studied this talk — the transcript, the pacing, and how the speaker moves and gestures. "
    + "Ask how they do something and I'll show you the exact moments."));

  if (a.structure && a.structure.hook_type) {
    card.appendChild(el("p", "muted small",
      `Opens with a ${String(a.structure.hook_type).replace(/_/g, " ")} hook. `
      + (a.structure.arc_summary || "")));
  }
  $("chat-thread").appendChild(card);
}

async function renderSuggestions() {
  const host = $("suggestions");
  clear(host);
  let items = [];
  try { items = await api("/api/suggestions"); } catch (_) { return; }
  items.forEach((q) => {
    const chip = el("button", "chip", q);
    chip.type = "button";
    chip.addEventListener("click", () => ask(q));
    host.appendChild(chip);
  });
}

/* -------------------------------------------------------------------- chat */

$("chat-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  const input = $("chat-input");
  const question = input.value.trim();
  if (!question) return;
  input.value = "";
  ask(question);
});

async function ask(question) {
  if (!currentTalk) return;
  const thread = $("chat-thread");

  const mine = el("div", "msg you");
  mine.appendChild(el("p", null, question));
  thread.appendChild(mine);

  // Static text reads as "stuck" during a 20-30s wait. A moving indicator plus
  // a changing status makes it visibly alive.
  const pending = el("div", "msg bot pending");
  const row = el("p", "thinking");
  row.appendChild(el("span", "spinner"));
  const label = el("span", null, "Searching the transcript…");
  row.appendChild(label);
  const clock = el("span", "muted small elapsed", "0s");
  row.appendChild(clock);
  pending.appendChild(row);
  thread.appendChild(pending);

  const steps = [
    "Searching the transcript…",
    "Checking how it was delivered…",
    "Pulling the measured pauses…",
    "Writing the coaching note…",
    "Locating the exact quotes…",
  ];
  const started = Date.now();
  const ticker = setInterval(() => {
    const secs = Math.round((Date.now() - started) / 1000);
    clock.textContent = `${secs}s`;
    label.textContent = steps[Math.min(steps.length - 1, Math.floor(secs / 5))];
  }, 1000);
  thread.scrollTop = thread.scrollHeight;
  $("chat-send").disabled = true;

  try {
    const result = await postJSON(
      `/api/chat/${encodeURIComponent(currentTalk.videodb_id)}`, { question });
    thread.removeChild(pending);
    thread.appendChild(buildAnswer(result));
  } catch (e) {
    thread.removeChild(pending);
    const errorMsg = el("div", "msg bot");
    errorMsg.appendChild(el("p", "error-text", e.message));
    thread.appendChild(errorMsg);
  } finally {
    clearInterval(ticker);
    $("chat-send").disabled = false;
    thread.scrollTop = thread.scrollHeight;
  }
}

/* A single dense block is hard to read on screen. Prefer the model's own
 * paragraph breaks; if it ignored them, split long text into 2-sentence groups
 * so an answer is never one unbroken wall. */
function toParagraphs(text) {
  const raw = String(text || "").trim();
  if (!raw) return [];

  const byBlank = raw.split(/\n\s*\n/).map((s) => s.trim()).filter(Boolean);
  if (byBlank.length > 1) return byBlank;

  if (raw.length <= 300) return [raw];

  const sentences = raw.match(/[^.!?]+[.!?]+(\s|$)/g) || [raw];
  const out = [];
  for (let i = 0; i < sentences.length; i += 2) {
    out.push(sentences.slice(i, i + 2).join("").trim());
  }
  return out.filter(Boolean);
}

/* Renders a paragraph, styling any quoted line so the evidence stands out from
 * the coaching around it. Each piece goes in via textContent — no innerHTML. */
function paragraphNode(text) {
  const p = el("p");
  const parts = String(text).split(/([“"][^“”"]{6,}[”"])/g);
  parts.forEach((part) => {
    if (!part) return;
    if (/^[“"].*[”"]$/.test(part) && part.length > 8) {
      p.appendChild(el("span", "said", part));
    } else {
      p.appendChild(document.createTextNode(part));
    }
  });
  return p;
}

function buildAnswer(result) {
  const card = el("div", "msg bot");
  toParagraphs(result.answer).forEach((para) => card.appendChild(paragraphNode(para)));

  if (result.practice) {
    const practice = el("div", "practice");
    practice.appendChild(el("strong", null, "Practise this: "));
    practice.appendChild(el("span", null, result.practice));
    card.appendChild(practice);
  }

  const citations = result.citations || [];
  if (citations.length) {
    const list = el("div", "citations");
    list.appendChild(el("div", "citations-head", "The moments that show it"));

    citations.forEach((c, i) => {
      const row = el("div", "citation");
      const head = el("div", "citation-head");
      head.appendChild(el("span", "cite-num", `${i + 1}`));
      if (c.technique) head.appendChild(el("span", "cite-technique", c.technique));
      head.appendChild(el("span", "timecode", mmss(c.start)));
      if (c.note) head.appendChild(el("span", "cite-note", c.note));
      row.appendChild(head);
      row.appendChild(el("p", "cite-quote", `“${c.quote}”`));
      list.appendChild(row);
    });

    // The clip is the point of the answer, so build it immediately rather than
    // making the learner ask for it a second time. A placeholder holds the
    // space so the layout doesn't jump when the player arrives.
    const slot = el("div", "demo-slot");
    const pending = el("div", "demo-pending");
    pending.appendChild(el("span", "spinner"));
    pending.appendChild(el("span", null, "Cutting the clip that shows this…"));
    slot.appendChild(pending);
    list.appendChild(slot);

    card.appendChild(list);
    compileDemo(citations, slot);
  }
  return card;
}

async function compileDemo(citations, slot) {
  // The technique becomes the on-clip title chip; the note becomes the
  // "what to watch for" line beneath it.
  const moments = citations.map((c) => ({
    start: c.start,
    end: c.end,
    label: (c.technique || "Technique").slice(0, 44),
    note: c.note || "",
  }));

  const talkId = currentTalk.videodb_id;
  try {
    const result = await postJSON(
      `/api/demo/${encodeURIComponent(talkId)}`, { moments });

    // The learner may have switched talks while this was rendering.
    if (!currentTalk || currentTalk.videodb_id !== talkId) return;

    clear(slot);
    const wrap = el("div", "demo");
    const video = el("video");
    video.controls = true;
    video.playsInline = true;
    video.preload = "auto";
    wrap.appendChild(video);

    const foot = el("div", "demo-foot");
    const n = (result.clips || []).length;
    foot.appendChild(el("span", "muted small",
      `${n} moment${n === 1 ? "" : "s"} · ${Math.round(result.total_seconds)}s`));
    const link = el("a", "ghost-link", "Open in the VideoDB player ↗");
    link.href = `https://console.videodb.io/player?url=${encodeURIComponent(result.stream_url)}`;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    foot.appendChild(link);
    wrap.appendChild(foot);

    slot.appendChild(wrap);
    attachStream(video, result.stream_url);
  } catch (e) {
    clear(slot);
    slot.appendChild(el("p", "error-text", e.message));
    const retry = el("button", "ghost demo-btn", "▶ Retry building the clip");
    retry.type = "button";
    retry.addEventListener("click", () => {
      clear(slot);
      const again = el("div", "demo-pending");
      again.appendChild(el("span", "spinner"));
      again.appendChild(el("span", null, "Cutting the clip…"));
      slot.appendChild(again);
      compileDemo(citations, slot);
    });
    slot.appendChild(retry);
  }
}

function attachStream(video, url) {
  if (video.canPlayType("application/vnd.apple.mpegurl")) {
    video.src = url;                        // Safari plays HLS natively
  } else if (window.Hls && window.Hls.isSupported()) {
    const hls = new window.Hls();
    hls.loadSource(url);
    hls.attachMedia(video);
  }
  // Otherwise the "open in player" link beside it is the fallback.
}

/* ------------------------------------------------------------------- boot */

loadExisting();
