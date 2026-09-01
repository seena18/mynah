/* mynah — one page, one polled state object.
 *
 * The server is the only source of truth. This file never keeps its own copy of
 * the project; it renders whatever /api/state last returned. The one concession
 * is that a textarea the user is typing in is left alone until it loses focus,
 * so a poll landing mid-sentence cannot eat the caret.
 */

const $ = (id) => document.getElementById(id);
let STATE = null;
let rows = new Map();          // chunk id -> row element
let recorder = null;

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

function apply(state) {
  if (!state) return;
  STATE = state;
  renderEngine(state.engine, state.queue);
  renderVoices(state.voices, state.project.voice_id);
  renderChunks(state.project);
  renderParams(state.project.params);
  if (document.activeElement !== $('title')) $('title').value = state.project.title;
  $('log').textContent = state.log.join('\n');
}

/* ---- header ------------------------------------------------------------ */

function renderEngine(engine, queue) {
  const pill = $('engine');
  const label = { cold: 'idle', loading: 'loading model…', ready: 'ready', error: 'error' };
  pill.textContent = `${label[engine.state] || engine.state} · ${engine.device}`;
  pill.className = 'pill ' + (engine.state === 'ready' ? 'ready'
    : engine.state === 'error' ? 'error' : 'loading');
  pill.title = engine.error || '';
  $('queue').textContent = queue.pending || queue.current
    ? `${queue.pending} queued${queue.current ? ', 1 rendering' : ''}` : '';
  $('stop').disabled = !queue.pending;
}

/* ---- voices ------------------------------------------------------------ */

function renderVoices(voices, selected) {
  const select = $('voice-select');
  const signature = voices.map(v => `${v.id}:${v.status}`).join('|') + `#${selected}`;
  if (select.dataset.signature !== signature) {
    select.dataset.signature = signature;
    select.innerHTML = '<option value="">— no voice —</option>';
    for (const voice of voices) {
      const option = document.createElement('option');
      const suffix = voice.status === 'ready' ? `${voice.seconds}s`
        : voice.status === 'error' ? 'failed' : 'compiling…';
      option.value = voice.id;
      option.textContent = `${voice.name} · ${suffix}`;
      option.disabled = voice.status !== 'ready';
      select.append(option);
    }
    select.value = selected || '';
  }
  const current = voices.find(v => v.id === selected);
  const failed = voices.find(v => v.status === 'error');
  $('voice-hint').textContent = failed ? failed.error
    : current ? 'ready' : 'needs more than 5 seconds of clear speech';
  $('voice-hint').style.color = failed ? 'var(--bad)' : '';
  const preview = $('voice-preview');
  if (current && preview.dataset.voice !== current.id) {
    preview.dataset.voice = current.id;
    preview.src = `/api/voices/${current.id}/reference.wav`;
    preview.hidden = false;
  } else if (!current) {
    preview.hidden = true;
  }
}

/* ---- chunks ------------------------------------------------------------ */

function buildRow(chunk) {
  const li = document.createElement('li');
  li.className = 'chunk';
  li.innerHTML = `
    <span class="n"><span class="dot"></span><br>${''}</span>
    <textarea rows="2" spellcheck="false"></textarea>
    <span class="side">
      <span class="row">
        <button class="icon play" title="Play take">▶</button>
        <button class="icon regen" title="Generate this chunk">↻</button>
        <button class="icon drop" title="Delete chunk">✕</button>
      </span>
      <span class="row"><input class="pause" type="number" step="0.1" min="0" max="10" title="Pause after (seconds)"></span>
    </span>
    <span class="err" hidden></span>`;

  const text = li.querySelector('textarea');
  text.addEventListener('change', () => put(`/api/chunks/${chunk.id}`, { text: text.value }).then(apply));
  li.querySelector('.pause').addEventListener('change', (event) =>
    put(`/api/chunks/${chunk.id}`, { pause_after: +event.target.value }).then(apply));
  li.querySelector('.regen').addEventListener('click', () =>
    post(`/api/chunks/${chunk.id}/generate`).then(apply).catch(alert));
  li.querySelector('.drop').addEventListener('click', () =>
    api(`/api/chunks/${chunk.id}`, { method: 'DELETE' }).then(apply));
  li.querySelector('.play').addEventListener('click', () => {
    const audio = new Audio(`/api/takes/${chunk.id}.wav?v=${li.dataset.fingerprint}`);
    audio.play();
  });
  return li;
}

