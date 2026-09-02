/* mynah — one page, one polled state object.
 *
 * The server is the only source of truth. This file never keeps its own copy of
 * the project; it renders whatever /api/state last returned. The one concession
 * is that a field the user is typing in is left alone until it loses focus, so
 * a poll landing mid-sentence cannot eat the caret.
 *
 * Which project is shown lives in the URL (?p=<id>), so a reload — or a second
 * tab — lands on the same one. Empty means "whichever was touched last".
 *
 * Layout: the chunk list is the page. Voices (global) live in a drawer, the
 * script composer in a sheet, sampling in a second drawer.
 */

const $ = (id) => document.getElementById(id);
let STATE = null;
let PID = new URLSearchParams(location.search).get('p') || '';
const rows = new Map();        // chunk id -> row element
const cards = new Map();       // voice id -> card element
let recorder = null;
let playing = null;            // the one <audio> allowed to be audible
let previewLoading = false;    // guards the Preview button's disabled state
                                // against being clobbered by the next poll

/* The model refuses a reference under 5 s of audio. The browser drops a few
   hundred ms between start() and the first captured sample, so a timer that
   reads 5.0 can be 4.3 s of audio. Six seconds on the clock keeps a margin. */
const MIN_RECORD = 6;

const P = (path = '') => `/api/projects/${PID}${path}`;
const stateUrl = () => PID ? `/api/state?p=${PID}` : '/api/state';

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: options.body instanceof FormData ? {} : { 'Content-Type': 'application/json' },
    ...options,
  });
  if (!response.ok) {
    let detail = response.statusText;
    try { detail = (await response.json()).detail || detail; } catch {}
    throw new Error(detail);
  }
  return response.status === 204 ? null : response.json();
}
const post = (path, body) => api(path, { method: 'POST', body: JSON.stringify(body ?? {}) });
const put = (path, body) => api(path, { method: 'PUT', body: JSON.stringify(body) });
const del = (path) => api(path, { method: 'DELETE' });
const fail = (error) => { console.error(error); toast(error.message || String(error)); };

function toast(message) {
  // The activity line doubles as a toast: short-lived, non-modal, dismissable.
  $('queue').textContent = message;
  $('queue').classList.add('bad');
  setTimeout(() => $('queue').classList.remove('bad'), 4000);
}

/* ---- apply ------------------------------------------------------------- */

function apply(state) {
  if (!state) return;
  STATE = state;
  if (state.project.id !== PID) {
    PID = state.project.id;
    for (const li of rows.values()) li.remove();
    rows.clear();
    if (playing) { playing.pause(); playing = null; }
    closePreview();
  }
  if (new URLSearchParams(location.search).get('p') !== PID) {
    history.replaceState(null, '', `?p=${PID}`);
  }
  renderEngine(state.engine, state.queue);
  renderProjects(state.projects, state.project.id);
  renderVoicePicker(state.voices, state.project.voice_id);
  renderVoiceCards(state.voices, state.project.voice_id);
  renderChunks(state.project);
  renderParams(state.project.params);
  if (document.activeElement !== $('title')) $('title').value = state.project.title;
  $('log').textContent = state.log.slice().reverse().join('\n');
}

/* ---- header ------------------------------------------------------------ */

function renderEngine(engine, queue) {
  const pill = $('engine');
  const label = { cold: 'idle', loading: 'loading model…', ready: 'ready', error: 'error' };
  const text = engine.state === 'loading' && engine.progress ? engine.progress
             : label[engine.state] || engine.state;
  pill.textContent = `${text} · ${engine.device}`;
  pill.className = 'pill ' + (engine.state === 'ready' ? 'ready'
    : engine.state === 'error' ? 'error' : 'loading');
  pill.title = engine.error || (engine.loader ? `weights: ${engine.loader}` : '');
  if (!$('queue').classList.contains('bad')) {
    $('queue').textContent = queue.pending || queue.current
      ? `${queue.current ? 'rendering' : ''}${queue.current && queue.pending ? ' · ' : ''}${queue.pending ? `${queue.pending} queued` : ''}`
      : '';
  }
  // Stop aborts the line being rendered as well as clearing the queue.
  $('stop').disabled = !queue.pending && !queue.current;
}

