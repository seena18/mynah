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
let stateEpoch = 0;            // discard polls that predate a save or navigation
let navigation = 0;
const edits = new Map();       // input -> unsaved value and its original project
const saving = new Map();      // input -> request currently saving it
let draggingRow = null;        // row currently owned by native drag and drop
let dragStartOrder = [];
let reordering = false;        // keep polls from undoing the optimistic order

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

function bindEdit(input, path, body, send = put) {
  function remember() {
    edits.set(input, { pid: PID, path: path(), body: body(), send });
    stateEpoch++;
    closePreview();
    if (STATE) renderChunks(STATE.project);
  }
  input.addEventListener('input', remember);
  input.addEventListener('change', () => {
    if (!edits.has(input)) remember();
    saveField(input).catch(fail);
  });
}

async function saveField(input) {
  if (saving.has(input)) {
    await saving.get(input);
    return saveField(input);
  }
  const edit = edits.get(input);
  if (!edit) return;
  stateEpoch++;
  const request = edit.send(edit.path, edit.body).then(state => {
    if (edits.get(input) === edit) edits.delete(input);
    stateEpoch++;
    if (PID === edit.pid) apply(state);
  }).finally(() => saving.delete(input));
  saving.set(input, request);
  return request;
}

async function flushEdits(pid = PID) {
  // A user can keep typing during a save. Drain until the latest values have
  // landed, and stop on failure instead of generating the previous script.
  while ([...edits.values()].some(edit => edit.pid === pid)) {
    await Promise.all([...edits].filter(([, edit]) => edit.pid === pid)
      .map(([input]) => saveField(input)));
  }
}

async function generate(chunkId = '') {
  const pid = PID;
  await flushEdits(pid);
  if (pid !== PID) return;
  const state = await post(`/api/projects/${pid}${chunkId ? `/chunks/${chunkId}` : ''}/generate`);
  if (pid === PID) apply(state);
}

function toast(message) {
  // The activity line doubles as a toast: short-lived, non-modal, dismissable.
  $('queue').textContent = message;
  $('queue').classList.add('bad');
  setTimeout(() => $('queue').classList.remove('bad'), 4000);
}

/* ---- apply ------------------------------------------------------------- */

function mixSignature(project) {
  // Everything that can change the stitched bytes or their sequence. Status
  // matters for re-rolls because the fingerprint deliberately stays the same.
  return project.chunks.map(chunk => [
    chunk.id, chunk.fingerprint, chunk.pause_after, chunk.status,
  ]).join('|');
}