function renderChunks(project) {
  const list = $('chunks');
  const seen = new Set();
  project.chunks.forEach((chunk, index) => {
    seen.add(chunk.id);
    let li = rows.get(chunk.id);
    if (!li) { li = buildRow(chunk); rows.set(chunk.id, li); }
    if (li.parentNode !== list || list.children[index] !== li) {
      list.insertBefore(li, list.children[index] || null);
    }
    li.className = `chunk s-${chunk.status}`;
    li.dataset.fingerprint = chunk.fingerprint;
    li.querySelector('.n').innerHTML = `<span class="dot"></span><br>${index + 1}`;
    li.querySelector('.n').title = chunk.status;

    const text = li.querySelector('textarea');
    if (document.activeElement !== text && text.value !== chunk.text) text.value = chunk.text;
    const pause = li.querySelector('.pause');
    if (document.activeElement !== pause) pause.value = chunk.pause_after;

    li.querySelector('.play').disabled = chunk.status !== 'ready';
    li.querySelector('.regen').disabled = ['queued', 'rendering', 'empty'].includes(chunk.status);
    const error = li.querySelector('.err');
    error.hidden = !chunk.error;
    error.textContent = chunk.error || '';
  });
  for (const [id, li] of rows) if (!seen.has(id)) { li.remove(); rows.delete(id); }

  const tally = project.chunks.reduce((acc, c) => (acc[c.status] = (acc[c.status] || 0) + 1, acc), {});
  $('counts').textContent = Object.entries(tally).map(([k, v]) => `${v} ${k}`).join(' · ');
  $('empty').hidden = project.chunks.length > 0;
  $('generate').disabled = !(tally.stale > 0);
  $('export').disabled = !(tally.ready > 0) || !!(tally.stale || tally.queued || tally.rendering);
}

function renderParams(params) {
  for (const [key, value] of Object.entries(params)) {
    const input = $(`p-${key}`);
    if (input && document.activeElement !== input) input.value = value;
  }
}

/* ---- recording --------------------------------------------------------- */

async function startRecording() {
  const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  const chunks = [];
  recorder = new MediaRecorder(stream);
  const started = Date.now();
  const timer = $('rec-timer');
  timer.hidden = false;
  const tick = setInterval(() => {
    const seconds = (Date.now() - started) / 1000;
    timer.textContent = `${seconds.toFixed(1)}s`;
    // The model rejects a reference under five seconds, so say when it is safe
    // to stop rather than letting the upload fail afterwards.
    timer.style.color = seconds < 5 ? 'var(--bad)' : 'var(--ok)';
  }, 100);

  recorder.ondataavailable = (event) => chunks.push(event.data);
  recorder.onstop = async () => {
    clearInterval(tick);
    timer.hidden = true;
    stream.getTracks().forEach(track => track.stop());
    $('record').classList.remove('recording');
    $('record').textContent = '● Record';
    const blob = new Blob(chunks, { type: recorder.mimeType });
    await sendVoice(new File([blob], 'recording.webm', { type: blob.type }));
    recorder = null;
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
  const response = await fetch(`/api/voices?name=${encodeURIComponent(name)}`,
    { method: 'POST', body: form });
  if (!response.ok) { alert('upload failed'); return; }
  const voice = await response.json();
  // Select it optimistically; the picker keeps it disabled until it compiles.
  await post('/api/project', { voice_id: voice.id }).then(apply);
}

/* ---- wiring ------------------------------------------------------------ */

$('record').addEventListener('click', () => {
  if (recorder) recorder.stop(); else startRecording().catch(e => alert(`microphone: ${e.message}`));
});
$('upload').addEventListener('click', () => $('file').click());
$('file').addEventListener('change', (event) => {
  if (event.target.files[0]) sendVoice(event.target.files[0]);
  event.target.value = '';
});
$('delete-voice').addEventListener('click', () => {
  const id = $('voice-select').value;
  if (id && confirm('Delete this voice?')) api(`/api/voices/${id}`, { method: 'DELETE' }).then(apply);
});
$('voice-select').addEventListener('change', (event) =>
  post('/api/project', { voice_id: event.target.value }).then(apply));
$('title').addEventListener('change', (event) =>
  post('/api/project', { title: event.target.value }).then(apply));

$('split').addEventListener('click', () => {
  const text = $('script').value.trim();
  if (!text) return;
  if (STATE?.project.chunks.length &&
      !confirm('Replace the current chunks? Takes for identical lines are kept.')) return;
  post('/api/script/split', { text, max_chars: +$('max-chars').value }).then(apply).catch(e => alert(e.message));
});
$('add-chunk').addEventListener('click', () => post('/api/chunks', { text: '' }).then(apply));
$('generate').addEventListener('click', () => post('/api/generate').then(apply));
$('stop').addEventListener('click', () => post('/api/queue/clear').then(apply));
$('tidy').addEventListener('click', () => post('/api/tidy').then(apply));
$('export').addEventListener('click', async () => {
  const response = await fetch('/api/export.wav');
  if (!response.ok) { alert((await response.json()).detail); return; }
  const url = URL.createObjectURL(await response.blob());
  const link = Object.assign(document.createElement('a'), {
    href: url, download: `${STATE.project.title || 'mynah'}.wav`,
  });
  link.click();
  URL.revokeObjectURL(url);
});

for (const key of ['temperature', 'top_p', 'top_k', 'repetition_penalty']) {
  $(`p-${key}`).addEventListener('change', (event) =>
    post('/api/project', { params: { [key]: +event.target.value } }).then(apply));
}

/* Poll faster while something is actually happening. */
async function poll() {
  try { apply(await api('/api/state')); } catch {}
  const busy = STATE?.queue.current || STATE?.queue.pending
    || STATE?.engine.state === 'loading'
    || STATE?.voices.some(v => v.status === 'compiling');
  setTimeout(poll, busy ? 700 : 2500);
}
poll();
