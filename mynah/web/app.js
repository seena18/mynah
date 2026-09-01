/* mynah — one page, one polled state object.
 *
 * The server is the only source of truth. This file never keeps its own copy of
 * the project; it renders whatever /api/state last returned. The one concession
 * is that a textarea the user is typing in is left alone until it loses focus,
 * so a poll landing mid-sentence cannot eat the caret.
 *
 * Which project is shown lives in the URL (?p=<id>), so a reload — or a second
 * tab — lands on the same one. Empty means "whichever was touched last".
 */

const $ = (id) => document.getElementById(id);
let STATE = null;
let PID = new URLSearchParams(location.search).get('p') || '';
let rows = new Map();          // chunk id -> row element
let recorder = null;
let playing = null;            // the one <audio> allowed to be audible

/* The model refuses a reference under 5 s of audio. The browser drops a few
   hundred ms between start() and the first captured sample, so a timer that
   reads 5.0 can be 4.3 s of audio — which is exactly the failure a user hits.
   Six seconds on the clock keeps a margin the server has never rejected. */
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

function apply(state) {
  if (!state) return;
  STATE = state;
  if (state.project.id !== PID) {
    // The server answered with a different project than the page was on —
    // first load, a new project, or the one we were on was deleted. Adopt it.
    PID = state.project.id;
    for (const li of rows.values()) li.remove();
    rows.clear();
    if (playing) { playing.pause(); playing = null; }
  }
  // Keep the address bar honest on every path, not only when PID changed
  // here: the picker sets PID before fetching, so a switch made through it
  // used to leave the URL pointing at the previous project.
  if (new URLSearchParams(location.search).get('p') !== PID) {
    history.replaceState(null, '', `?p=${PID}`);
  }
  renderEngine(state.engine, state.queue);
  renderProjects(state.projects, state.project.id);
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

function renderProjects(projects, currentId) {
  const select = $('project-select');
  const signature = projects.map(p => `${p.id}:${p.title}:${p.chunks}`).join('|') + `#${currentId}`;
  if (select.dataset.signature === signature) return;
  select.dataset.signature = signature;
  select.innerHTML = '';
  for (const project of projects) {
    const option = document.createElement('option');
    option.value = project.id;
    const ready = project.counts.ready || 0;
    option.textContent = `${project.title} · ${ready}/${project.chunks}`;
    select.append(option);
  }
  select.value = currentId;
  $('delete-project').disabled = projects.length < 2;
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
      // Not disabled: a voice is selected the moment it is uploaded, and a
      // disabled option cannot show as selected, which made the picker read
      // "no voice" while its own voice was compiling.
      select.append(option);
    }
    select.value = selected || '';
  }
  const current = voices.find(v => v.id === selected);
  // A failed voice is reported by name, because after a failure the server
  // has already reselected the previous voice — the user needs to see why
  // the new one is not the one in the picker. It stays until deleted with ✕.
  const failed = voices.find(v => v.status === 'error');
  $('voice-hint').textContent = failed ? `${failed.name}: ${failed.error}`
    : !current ? 'needs more than 5 seconds of clear speech'
    : current.status === 'ready' ? `ready · ${current.seconds}s reference`
    : 'compiling…';
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
    <span class="n"><span class="dot"></span><br></span>
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
  text.addEventListener('change', () => put(P(`/chunks/${chunk.id}`), { text: text.value }).then(apply));
  li.querySelector('.pause').addEventListener('change', (event) =>
    put(P(`/chunks/${chunk.id}`), { pause_after: +event.target.value }).then(apply));
  li.querySelector('.regen').addEventListener('click', () =>
    post(P(`/chunks/${chunk.id}/generate`)).then(apply).catch(alert));
  li.querySelector('.drop').addEventListener('click', () =>
    api(P(`/chunks/${chunk.id}`), { method: 'DELETE' }).then(apply));
  li.querySelector('.play').addEventListener('click', () => {
    // One take at a time. A second click on the same row stops it; a click
    // on another row swaps to it. Without this, every click layered a new
    // playback over the last.
    if (playing) {
      const same = playing.dataset.chunk === chunk.id;
      playing.pause(); playing = null;
      if (same) return;
    }
    const audio = new Audio(P(`/takes/${chunk.id}.wav?v=${li.dataset.fingerprint}`));
    audio.dataset.chunk = chunk.id;
    audio.onended = () => { if (playing === audio) playing = null; };
    playing = audio;
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
  const timer = $('rec-timer');
  const note = $('rec-note');
  note.hidden = true;
  timer.hidden = false;
  timer.textContent = '0.0s';
  timer.style.color = 'var(--bad)';
  // Clock from the moment capture actually begins, not from the click.
  let started = 0;
  let seconds = 0;
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
      // Do not upload a clip that will be refused; say so here instead of
      // leaving a failed voice in the list.
      note.textContent = `Recording was ${seconds.toFixed(1)}s — keep going past `
        + `${MIN_RECORD}s so the model gets its 5s of speech. Nothing was saved.`;
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
  const response = await fetch(`/api/voices?name=${encodeURIComponent(name)}&p=${PID}`,
    { method: 'POST', body: form });
  if (!response.ok) { alert('upload failed'); return; }
  // The server selects the new voice itself, and puts the previous one back
  // if compilation fails — so a bad upload never leaves the project pointing
  // at a voice that does not exist.
  apply(await response.json());
}

/* ---- wiring ------------------------------------------------------------ */

$('project-select').addEventListener('change', async (event) => {
  PID = event.target.value;
  apply(await api(stateUrl()));
});
$('new-project').addEventListener('click', () => {
  const title = prompt('Project name', 'Untitled');
  if (title === null) return;
  post('/api/projects', { title }).then(apply);
});
$('delete-project').addEventListener('click', () => {
  if (!STATE) return;
  const n = STATE.project.chunks.length;
  if (confirm(`Delete "${STATE.project.title}"${n ? ` and its ${n} chunk(s)` : ''}? `
              + 'Takes shared with other projects are kept.')) {
    api(P(), { method: 'DELETE' }).then(apply);
  }
});
$('title').addEventListener('change', (event) => post(P(), { title: event.target.value }).then(apply));

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
  if (id && confirm('Delete this voice? Any project using it will need another.')) {
    api(`/api/voices/${id}?p=${PID}`, { method: 'DELETE' }).then(apply);
  }
});
$('voice-select').addEventListener('change', (event) =>
  post(P(), { voice_id: event.target.value }).then(apply));