function renderProjects(projects, currentId) {
  const select = $('project-select');
  const signature = projects.map(p => `${p.id}:${p.title}:${p.chunks}:${p.counts.ready || 0}`).join('|') + `#${currentId}`;
  if (select.dataset.signature !== signature) {
    select.dataset.signature = signature;
    select.innerHTML = '';
    for (const project of projects) {
      const option = document.createElement('option');
      option.value = project.id;
      option.textContent = `${project.title} · ${project.counts.ready || 0}/${project.chunks}`;
      select.append(option);
    }
    select.value = currentId;
  }
  $('delete-project').disabled = projects.length < 2;
}

/* ---- voices: compact picker on the page ------------------------------- */

function renderVoicePicker(voices, selected) {
  const select = $('voice-select');
  const signature = voices.map(v => `${v.id}:${v.status}:${v.name}`).join('|') + `#${selected}`;
  if (select.dataset.signature !== signature) {
    select.dataset.signature = signature;
    select.innerHTML = '<option value="">— none —</option>';
    for (const voice of voices) {
      if (voice.status === 'error') continue;      // failures live in the drawer
      const option = document.createElement('option');
      option.value = voice.id;
      option.textContent = voice.status === 'ready' ? voice.name : `${voice.name} (compiling…)`;
      select.append(option);
    }
    const manage = document.createElement('option');
    manage.value = '__manage__';
    manage.textContent = 'Manage voices…';
    select.append(manage);
    select.value = selected || '';
  }
  const current = voices.find(v => v.id === selected);
  const failed = voices.filter(v => v.status === 'error').length;
  const compiling = voices.filter(v => v.status === 'compiling').length;
  $('voice-hint').textContent = !voices.length ? 'record or upload one to begin'
    : !current ? 'pick a voice to generate'
    : current.status === 'ready' ? `${current.seconds}s reference`
    : 'compiling…';
  const badge = $('voice-badge');
  badge.hidden = !(failed || compiling);
  badge.textContent = failed ? `${failed} failed` : `${compiling} compiling`;
  badge.className = 'badge' + (failed ? ' attention' : '');
}

/* ---- voices: cards in the drawer -------------------------------------- */

function buildCard(voice) {
  const li = document.createElement('li');
  li.className = 'voice-card';
  li.innerHTML = `
    <div class="v-head">
      <input class="v-name" spellcheck="false" title="Rename">
      <span class="v-meta"></span>
    </div>
    <audio controls preload="metadata"></audio>
    <div class="v-actions">
      <button class="btn use">Use in this project</button>
      <span class="grow"></span>
      <button class="ghost danger del">Delete</button>
    </div>
    <p class="v-error" hidden></p>`;
  const name = li.querySelector('.v-name');
  name.addEventListener('change', () =>
    put(`/api/voices/${voice.id}?p=${PID}`, { name: name.value }).then(apply).catch(fail));
  name.addEventListener('keydown', (e) => { if (e.key === 'Enter') name.blur(); });
  li.querySelector('.use').addEventListener('click', () =>
    post(P(), { voice_id: voice.id }).then(apply).catch(fail));
  li.querySelector('.del').addEventListener('click', () => {
    if (confirm(`Delete "${name.value}"? Projects using it will need another voice.`)) {
      del(`/api/voices/${voice.id}?p=${PID}`).then(apply).catch(fail);
    }
  });
  return li;
}

function renderVoiceCards(voices, selected) {
  const list = $('voice-list');
  const seen = new Set();
  voices.forEach((voice, index) => {
    seen.add(voice.id);
    let li = cards.get(voice.id);
    if (!li) { li = buildCard(voice); cards.set(voice.id, li); }
    if (list.children[index] !== li) list.insertBefore(li, list.children[index] || null);
    const current = voice.id === selected;
    li.classList.toggle('current', current);
    const name = li.querySelector('.v-name');
    if (document.activeElement !== name && name.value !== voice.name) name.value = voice.name;
    const meta = li.querySelector('.v-meta');
    meta.textContent = voice.status === 'ready' ? `${voice.seconds}s`
      : voice.status === 'error' ? 'failed' : 'compiling…';
    meta.className = `v-meta ${voice.status === 'ready' ? '' : voice.status}`;
    const audio = li.querySelector('audio');
    const src = `/api/voices/${voice.id}/reference.wav`;
    if (voice.status !== 'error' && !audio.getAttribute('src')) audio.setAttribute('src', src);
    audio.hidden = voice.status === 'error';
    const use = li.querySelector('.use');
    use.disabled = current || voice.status !== 'ready';
    use.textContent = current ? 'In use here' : 'Use in this project';
    const error = li.querySelector('.v-error');
    error.hidden = !voice.error;
    error.textContent = voice.error || '';
  });
  for (const [id, li] of cards) if (!seen.has(id)) { li.remove(); cards.delete(id); }
  $('voices-empty').hidden = voices.length > 0;
}