function apply(state) {
  if (!state) return;
  if (state.project.id !== STATE?.project.id) {
    stateEpoch++;
    for (const li of rows.values()) li.remove();
    rows.clear();
    if (playing) { playing.pause(); playing = null; }
    closePreview();
  } else if (STATE && mixSignature(state.project) !== mixSignature(STATE.project)) {
    // A preview is one stitched snapshot. Once its inputs change, discard it
    // so playback cannot claim to represent the rows currently on screen.
    closePreview();
  }
  PID = state.project.id;
  STATE = state;
  if (new URLSearchParams(location.search).get('p') !== PID) {
    history.replaceState(null, '', `?p=${PID}`);
  }
  renderEngine(state.engine, state.queue);
  renderProjects(state.projects, state.project.id);
  renderVoicePicker(state.voices, state.project.voice_id);
  renderVoiceCards(state.voices, state.project.voice_id);
  renderChunks(state.project);
  renderParams(state.project.params);
  if (document.activeElement !== $('title') && !edits.has($('title'))) $('title').value = state.project.title;
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

function chunkOrder() {
  return [...$('chunks').children].map(row => row.dataset.chunkId);
}

function refreshChunkNumbers() {
  [...$('chunks').children].forEach((row, index) => {
    row.querySelector('.idx').textContent = index + 1;
    row.querySelector('.drag').setAttribute('aria-label', `Reorder line ${index + 1}`);
  });
}

async function saveChunkOrder(ids) {
  const pid = PID;
  reordering = true;
  stateEpoch++;
  closePreview();
  try {
    await flushEdits(pid);
    if (pid !== PID) return;
    const state = await put(`/api/projects/${pid}/chunks/order`, { ids });
    if (pid === PID) apply(state);
  } catch (error) {
    if (pid === PID) {
      try { apply(await api(`/api/state?p=${encodeURIComponent(pid)}`)); } catch {}
    }
    throw error;
  } finally {
    reordering = false;
  }
}

function finishChunkDrag() {
  if (!draggingRow) return;
  const before = dragStartOrder;
  const after = chunkOrder();
  draggingRow.classList.remove('dragging');
  draggingRow = null;
  dragStartOrder = [];
  if (before.join('|') !== after.join('|')) saveChunkOrder(after).catch(fail);
}

$('chunks').addEventListener('dragover', (event) => {
  if (!draggingRow) return;
  event.preventDefault();
  event.dataTransfer.dropEffect = 'move';
  const otherRows = [...$('chunks').children].filter(row => row !== draggingRow);
  const before = otherRows.find(row => {
    const box = row.getBoundingClientRect();
    return event.clientY < box.top + box.height / 2;
  });
  $('chunks').insertBefore(draggingRow, before || null);
  refreshChunkNumbers();
});
$('chunks').addEventListener('drop', (event) => {
  if (draggingRow) event.preventDefault();
});

function buildRow(chunk) {
  const li = document.createElement('li');
  li.className = 'chunk';
  li.dataset.chunkId = chunk.id;
  li.innerHTML = `
    <span class="n">
      <button class="drag" draggable="true" title="Drag to reorder" aria-label="Reorder line"></button>
      <span class="marker"><span class="dot"></span><span class="idx"></span></span>
    </span>
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
  bindEdit(text, () => P(`/chunks/${chunk.id}`), () => ({ text: text.value }));
  const pause = li.querySelector('.pause');
  bindEdit(pause, () => P(`/chunks/${chunk.id}`), () => ({ pause_after: +pause.value }));
  const handle = li.querySelector('.drag');
  handle.addEventListener('dragstart', (event) => {
    draggingRow = li;
    dragStartOrder = chunkOrder();
    event.dataTransfer.effectAllowed = 'move';
    event.dataTransfer.setData('text/plain', chunk.id);
    closePreview();
    requestAnimationFrame(() => { if (draggingRow === li) li.classList.add('dragging'); });
  });
  handle.addEventListener('dragend', finishChunkDrag);
  handle.addEventListener('keydown', (event) => {
    if (event.key !== 'ArrowUp' && event.key !== 'ArrowDown') return;
    event.preventDefault();
    const sibling = event.key === 'ArrowUp' ? li.previousElementSibling : li.nextElementSibling;
    if (!sibling) return;
    if (event.key === 'ArrowUp') $('chunks').insertBefore(li, sibling);
    else $('chunks').insertBefore(sibling, li);
    refreshChunkNumbers();
    saveChunkOrder(chunkOrder()).catch(fail);
  });
  li.querySelector('.regen').addEventListener('click', () =>
    generate(chunk.id).catch(fail));
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
    // Also bypass immutable responses cached by older releases of the app.
    const audio = new Audio(P(`/takes/${chunk.id}.wav?v=${li.dataset.fingerprint}&play=${crypto.randomUUID()}`));
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
    if (!draggingRow && !reordering && (li.parentNode !== list || list.children[index] !== li)) {
      list.insertBefore(li, list.children[index] || null);
    }
    // now-playing is owned by highlightPlayingLine(), which only touches the
    // DOM when the playing segment actually changes — an unconditional reset
    // here would erase it on every poll tick and never get a chance to
    // reapply, since from the highlighter's perspective nothing changed.
    li.className = `chunk s-${chunk.status}${li.classList.contains('now-playing') ? ' now-playing' : ''}${li === draggingRow ? ' dragging' : ''}`;
    li.dataset.fingerprint = chunk.fingerprint;
    li.querySelector('.idx').textContent = index + 1;
    li.querySelector('.n').title = chunk.status;
    const text = li.querySelector('textarea');
    if (document.activeElement !== text && !edits.has(text) && text.value !== chunk.text) { text.value = chunk.text; autosize(text); }
    else if (fresh) autosize(text);
    const pause = li.querySelector('.pause');
    if (document.activeElement !== pause && !edits.has(pause)) pause.value = chunk.pause_after;
    li.querySelector('.play').disabled = chunk.status !== 'ready';
    li.querySelector('.regen').disabled = ['queued', 'rendering', 'empty', 'no-voice'].includes(chunk.status);
    const error = li.querySelector('.err');
    error.hidden = !chunk.error;
    error.textContent = chunk.error || '';
  });
  for (const [id, li] of rows) if (!seen.has(id)) { li.remove(); rows.delete(id); }
  refreshChunkNumbers();

  const tally = project.chunks.reduce((acc, c) => (acc[c.status] = (acc[c.status] || 0) + 1, acc), {});
  const order = ['ready', 'stale', 'queued', 'rendering', 'no-voice', 'empty'];
  $('counts').textContent = order.filter(k => tally[k]).map(k => `${tally[k]} ${k}`).join(' · ');
  $('empty').hidden = project.chunks.length > 0;
  const dirty = [...edits.values()].some(edit => edit.pid === project.id);
  $('generate').disabled = !(tally.stale > 0 || (dirty && project.voice_id));
  const count = $('stale-count');
  count.hidden = !(tally.stale > 0);
  count.textContent = tally.stale || '';
  const canRender = (tally.ready > 0) && !(dirty || tally.stale || tally.queued || tally.rendering || tally['no-voice']);
  $('export').disabled = !canRender;
  // Not disabled outright while a preview is already loading — openPreview()
  // owns that state until its own canplay/error fires, or the next poll
  // (which runs every 700ms-2.5s) would flip the button back on mid-fetch.
  if (!previewLoading) $('preview-open').disabled = !canRender;
}

function renderParams(params) {
  for (const [key, value] of Object.entries(params)) {
    const input = $(`p-${key}`);
    if (input && document.activeElement !== input && !edits.has(input)) input.value = value;
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
    generate().catch(fail);
  }
});
$('open-voices').addEventListener('click', () => openOverlay('voices-drawer'));
$('open-settings').addEventListener('click', () => openOverlay('settings-drawer'));
$('paste-script').addEventListener('click', () => openOverlay('script-modal'));
document.querySelectorAll('[data-open-script]').forEach(b => b.addEventListener('click', () => openOverlay('script-modal')));
document.querySelectorAll('[data-add-chunk]').forEach(b => b.addEventListener('click', () => post(P('/chunks'), { text: '' }).then(apply).catch(fail)));
$('activity-toggle').addEventListener('click', () => { $('activity').hidden = !$('activity').hidden; });

/* ---- recording --------------------------------------------------------- */

/* A scrolling level meter fed by the microphone itself, so it is obvious the
   input is live and being heard — a silent mic otherwise looks identical to a
   working one until the voice fails to compile. */
function startMeter(stream) {
  const context = new AudioContext();
  const analyser = context.createAnalyser();
  analyser.fftSize = 1024;
  context.createMediaStreamSource(stream).connect(analyser);
  const samples = new Float32Array(analyser.fftSize);
  const canvas = $('rec-meter');
  const history = [];
  let live = true;
  (function tick() {
    if (!live) return;
    analyser.getFloatTimeDomainData(samples);
    let sum = 0;
    for (let i = 0; i < samples.length; i++) sum += samples[i] * samples[i];
    // Root mean square, then a gentle curve so speech fills the meter.
    history.push(Math.min(1, Math.sqrt(sum / samples.length) * 4) ** 0.7);
    if (history.length > 64) history.shift();
    const { pen, width, height } = surface(canvas);
    const bar = 2, gap = 2, slots = Math.max(1, Math.floor(width / (bar + gap)));
    const middle = height / 2;
    pen.fillStyle = ink(canvas.dataset.enough ? '--ok' : '--bad');
    for (let i = 0; i < slots; i++) {
      // Newest on the right, so the trace scrolls the way it is read.
      const value = history[history.length - slots + i] || 0;
      const tall = Math.max(2, value * (height - 3));
      pen.fillRect(i * (bar + gap), middle - tall / 2, bar, tall);
    }
    requestAnimationFrame(tick);
  })();
  return () => { live = false; context.close().catch(() => {}); };
}

async function startRecording() {
  const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  const chunks = [];
  recorder = new MediaRecorder(stream);
  const timer = $('rec-timer');
  const note = $('rec-note');
  const meter = $('rec-meter');
  const stopMeter = startMeter(stream);
  meter.hidden = false;
  $('rec-hint').hidden = true;
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
    meter.dataset.enough = seconds < MIN_RECORD ? '' : 'yes';
  }, 100);
  recorder.ondataavailable = (event) => chunks.push(event.data);
  recorder.onstop = async () => {
    clearInterval(tick);
    stopMeter();
    meter.hidden = true;
    $('rec-hint').hidden = false;
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
let previewPeaks = null;       // normalised amplitude per bucket, from the real samples
let previewUrl = "";           // object URL for the fetched mix, revoked on close
let previewRequest = 0;        // invalidates async work after close/project switch

function ink(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

/* Back the canvas at device resolution: a 190px canvas on a retina screen is
   380 real pixels, and drawing at CSS size makes the bars soft. */
function surface(canvas) {
  const ratio = window.devicePixelRatio || 1;
  const width = canvas.clientWidth, height = canvas.clientHeight;
  if (canvas.width !== Math.round(width * ratio)) {
    canvas.width = Math.round(width * ratio);
    canvas.height = Math.round(height * ratio);
  }
  const pen = canvas.getContext('2d');
  pen.setTransform(ratio, 0, 0, ratio, 0, 0);
  pen.clearRect(0, 0, width, height);
  return { pen, width, height };
}

/* Peak amplitude per bucket, straight from the decoded samples. Strided
   because a 6-second mix is 145k samples and only the envelope is drawn. */
function peaksFrom(buffer, buckets) {
  const data = buffer.getChannelData(0);
  const per = Math.max(1, Math.floor(data.length / buckets));
  const peaks = new Float32Array(buckets);
  let loudest = 0;
  for (let i = 0; i < buckets; i++) {
    let top = 0;
    for (let j = i * per; j < (i + 1) * per && j < data.length; j += 3) {
      const v = data[j] < 0 ? -data[j] : data[j];
      if (v > top) top = v;
    }
    peaks[i] = top;
    if (top > loudest) loudest = top;
  }
  if (loudest > 0) for (let i = 0; i < buckets; i++) peaks[i] /= loudest;
  return peaks;
}

/* Mirrored bars, played portion in the accent colour. */
function drawWave(canvas, peaks, progress) {
  const { pen, width, height } = surface(canvas);
  const bar = 2, gap = 2, count = Math.max(1, Math.floor(width / (bar + gap)));
  const middle = height / 2;
  const played = ink('--accent'), rest = ink('--line-strong');
  for (let i = 0; i < count; i++) {
    const value = peaks ? peaks[Math.floor(i / count * peaks.length)] || 0 : 0;
    const tall = Math.max(2, value * (height - 3));
    pen.fillStyle = (i + 0.5) / count <= progress ? played : rest;
    pen.fillRect(i * (bar + gap), middle - tall / 2, bar, tall);
  }
}

function paintPreview() {
  const total = previewAudio.duration;
  const progress = total ? previewAudio.currentTime / total : 0;
  drawWave($('preview-wave'), previewPeaks, progress);
}

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
  const request = ++previewRequest;
  const pid = PID;
  const current = () => request === previewRequest && pid === PID;
  previewLoading = true;
  $('preview-open').disabled = true;
  $('preview-open').textContent = 'Loading…';
  // Stitched playback and chunk playback are still only one audible thing.
  if (playing) { playing.pause(); playing = null; }
  try {
    const timeline = await api(`/api/projects/${pid}/timeline`);
    if (!current()) return;
    timelineSegments = timeline.segments;
  } catch (error) {
    if (!current()) return;
    timelineSegments = [];     // still play — just no line highlight
  }
  // Fetch the mix once and use those same bytes for both the waveform and
  // playback. Letting <audio> fetch it separately would download it twice —
  // and /preview.wav re-stitches per request, so the two copies need not even
  // be identical.
  try {
    const response = await fetch(`/api/projects/${pid}/preview.wav`);
    if (!response.ok) throw new Error('preview failed');
    const bytes = await response.arrayBuffer();
    if (!current()) return;
    const decoder = new AudioContext();
    // decodeAudioData detaches the buffer it is given, so hand it a copy.
    let decoded;
    try { decoded = await decoder.decodeAudioData(bytes.slice(0)); }
    finally { await decoder.close(); }
    if (!current()) return;
    previewPeaks = peaksFrom(decoded, 400);
    if (previewUrl) URL.revokeObjectURL(previewUrl);
    previewUrl = URL.createObjectURL(new Blob([bytes], { type: 'audio/wav' }));
    previewAudio.src = previewUrl;
  } catch (error) {
    if (!current()) return;
    previewPeaks = null;       // still play — just no waveform
    previewAudio.src = `/api/projects/${pid}/preview.wav`;
  }
  previewAudio.load();
}

function closePreview() {
  previewRequest++;
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
  if (previewUrl) { URL.revokeObjectURL(previewUrl); previewUrl = ""; }
  previewPeaks = null;
  drawWave($('preview-wave'), null, 0);
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
  paintPreview();
  highlightPlayingLine();
});
previewAudio.addEventListener('loadedmetadata', () => {
  $('preview-time').textContent = `${formatTime(previewAudio.currentTime)} / ${formatTime(previewAudio.duration)}`;
  paintPreview();
});
previewAudio.addEventListener('seeked', paintPreview);
// timeupdate only fires about four times a second, which reads as a stuttering
// playhead. While playing, repaint on every frame instead.
(function sweep() {
  if (!previewAudio.paused && !$('preview-active').hidden) paintPreview();
  requestAnimationFrame(sweep);
})();

$('preview-open').addEventListener('click', openPreview);
$('preview-close').addEventListener('click', closePreview);
$('preview-play').addEventListener('click', () => {
  if (previewAudio.paused) previewAudio.play().catch(fail); else previewAudio.pause();
});
/* Click or drag anywhere on the waveform to seek. */
function seekTo(event) {
  if (!previewAudio.duration) return;
  const box = $('preview-wave').getBoundingClientRect();
  const at = Math.min(1, Math.max(0, (event.clientX - box.left) / box.width));
  previewAudio.currentTime = at * previewAudio.duration;
  paintPreview();
}
$('preview-wave').addEventListener('pointerdown', (event) => {
  $('preview-wave').setPointerCapture(event.pointerId);
  seekTo(event);
});
$('preview-wave').addEventListener('pointermove', (event) => {
  if (event.buttons) seekTo(event);
});
window.addEventListener('resize', () => {
  if (!$('preview-active').hidden) paintPreview();
});

/* ---- wiring ------------------------------------------------------------ */

$('project-select').addEventListener('change', async (event) => {
  const pid = event.target.value;
  const request = ++navigation;
  closePreview();
  if (playing) { playing.pause(); playing = null; }
  try {
    await flushEdits();
    const state = await api(`/api/state?p=${encodeURIComponent(pid)}`);
    if (request === navigation) apply(state);
  } catch (error) {
    if (request === navigation) $('project-select').value = PID;
    fail(error);
  }
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
bindEdit($('title'), () => P(), () => ({ title: $('title').value }), post);
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
$('generate').addEventListener('click', () => generate().catch(fail));
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
  const input = $(`p-${key}`);
  bindEdit(input, () => P(), () => ({ params: { [key]: +input.value } }), post);
}

/* Poll faster while something is actually happening. */
async function poll() {
  const epoch = stateEpoch;
  const pid = PID;
  try {
    const state = await api(stateUrl());
    if (epoch === stateEpoch && pid === PID) apply(state);
  } catch (error) {
    if (pid === PID && PID && /no such project/i.test(error.message)) PID = '';
  }
  const busy = STATE?.queue.current || STATE?.queue.pending
    || STATE?.engine.state === 'loading'
    || STATE?.voices.some(v => v.status === 'compiling');
  setTimeout(poll, busy ? 700 : 2500);
}
poll();