$('split').addEventListener('click', () => {
  const text = $('script').value.trim();
  if (!text) return;
  if (STATE?.project.chunks.length &&
      !confirm('Replace the current chunks? Takes for identical lines are kept.')) return;
  post(P('/split'), { text, max_chars: +$('max-chars').value }).then(apply).catch(e => alert(e.message));
});
$('add-chunk').addEventListener('click', () => post(P('/chunks'), { text: '' }).then(apply));
$('generate').addEventListener('click', () => post(P('/generate')).then(apply));
$('stop').addEventListener('click', () => post(`/api/queue/clear?p=${PID}`).then(apply));
$('tidy').addEventListener('click', () => post(`/api/tidy?p=${PID}`).then(apply));
$('export').addEventListener('click', async () => {
  const response = await fetch(P('/export.wav'));
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
    post(P(), { params: { [key]: +event.target.value } }).then(apply));
}

/* Poll faster while something is actually happening. */
async function poll() {
  try {
    apply(await api(stateUrl()));
  } catch (error) {
    // A stale ?p= (project deleted elsewhere) must not strand the page.
    if (PID && /no such project/i.test(error.message)) { PID = ''; }
  }
  const busy = STATE?.queue.current || STATE?.queue.pending
    || STATE?.engine.state === 'loading'
    || STATE?.voices.some(v => v.status === 'compiling');
  setTimeout(poll, busy ? 700 : 2500);
}
poll();