/* ---- chunks ------------------------------------------------------------ */

function autosize(textarea) {
  textarea.style.height = 'auto';
  textarea.style.height = `${textarea.scrollHeight + 2}px`;
}

function buildRow(chunk) {
  const li = document.createElement('li');
  li.className = 'chunk';
  li.innerHTML = `
    <span class="n"><span class="dot"></span><span class="idx"></span></span>
    <textarea rows="1" spellcheck="false" placeholder="Empty line — type something to say"></textarea>
    <span class="side">
      <button class="ghost icon play" title="Play take">▶</button>
      <button class="ghost icon regen" title="Generate this line">↻</button>
      <input class="pause" type="number" step="0.1" min="0" max="10" title="Pause after, seconds"><span class="unit">s</span>
      <button class="ghost icon danger drop" title="Delete line">✕</button>
    </span>
    <span class="err" hidden></span>`;
  const text = li.querySelector('textarea');
  text.addEventListener('input', () => autosize(text));
  text.addEventListener('change', () => put(P(`/chunks/${chunk.id}`), { text: text.value }).then(apply).catch(fail));
  li.querySelector('.pause').addEventListener('change', (e) =>
    put(P(`/chunks/${chunk.id}`), { pause_after: +e.target.value }).then(apply).catch(fail));
  li.querySelector('.regen').addEventListener('click', () =>
    post(P(`/chunks/${chunk.id}/generate`)).then(apply).catch(fail));
  li.querySelector('.drop').addEventListener('click', () =>
    del(P(`/chunks/${chunk.id}`)).then(apply).catch(fail));
  li.querySelector('.play').addEventListener('click', () => {
    // Only one audible thing at a time — a chunk take and the stitched
    // preview compete for the same "what am I listening to" slot.
    if (!previewAudio.paused) previewAudio.pause();
    if (playing) {
      const same = playing.dataset.chunk === chunk.id;
      playing.pause(); playing = null;
      if (same) return;
    }
    const audio = new Audio(P(`/takes/${chunk.id}.wav?v=${li.dataset.fingerprint}`));
    audio.dataset.chunk = chunk.id;
    audio.onended = () => { if (playing === audio) playing = null; };
    playing = audio;
    audio.play().catch(fail);
  });
  return li;
}

function renderChunks(project) {
  const list = $('chunks');
  const seen = new Set();
  project.chunks.forEach((chunk, index) => {
    seen.add(chunk.id);
    let li = rows.get(chunk.id);
    const fresh = !li;
    if (fresh) { li = buildRow(chunk); rows.set(chunk.id, li); }
    if (li.parentNode !== list || list.children[index] !== li) {
      list.insertBefore(li, list.children[index] || null);
    }
    // now-playing is owned by highlightPlayingLine(), which only touches the
    // DOM when the playing segment actually changes — an unconditional reset
    // here would erase it on every poll tick and never get a chance to
    // reapply, since from the highlighter's perspective nothing changed.
    li.className = `chunk s-${chunk.status}${li.classList.contains('now-playing') ? ' now-playing' : ''}`;
    li.dataset.fingerprint = chunk.fingerprint;
    li.querySelector('.idx').textContent = index + 1;
    li.querySelector('.n').title = chunk.status;
    const text = li.querySelector('textarea');
    if (document.activeElement !== text && text.value !== chunk.text) { text.value = chunk.text; autosize(text); }
    else if (fresh) autosize(text);
    const pause = li.querySelector('.pause');
    if (document.activeElement !== pause) pause.value = chunk.pause_after;
    li.querySelector('.play').disabled = chunk.status !== 'ready';
    li.querySelector('.regen').disabled = ['queued', 'rendering', 'empty', 'no-voice'].includes(chunk.status);
    const error = li.querySelector('.err');
    error.hidden = !chunk.error;
    error.textContent = chunk.error || '';
  });
  for (const [id, li] of rows) if (!seen.has(id)) { li.remove(); rows.delete(id); }

  const tally = project.chunks.reduce((acc, c) => (acc[c.status] = (acc[c.status] || 0) + 1, acc), {});
  const order = ['ready', 'stale', 'queued', 'rendering', 'no-voice', 'empty'];
  $('counts').textContent = order.filter(k => tally[k]).map(k => `${tally[k]} ${k}`).join(' · ');
  $('empty').hidden = project.chunks.length > 0;
  $('generate').disabled = !(tally.stale > 0);
  const count = $('stale-count');
  count.hidden = !(tally.stale > 0);
  count.textContent = tally.stale || '';
  const canRender = (tally.ready > 0) && !(tally.stale || tally.queued || tally.rendering);
  $('export').disabled = !canRender;
  // Not disabled outright while a preview is already loading — openPreview()
  // owns that state until its own canplay/error fires, or the next poll
  // (which runs every 700ms-2.5s) would flip the button back on mid-fetch.
  if (!previewLoading) $('preview-open').disabled = !canRender;
}

function renderParams(params) {
  for (const [key, value] of Object.entries(params)) {
    const input = $(`p-${key}`);
    if (input && document.activeElement !== input) input.value = value;
  }
}

/* ---- overlays ---------------------------------------------------------- */

const OVERLAYS = ['script-modal', 'voices-drawer', 'settings-drawer'];
function openOverlay(id) {
  for (const other of OVERLAYS) $(other).hidden = other !== id;
  $('backdrop').hidden = false;
  const focus = $(id).querySelector('textarea, input, button:not([data-close])');
  if (focus) setTimeout(() => focus.focus(), 30);
}
function closeOverlays() {
  for (const id of OVERLAYS) $(id).hidden = true;
  $('backdrop').hidden = true;
}
document.querySelectorAll('[data-close]').forEach(b => b.addEventListener('click', closeOverlays));
$('backdrop').addEventListener('click', closeOverlays);
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') { closeOverlays(); return; }
  // Cmd/Ctrl+Enter generates whatever is stale, from anywhere on the page.
  if (e.key === 'Enter' && (e.metaKey || e.ctrlKey) && !$('generate').disabled) {
    e.preventDefault();
    if (document.activeElement && document.activeElement.tagName === 'TEXTAREA') document.activeElement.blur();
    setTimeout(() => post(P('/generate')).then(apply).catch(fail), 60);
  }
});
$('open-voices').addEventListener('click', () => openOverlay('voices-drawer'));
$('open-settings').addEventListener('click', () => openOverlay('settings-drawer'));
$('paste-script').addEventListener('click', () => openOverlay('script-modal'));
document.querySelectorAll('[data-open-script]').forEach(b => b.addEventListener('click', () => openOverlay('script-modal')));
document.querySelectorAll('[data-add-chunk]').forEach(b => b.addEventListener('click', () => post(P('/chunks'), { text: '' }).then(apply).catch(fail)));
$('activity-toggle').addEventListener('click', () => { $('activity').hidden = !$('activity').hidden; });

/* ---- recording --------------------------------------------------------- */

async function startRecording() {
  const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  const chunks = [];
  recorder = new MediaRecorder(stream);
  const timer = $('rec-timer');
  const note = $('rec-note');
  note.hidden = true;
  timer.hidden = false;
  timer.textContent = '0.0s';
  timer.style.color = 'var(--bad)';
  let started = 0, seconds = 0;
  recorder.onstart = () => { started = Date.now(); };
  const tick = setInterval(() => {
    if (!started) return;
    seconds = (Date.now() - started) / 1000;
    timer.textContent = `${seconds.toFixed(1)}s`;
    timer.style.color = seconds < MIN_RECORD ? 'var(--bad)' : 'var(--ok)';
  }, 100);
  recorder.ondataavailable = (event) => chunks.push(event.data);
  recorder.onstop = async () => {
    clearInterval(tick);
    timer.hidden = true;
    stream.getTracks().forEach(track => track.stop());
    $('record').classList.remove('recording');
    $('record').textContent = '● Record';
    const mimeType = recorder.mimeType;
    recorder = null;
    if (seconds < MIN_RECORD) {
      note.textContent = `Recording was ${seconds.toFixed(1)}s — keep going past ${MIN_RECORD}s so the model gets its 5s of speech. Nothing was saved.`;
      note.style.color = 'var(--warn)';
      note.hidden = false;
      return;
    }
    const blob = new Blob(chunks, { type: mimeType });
    await sendVoice(new File([blob], 'recording.webm', { type: blob.type }));
  };
  recorder.start();
  $('record').classList.add('recording');
  $('record').textContent = '■ Stop';
}

async function sendVoice(file) {
  const name = prompt('Name this voice', file.name.replace(/\.[^.]+$/, '')) ?? '';
  if (name === '') return;
  const form = new FormData();
  form.append('file', file);
  const response = await fetch(`/api/voices?name=${encodeURIComponent(name)}&p=${PID}`, { method: 'POST', body: form });
  if (!response.ok) { toast('upload failed'); return; }
  // The server selects the new voice itself, and puts the previous one back
  // if compilation fails — so a bad upload never strands the project.
  apply(await response.json());
}

$('record').addEventListener('click', () => {
  if (recorder) recorder.stop(); else startRecording().catch(e => toast(`microphone: ${e.message}`));
});
$('upload').addEventListener('click', () => $('file').click());
$('file').addEventListener('change', (event) => {
  if (event.target.files[0]) sendVoice(event.target.files[0]);
  event.target.value = '';
});

/* ---- stitched playback -------------------------------------------------- */
/* The same mix Export downloads, played in the page. `/timeline` says where
   each line lands in it, so the line currently sounding can be highlighted —
   the point of doing this here rather than just linking to the WAV. */

const previewAudio = $('preview-audio');
let timelineSegments = [];     // [{id, start, end}], seconds, from /timeline
let lastHighlighted = null;    // chunk id, so timeupdate only touches the DOM on change

function formatTime(seconds) {
  seconds = Number.isFinite(seconds) && seconds > 0 ? seconds : 0;
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${m}:${String(s).padStart(2, '0')}`;
}

function highlightPlayingLine() {
  const t = previewAudio.currentTime;
  const segment = timelineSegments.find(s => t >= s.start && t < s.end);
  const id = segment ? segment.id : null;
  if (id === lastHighlighted) return;
  lastHighlighted = id;
  for (const [chunkId, li] of rows) li.classList.toggle('now-playing', chunkId === id);
  if (id) rows.get(id)?.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
}

async function openPreview() {
  if (previewLoading) return;
  previewLoading = true;
  $('preview-open').disabled = true;
  $('preview-open').textContent = 'Loading…';
  // Stitched playback and chunk playback are still only one audible thing.
  if (playing) { playing.pause(); playing = null; }
  try {
    timelineSegments = (await api(P('/timeline'))).segments;
  } catch (error) {
    timelineSegments = [];     // still play — just no line highlight
  }
  previewAudio.src = P('/preview.wav');
  previewAudio.load();
}

function closePreview() {
  previewLoading = false;
  previewAudio.pause();
  previewAudio.removeAttribute('src');
  previewAudio.load();
  timelineSegments = [];
  lastHighlighted = null;
  for (const li of rows.values()) li.classList.remove('now-playing');
  $('preview-active').hidden = true;
  $('preview-open').hidden = false;
  $('preview-open').disabled = false;
  $('preview-open').textContent = '▶ Preview';
  $('preview-seek').value = 0;
  $('preview-time').textContent = '0:00 / 0:00';
}

previewAudio.addEventListener('canplay', () => {
  if (!previewLoading) return;         // a stray event after closePreview()
  previewLoading = false;
  $('preview-open').hidden = true;
  $('preview-active').hidden = false;
  previewAudio.play().catch(fail);
});
previewAudio.addEventListener('error', () => {
  if (!previewLoading) return;
  previewLoading = false;
  $('preview-open').disabled = false;
  $('preview-open').textContent = '▶ Preview';
  toast('preview failed — try Export instead');
});
previewAudio.addEventListener('play', () => {
  if (playing) { playing.pause(); playing = null; }
  $('preview-play').textContent = '⏸';
});
previewAudio.addEventListener('pause', () => { $('preview-play').textContent = '▶'; });
previewAudio.addEventListener('ended', () => { lastHighlighted = null; highlightPlayingLine(); });
previewAudio.addEventListener('timeupdate', () => {
  $('preview-time').textContent = `${formatTime(previewAudio.currentTime)} / ${formatTime(previewAudio.duration)}`;
  if (previewAudio.duration) $('preview-seek').value = previewAudio.currentTime / previewAudio.duration;
  highlightPlayingLine();
});
previewAudio.addEventListener('loadedmetadata', () => {
  $('preview-time').textContent = `${formatTime(previewAudio.currentTime)} / ${formatTime(previewAudio.duration)}`;
});

$('preview-open').addEventListener('click', openPreview);
$('preview-close').addEventListener('click', closePreview);
$('preview-play').addEventListener('click', () => {
  if (previewAudio.paused) previewAudio.play().catch(fail); else previewAudio.pause();
});
$('preview-seek').addEventListener('input', () => {
  if (previewAudio.duration) previewAudio.currentTime = +$('preview-seek').value * previewAudio.duration;
});

/* ---- wiring ------------------------------------------------------------ */

$('project-select').addEventListener('change', async (event) => {
  PID = event.target.value;
  apply(await api(stateUrl()).catch(fail));
});
$('new-project').addEventListener('click', () => {
  const title = prompt('Project name', 'Untitled');
  if (title === null) return;
  post('/api/projects', { title }).then(apply).catch(fail);
});
$('delete-project').addEventListener('click', () => {
  if (!STATE) return;
  const n = STATE.project.chunks.length;
  if (confirm(`Delete "${STATE.project.title}"${n ? ` and its ${n} line(s)` : ''}? Takes shared with other projects are kept.`)) {
    del(P()).then(apply).catch(fail);
  }
});
$('title').addEventListener('change', (e) => post(P(), { title: e.target.value }).then(apply).catch(fail));
$('title').addEventListener('keydown', (e) => { if (e.key === 'Enter') e.target.blur(); });

$('voice-select').addEventListener('change', (event) => {
  if (event.target.value === '__manage__') {
    event.target.value = STATE?.project.voice_id || '';
    openOverlay('voices-drawer');
    return;
  }
  post(P(), { voice_id: event.target.value }).then(apply).catch(fail);
});

$('split').addEventListener('click', () => {
  const text = $('script').value.trim();
  if (!text) { $('script').focus(); return; }
  if (STATE?.project.chunks.length &&
      !confirm('Replace the current lines? Takes for identical lines are kept.')) return;
  post(P('/split'), { text, max_chars: +$('max-chars').value })
    .then((s) => { apply(s); closeOverlays(); $('script').value = ''; })
    .catch(fail);
});
$('add-chunk').addEventListener('click', () => post(P('/chunks'), { text: '' }).then(apply).catch(fail));
$('generate').addEventListener('click', () => post(P('/generate')).then(apply).catch(fail));
$('stop').addEventListener('click', () => post(`/api/queue/clear?p=${PID}`).then(apply).catch(fail));
$('tidy').addEventListener('click', () => post(`/api/tidy?p=${PID}`).then(apply).catch(fail));
$('export').addEventListener('click', async () => {
  const response = await fetch(P('/export.wav'));
  if (!response.ok) { toast((await response.json()).detail); return; }
  const url = URL.createObjectURL(await response.blob());
  const link = Object.assign(document.createElement('a'), {
    href: url, download: `${STATE.project.title || 'mynah'}.wav`,
  });
  link.click();
  URL.revokeObjectURL(url);
});
for (const key of ['temperature', 'top_p', 'top_k', 'repetition_penalty']) {
  $(`p-${key}`).addEventListener('change', (e) =>
    post(P(), { params: { [key]: +e.target.value } }).then(apply).catch(fail));
}

/* Poll faster while something is actually happening. */
async function poll() {
  try {
    apply(await api(stateUrl()));
  } catch (error) {
    if (PID && /no such project/i.test(error.message)) PID = '';
  }
  const busy = STATE?.queue.current || STATE?.queue.pending
    || STATE?.engine.state === 'loading'
    || STATE?.voices.some(v => v.status === 'compiling');
  setTimeout(poll, busy ? 700 : 2500);
}
poll();
