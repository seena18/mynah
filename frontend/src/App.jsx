import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import { api, del, post, put } from './api.js';
import { drawWave, formatTime, ink, peaksFrom, surface } from './audio.js';
import {
  ChunkRow,
  ScriptModal,
  SettingsDrawer,
  Toolbar,
  TopBar,
  VoicesDrawer,
} from './components.jsx';

const MIN_RECORD = 6;
const INITIAL_PID = new URLSearchParams(window.location.search).get('p') || '';

function mixSignature(project) {
  return project.chunks.map((chunk) => [
    chunk.id,
    chunk.fingerprint,
    chunk.pause_after,
    chunk.status,
  ].join(':')).join('|');
}

function useLatest(value) {
  const ref = useRef(value);
  ref.current = value;
  return ref;
}

function App() {
  const [state, setState] = useState(null);
  const stateRef = useLatest(state);
  const pidRef = useRef(INITIAL_PID);
  const [draftVersion, setDraftVersion] = useState(0);
  const draftsRef = useRef(new Map());
  const savingRef = useRef(new Map());
  const epochRef = useRef(0);
  const navigationRef = useRef(0);
  const closePreviewRef = useRef(() => {});
  const [overlay, setOverlay] = useState(null);
  const [activityOpen, setActivityOpen] = useState(false);
  const [notice, setNotice] = useState('');
  const noticeTimer = useRef(null);
  const playingChunk = useRef(null);
  const [order, setOrder] = useState([]);
  const orderRef = useLatest(order);
  const dragRef = useRef(null);
  const [draggingId, setDraggingId] = useState(null);

  const previewAudioRef = useRef(null);
  const previewWaveRef = useRef(null);
  const previewLoadingRef = useRef(false);
  const previewRequestRef = useRef(0);
  const previewUrlRef = useRef('');
  const previewPeaksRef = useRef(null);
  const timelineRef = useRef([]);
  const [previewOpen, setPreviewOpen] = useState(false);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewPlaying, setPreviewPlaying] = useState(false);
  const [previewTime, setPreviewTime] = useState('0:00 / 0:00');
  const [playingId, setPlayingId] = useState(null);

  const [recording, setRecording] = useState(false);
  const [recordSeconds, setRecordSeconds] = useState(0);
  const recordSecondsRef = useRef(0);
  const [recordNote, setRecordNote] = useState('');
  const recorderRef = useRef(null);
  const meterRef = useRef(null);
  const fileRef = useRef(null);

  const path = useCallback((suffix = '') => `/api/projects/${pidRef.current}${suffix}`, []);

  const toast = useCallback((message) => {
    setNotice(message);
    window.clearTimeout(noticeTimer.current);
    noticeTimer.current = window.setTimeout(() => setNotice(''), 4000);
  }, []);

  const stopChunkAudio = useCallback(() => {
    if (playingChunk.current) {
      playingChunk.current.pause();
      playingChunk.current = null;
    }
  }, []);

  const closePreview = useCallback(() => {
    previewRequestRef.current += 1;
    previewLoadingRef.current = false;
    setPreviewLoading(false);
    setPreviewOpen(false);
    setPreviewPlaying(false);
    setPlayingId(null);
    const audio = previewAudioRef.current;
    if (audio) {
      audio.pause();
      audio.removeAttribute('src');
      audio.load();
    }
    timelineRef.current = [];
    window.timelineSegments = timelineRef.current;
    previewPeaksRef.current = null;
    if (previewUrlRef.current) {
      URL.revokeObjectURL(previewUrlRef.current);
      previewUrlRef.current = '';
    }
    drawWave(previewWaveRef.current, null, 0);
    setPreviewTime('0:00 / 0:00');
  }, []);
  closePreviewRef.current = closePreview;

  const applyState = useCallback((next) => {
    if (!next) return;
    const previous = stateRef.current;
    if (previous && (
      previous.project.id !== next.project.id
      || mixSignature(previous.project) !== mixSignature(next.project)
    )) {
      closePreviewRef.current();
      if (previous.project.id !== next.project.id) stopChunkAudio();
    }
    pidRef.current = next.project.id;
    if (new URLSearchParams(window.location.search).get('p') !== next.project.id) {
      window.history.replaceState(null, '', `?p=${next.project.id}`);
    }
    stateRef.current = next;
    window.STATE = next;
    setState(next);
  }, [stateRef, stopChunkAudio]);

  const fail = useCallback((error) => {
    console.error(error);
    toast(error?.message || String(error));
  }, [toast]);

  const rememberDraft = useCallback((key, value, makeRequest) => {
    draftsRef.current.set(key, {
      pid: pidRef.current,
      value,
      makeRequest,
    });
    epochRef.current += 1;
    closePreviewRef.current();
    setDraftVersion((version) => version + 1);
  }, []);

  const saveField = useCallback(async function save(key) {
    if (savingRef.current.has(key)) {
      await savingRef.current.get(key);
      return save(key);
    }
    const draft = draftsRef.current.get(key);
    if (!draft) return;
    epochRef.current += 1;
    const request = draft.makeRequest(draft.pid, draft.value)
      .then((next) => {
        if (draftsRef.current.get(key) === draft) {
          draftsRef.current.delete(key);
          setDraftVersion((version) => version + 1);
        }
        epochRef.current += 1;
        if (pidRef.current === draft.pid) applyState(next);
      })
      .finally(() => savingRef.current.delete(key));
    savingRef.current.set(key, request);
    return request;
  }, [applyState]);

  const flushEdits = useCallback(async (pid = pidRef.current) => {
    while ([...draftsRef.current.values()].some((draft) => draft.pid === pid)) {
      await Promise.all([...draftsRef.current]
        .filter(([, draft]) => draft.pid === pid)
        .map(([key]) => saveField(key)));
    }
  }, [saveField]);

  const generate = useCallback(async (chunkId = '') => {
    const pid = pidRef.current;
    await flushEdits(pid);
    if (pid !== pidRef.current) return;
    applyState(await post(`/api/projects/${pid}${chunkId ? `/chunks/${chunkId}` : ''}/generate`));
  }, [applyState, flushEdits]);

  useEffect(() => {
    let active = true;
    let timer;
    async function poll() {
      const epoch = epochRef.current;
      const pid = pidRef.current;
      try {
        const next = await api(pid ? `/api/state?p=${encodeURIComponent(pid)}` : '/api/state');
        if (active && epoch === epochRef.current && pid === pidRef.current) applyState(next);
      } catch (error) {
        if (active && pid === pidRef.current && pid && /no such project/i.test(error.message)) {
          pidRef.current = '';
        }
      }
      if (!active) return;
      const latest = stateRef.current;
      const busy = latest?.queue.current || latest?.queue.pending
        || latest?.engine.state === 'loading'
        || latest?.voices.some((voice) => voice.status === 'compiling');
      timer = window.setTimeout(poll, busy ? 700 : 2500);
    }
    poll();
    return () => {
      active = false;
      window.clearTimeout(timer);
    };
  }, [applyState, stateRef]);

  useEffect(() => {
    const chunks = state?.project.chunks || [];
    if (!dragRef.current) setOrder(chunks.map((chunk) => chunk.id));
  }, [state?.project.id, state?.project.chunks]);

  useEffect(() => {
    window.STATE = state;
    window.edits = draftsRef.current;
    window.timelineSegments = timelineRef.current;
    window.chunkOrder = () => [...document.querySelectorAll('#chunks .chunk')]
      .map((row) => row.dataset.chunkId);
  }, [state, draftVersion]);

  useEffect(() => {
    function keyboard(event) {
      if (event.key === 'Escape') {
        setOverlay(null);
        return;
      }
      if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) {
        const button = document.getElementById('generate');
        if (button && !button.disabled) {
          event.preventDefault();
          generate().catch(fail);
        }
      }
    }
    document.addEventListener('keydown', keyboard);
    return () => document.removeEventListener('keydown', keyboard);
  }, [fail, generate]);

  useEffect(() => () => {
    window.clearTimeout(noticeTimer.current);
    stopChunkAudio();
    closePreviewRef.current();
    if (recorderRef.current?.state === 'recording') recorderRef.current.stop();
  }, [stopChunkAudio]);

  const project = state?.project;
  const chunksById = useMemo(() => new Map(
    (project?.chunks || []).map((chunk) => [chunk.id, chunk]),
  ), [project?.chunks]);
  const visibleChunks = order.map((id) => chunksById.get(id)).filter(Boolean);
  for (const chunk of project?.chunks || []) {
    if (!order.includes(chunk.id)) visibleChunks.push(chunk);
  }

  const tally = useMemo(() => (project?.chunks || []).reduce((counts, chunk) => {
    counts[chunk.status] = (counts[chunk.status] || 0) + 1;
    return counts;
  }, {}), [project?.chunks]);
  const dirty = [...draftsRef.current.values()].some((draft) => draft.pid === project?.id);
  const canGenerate = Boolean((tally.stale > 0) || (dirty && project?.voice_id));
  const canRender = Boolean(tally.ready > 0) && !(
    dirty || tally.stale || tally.queued || tally.rendering || tally['no-voice']
  );

  const saveChunkOrder = useCallback(async (ids) => {
    const pid = pidRef.current;
    epochRef.current += 1;
    closePreviewRef.current();
    try {
      await flushEdits(pid);
      if (pid !== pidRef.current) return;
      applyState(await put(`/api/projects/${pid}/chunks/order`, { ids }));
    } catch (error) {
      if (pid === pidRef.current) {
        try {
          applyState(await api(`/api/state?p=${encodeURIComponent(pid)}`));
        } catch {
          // Preserve the original ordering error.
        }
      }
      throw error;
    }
  }, [applyState, flushEdits]);

  const removeDragGhost = useCallback(() => {
    dragRef.current?.ghost?.remove();
    document.getElementById('chunks')?.classList.remove('drag-active');
  }, []);

  const finishDrag = useCallback(() => {
    const drag = dragRef.current;
    removeDragGhost();
    dragRef.current = null;
    setDraggingId(null);
    if (!drag) return;
    const after = orderRef.current;
    if (drag.before.join('|') !== after.join('|')) saveChunkOrder(after).catch(fail);
  }, [fail, orderRef, removeDragGhost, saveChunkOrder]);

  const beginDrag = useCallback((event, chunkId) => {
    const row = event.currentTarget.closest('.chunk');
    const box = row.getBoundingClientRect();
    const ghost = row.cloneNode(true);
    ghost.className = 'chunk drag-ghost';
    ghost.style.width = `${box.width}px`;
    ghost.querySelector('textarea').value = row.querySelector('textarea').value;
    ghost.querySelector('.pause').value = row.querySelector('.pause').value;
    document.body.append(ghost);
    document.getElementById('chunks')?.classList.add('drag-active');
    const offset = {
      x: Math.max(0, event.clientX - box.left),
      y: Math.max(0, event.clientY - box.top),
    };
    dragRef.current = { id: chunkId, before: [...orderRef.current], ghost, offset };
    event.dataTransfer.effectAllowed = 'move';
    event.dataTransfer.setData('text/plain', chunkId);
    const blank = document.createElement('canvas');
    blank.width = 1;
    blank.height = 1;
    event.dataTransfer.setDragImage(blank, 0, 0);
    ghost.style.transform = `translate3d(${event.clientX - offset.x}px, ${event.clientY - offset.y}px, 0)`;
    closePreviewRef.current();
    requestAnimationFrame(() => setDraggingId(chunkId));
  }, [orderRef]);

  const moveDrag = useCallback((event) => {
    const drag = dragRef.current;
    if (!drag) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = 'move';
    drag.ghost.style.transform = `translate3d(${event.clientX - drag.offset.x}px, ${event.clientY - drag.offset.y}px, 0)`;
    const rows = [...document.querySelectorAll('#chunks .chunk')]
      .filter((row) => row.dataset.chunkId !== drag.id);
    const before = rows.find((row) => {
      const box = row.getBoundingClientRect();
      return event.clientY < box.top + box.height / 2;
    });
    const next = rows.map((row) => row.dataset.chunkId);
    const at = before ? next.indexOf(before.dataset.chunkId) : next.length;
    next.splice(at, 0, drag.id);
    if (next.join('|') !== orderRef.current.join('|')) {
      const previousTops = new Map(rows.map((row) => [
        row.dataset.chunkId,
        row.getBoundingClientRect().top,
      ]));
      orderRef.current = next;
      setOrder(next);
      requestAnimationFrame(() => {
        for (const row of document.querySelectorAll('#chunks .chunk')) {
          if (row.dataset.chunkId === drag.id || !previousTops.has(row.dataset.chunkId)) continue;
          const moved = previousTops.get(row.dataset.chunkId) - row.getBoundingClientRect().top;
          if (!moved) continue;
          row.getAnimations().forEach((animation) => animation.cancel());
          row.animate([
            { transform: `translateY(${moved}px)` },
            { transform: 'translateY(0)' },
          ], { duration: 150, easing: 'cubic-bezier(.22,.8,.3,1)' });
        }
      });
    }
  }, [orderRef]);

  const moveByKeyboard = useCallback((event, chunkId) => {
    if (!['ArrowUp', 'ArrowDown'].includes(event.key)) return;
    event.preventDefault();
    const current = [...orderRef.current];
    const from = current.indexOf(chunkId);
    const to = event.key === 'ArrowUp' ? from - 1 : from + 1;
    if (to < 0 || to >= current.length) return;
    [current[from], current[to]] = [current[to], current[from]];
    orderRef.current = current;
    setOrder(current);
    saveChunkOrder(current).catch(fail);
  }, [fail, orderRef, saveChunkOrder]);

  const playChunk = useCallback((chunk) => {
    const preview = previewAudioRef.current;
    if (preview && !preview.paused) preview.pause();
    if (playingChunk.current) {
      const same = playingChunk.current.dataset.chunk === chunk.id;
      playingChunk.current.pause();
      playingChunk.current = null;
      if (same) return;
    }
    const audio = new Audio(`${path(`/takes/${chunk.id}.wav`)}?v=${chunk.fingerprint}&play=${crypto.randomUUID()}`);
    audio.dataset.chunk = chunk.id;
    audio.onended = () => {
      if (playingChunk.current === audio) playingChunk.current = null;
    };
    playingChunk.current = audio;
    audio.play().catch(fail);
  }, [fail, path]);

  const paintPreview = useCallback(() => {
    const audio = previewAudioRef.current;
    if (!audio) return;
    drawWave(previewWaveRef.current, previewPeaksRef.current,
      audio.duration ? audio.currentTime / audio.duration : 0);
  }, []);

  const highlightPlayingLine = useCallback(() => {
    const audio = previewAudioRef.current;
    if (!audio) return;
    const segment = timelineRef.current.find(
      (item) => audio.currentTime >= item.start && audio.currentTime < item.end,
    );
    const id = segment?.id || null;
    setPlayingId((previous) => {
      if (previous !== id && id) {
        document.querySelector(`[data-chunk-id="${CSS.escape(id)}"]`)
          ?.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
      }
      return id;
    });
  }, []);

  const openPreview = useCallback(async () => {
    if (previewLoadingRef.current) return;
    const request = ++previewRequestRef.current;
    const pid = pidRef.current;
    const current = () => request === previewRequestRef.current && pid === pidRef.current;
    previewLoadingRef.current = true;
    setPreviewLoading(true);
    stopChunkAudio();
    try {
      const timeline = await api(`/api/projects/${pid}/timeline`);
      if (!current()) return;
      timelineRef.current = timeline.segments;
      window.timelineSegments = timelineRef.current;
    } catch {
      if (!current()) return;
      timelineRef.current = [];
      window.timelineSegments = timelineRef.current;
    }
    try {
      const response = await fetch(`/api/projects/${pid}/preview.wav`);
      if (!response.ok) throw new Error('preview failed');
      const bytes = await response.arrayBuffer();
      if (!current()) return;
      const decoder = new AudioContext();
      let decoded;
      try {
        decoded = await decoder.decodeAudioData(bytes.slice(0));
      } finally {
        await decoder.close();
      }
      if (!current()) return;
      previewPeaksRef.current = peaksFrom(decoded, 400);
      if (previewUrlRef.current) URL.revokeObjectURL(previewUrlRef.current);
      previewUrlRef.current = URL.createObjectURL(new Blob([bytes], { type: 'audio/wav' }));
      previewAudioRef.current.src = previewUrlRef.current;
    } catch {
      if (!current()) return;
      previewPeaksRef.current = null;
      previewAudioRef.current.src = `/api/projects/${pid}/preview.wav`;
    }
    previewAudioRef.current.load();
  }, [stopChunkAudio]);

  useEffect(() => {
    const audio = previewAudioRef.current;
    if (!audio) return undefined;
    const canplay = () => {
      if (!previewLoadingRef.current) return;
      previewLoadingRef.current = false;
      setPreviewLoading(false);
      setPreviewOpen(true);
      audio.play().catch(fail);
    };
    const error = () => {
      if (!previewLoadingRef.current) return;
      previewLoadingRef.current = false;
      setPreviewLoading(false);
      toast('preview failed — try Export instead');
    };
    const update = () => {
      setPreviewTime(`${formatTime(audio.currentTime)} / ${formatTime(audio.duration)}`);
      paintPreview();
      highlightPlayingLine();
    };
    const ended = () => {
      setPlayingId(null);
      highlightPlayingLine();
    };
    const play = () => {
      stopChunkAudio();
      setPreviewPlaying(true);
    };
    const pause = () => setPreviewPlaying(false);
    audio.addEventListener('canplay', canplay);
    audio.addEventListener('error', error);
    audio.addEventListener('play', play);
    audio.addEventListener('pause', pause);
    audio.addEventListener('ended', ended);
    audio.addEventListener('timeupdate', update);
    audio.addEventListener('loadedmetadata', update);
    audio.addEventListener('seeked', paintPreview);
    return () => {
      audio.removeEventListener('canplay', canplay);
      audio.removeEventListener('error', error);
      audio.removeEventListener('play', play);
      audio.removeEventListener('pause', pause);
      audio.removeEventListener('ended', ended);
      audio.removeEventListener('timeupdate', update);
      audio.removeEventListener('loadedmetadata', update);
      audio.removeEventListener('seeked', paintPreview);
    };
  // The audio element is absent during the initial loading screen, so state is
  // a dependency: attach the native media listeners when the app first mounts.
  }, [fail, highlightPlayingLine, paintPreview, Boolean(state), stopChunkAudio, toast]);

  useEffect(() => {
    let frame;
    const sweep = () => {
      const audio = previewAudioRef.current;
      if (audio && !audio.paused && previewOpen) paintPreview();
      frame = requestAnimationFrame(sweep);
    };
    frame = requestAnimationFrame(sweep);
    return () => cancelAnimationFrame(frame);
  }, [paintPreview, previewOpen]);

  useEffect(() => {
    const resize = () => {
      if (previewOpen) paintPreview();
    };
    window.addEventListener('resize', resize);
    return () => window.removeEventListener('resize', resize);
  }, [paintPreview, previewOpen]);

  const seekPreview = useCallback((event) => {
    const audio = previewAudioRef.current;
    if (!audio?.duration) return;
    const box = previewWaveRef.current.getBoundingClientRect();
    const at = Math.min(1, Math.max(0, (event.clientX - box.left) / box.width));
    audio.currentTime = at * audio.duration;
    paintPreview();
  }, [paintPreview]);

  const startMeter = useCallback((stream) => {
    const context = new AudioContext();
    const analyser = context.createAnalyser();
    analyser.fftSize = 1024;
    context.createMediaStreamSource(stream).connect(analyser);
    const samples = new Float32Array(analyser.fftSize);
    const history = [];
    let live = true;
    const tick = () => {
      if (!live) return;
      analyser.getFloatTimeDomainData(samples);
      let sum = 0;
      for (let i = 0; i < samples.length; i += 1) sum += samples[i] * samples[i];
      history.push(Math.min(1, Math.sqrt(sum / samples.length) * 4) ** 0.7);
      if (history.length > 64) history.shift();
      const drawing = surface(meterRef.current);
      if (drawing) {
        const { pen, width, height } = drawing;
        const slots = Math.max(1, Math.floor(width / 4));
        pen.fillStyle = ink(recordSecondsRef.current >= MIN_RECORD ? '--ok' : '--bad');
        for (let i = 0; i < slots; i += 1) {
          const value = history[history.length - slots + i] || 0;
          const tall = Math.max(2, value * (height - 3));
          pen.fillRect(i * 4, height / 2 - tall / 2, 2, tall);
        }
      }
      requestAnimationFrame(tick);
    };
    tick();
    return () => {
      live = false;
      context.close().catch(() => {});
    };
  }, []);

  const sendVoice = useCallback(async (file) => {
    const name = window.prompt('Name this voice', file.name.replace(/\.[^.]+$/, '')) ?? '';
    if (!name) return;
    const form = new FormData();
    form.append('file', file);
    const response = await fetch(`/api/voices?name=${encodeURIComponent(name)}&p=${pidRef.current}`, {
      method: 'POST',
      body: form,
    });
    if (!response.ok) {
      toast('upload failed');
      return;
    }
    applyState(await response.json());
  }, [applyState, toast]);

  const startRecording = useCallback(async () => {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    const captured = [];
    const recorder = new MediaRecorder(stream);
    recorderRef.current = recorder;
    const stopMeter = startMeter(stream);
    let started = 0;
    let seconds = 0;
    setRecordNote('');
    setRecordSeconds(0);
    recordSecondsRef.current = 0;
    setRecording(true);
    const timer = window.setInterval(() => {
      if (!started) return;
      seconds = (Date.now() - started) / 1000;
      recordSecondsRef.current = seconds;
      setRecordSeconds(seconds);
    }, 100);
    recorder.onstart = () => { started = Date.now(); };
    recorder.ondataavailable = (event) => captured.push(event.data);
    recorder.onstop = async () => {
      window.clearInterval(timer);
      stopMeter();
      stream.getTracks().forEach((track) => track.stop());
      const mimeType = recorder.mimeType;
      recorderRef.current = null;
      setRecording(false);
      if (seconds < MIN_RECORD) {
        setRecordNote(`Recording was ${seconds.toFixed(1)}s — keep going past ${MIN_RECORD}s so the model gets its 5s of speech. Nothing was saved.`);
        return;
      }
      const blob = new Blob(captured, { type: mimeType });
      await sendVoice(new File([blob], 'recording.webm', { type: blob.type }));
    };
    recorder.start();
  }, [sendVoice, startMeter]);

  const toggleRecording = useCallback(() => {
    if (recorderRef.current) recorderRef.current.stop();
    else startRecording().catch((error) => toast(`microphone: ${error.message}`));
  }, [startRecording, toast]);

  const switchProject = useCallback(async (pid) => {
    const request = ++navigationRef.current;
    closePreviewRef.current();
    stopChunkAudio();
    try {
      await flushEdits();
      const next = await api(`/api/state?p=${encodeURIComponent(pid)}`);
      if (request === navigationRef.current) applyState(next);
    } catch (error) {
      fail(error);
    }
  }, [applyState, fail, flushEdits, stopChunkAudio]);

  const exportWav = useCallback(async () => {
    const response = await fetch(path('/export.wav'));
    if (!response.ok) {
      let message = 'export failed';
      try { message = (await response.json()).detail || message; } catch { /* keep fallback */ }
      toast(message);
      return;
    }
    const url = URL.createObjectURL(await response.blob());
    const filename = `${stateRef.current?.project.title || 'mynah'}.wav`;
    Object.assign(document.createElement('a'), { href: url, download: filename }).click();
    toast(`downloaded ${filename}`);
    setTimeout(() => URL.revokeObjectURL(url), 0);
  }, [path, stateRef, toast]);

  if (!state || !project) {
    return <div className="boot"><span className="brand">mynah</span><span className="pill loading">starting…</span></div>;
  }

  const queueText = notice || (
    state.queue.pending || state.queue.current
      ? `${state.queue.current ? 'rendering' : ''}${state.queue.current && state.queue.pending ? ' · ' : ''}${state.queue.pending ? `${state.queue.pending} queued` : ''}`
      : ''
  );
  const countOrder = ['ready', 'stale', 'queued', 'rendering', 'no-voice', 'empty'];
  const counts = countOrder.filter((key) => tally[key]).map((key) => `${tally[key]} ${key}`).join(' · ');

  return (
    <>
      <TopBar
        state={state}
        project={project}
        onSwitch={switchProject}
        onApply={applyState}
        onFail={fail}
        onRemember={rememberDraft}
        onSave={saveField}
        titleDraft={draftsRef.current.get('title')?.value}
        openVoices={() => setOverlay('voices')}
        openSettings={() => setOverlay('settings')}
      />

      <main>
        <Toolbar
          voices={state.voices}
          project={project}
          onApply={applyState}
          onFail={fail}
          openVoices={() => setOverlay('voices')}
          openScript={() => setOverlay('script')}
        />

        <ol
          id="chunks"
          className="chunks"
          onDragOver={moveDrag}
          onDrop={(event) => { if (dragRef.current) event.preventDefault(); }}
        >
          {visibleChunks.map((chunk, index) => (
            <ChunkRow
              key={chunk.id}
              chunk={chunk}
              index={index}
              projectId={project.id}
              isPlaying={playingId === chunk.id}
              isDragging={draggingId === chunk.id}
              draftText={draftsRef.current.get(`chunk:${chunk.id}:text`)?.value}
              draftPause={draftsRef.current.get(`chunk:${chunk.id}:pause`)?.value}
              onRemember={rememberDraft}
              onSave={saveField}
              onGenerate={generate}
              onDelete={() => del(path(`/chunks/${chunk.id}`)).then(applyState).catch(fail)}
              onPlay={() => playChunk(chunk)}
              onDragStart={beginDrag}
              onDragEnd={finishDrag}
              onDragKey={moveByKeyboard}
              onFail={fail}
            />
          ))}
        </ol>

        <div id="empty" className="empty" hidden={project.chunks.length > 0}>
          <div className="empty-mark" />
          <h2>Nothing here yet</h2>
          <p>Paste a script and split it into lines, or add a line by hand. Each line is generated on its own, so fixing one never touches the rest.</p>
          <div className="row center">
            <button className="btn primary" data-open-script onClick={() => setOverlay('script')}>Paste script…</button>
            <button className="ghost" data-add-chunk onClick={() => post(path('/chunks'), { text: '' }).then(applyState).catch(fail)}>+ Add a line</button>
          </div>
        </div>
      </main>

      <footer className="bar bottom">
        <button id="generate" className="btn primary" disabled={!canGenerate} onClick={() => generate().catch(fail)}>
          Generate<span id="stale-count" className="count" hidden={!tally.stale}>{tally.stale || ''}</span>
        </button>
        <button id="stop" className="ghost" title="Stop the line being generated and clear the queue" disabled={!state.queue.pending && !state.queue.current} onClick={() => post(`/api/queue/clear?p=${project.id}`).then(applyState).catch(fail)}>Stop</button>
        <span id="queue" className={`muted mono${notice ? ' bad' : ''}`} role="status" aria-live="polite">{queueText}</span>
        <button id="activity-toggle" className="ghost small" onClick={() => setActivityOpen((open) => !open)}>Activity</button>
        <span className="grow" />
        <span id="counts" className="muted">{counts}</span>
        <button id="preview-open" className="btn" title="Play the full stitched mix in your browser" hidden={previewOpen} disabled={!canRender || previewLoading} onClick={openPreview}>
          {previewLoading ? 'Loading…' : '▶ Preview'}
        </button>
        <div id="preview-active" className="preview-active" hidden={!previewOpen}>
          <audio id="preview-audio" ref={previewAudioRef} preload="none" />
          <button id="preview-play" className="ghost icon" title="Play/pause" onClick={() => {
            const audio = previewAudioRef.current;
            if (audio.paused) audio.play().catch(fail); else audio.pause();
          }}>{previewPlaying ? '⏸' : '▶'}</button>
          <canvas
            id="preview-wave"
            ref={previewWaveRef}
            className="wave"
            title="Click to seek"
            onPointerDown={(event) => {
              event.currentTarget.setPointerCapture(event.pointerId);
              seekPreview(event);
            }}
            onPointerMove={(event) => { if (event.buttons) seekPreview(event); }}
          />
          <span id="preview-time" className="mono small muted">{previewTime}</span>
          <button id="preview-close" className="ghost icon" title="Close preview" onClick={closePreview}>✕</button>
        </div>
        <button id="export" className="btn" disabled={!canRender} onClick={() => exportWav().catch(fail)}>Export WAV</button>
      </footer>

      <div id="activity" className="activity" hidden={!activityOpen}><pre id="log">{[...state.log].reverse().join('\n')}</pre></div>

      <ScriptModal
        open={overlay === 'script'}
        project={project}
        close={() => setOverlay(null)}
        onApply={applyState}
        onFail={fail}
      />
      <VoicesDrawer
        open={overlay === 'voices'}
        voices={state.voices}
        project={project}
        close={() => setOverlay(null)}
        onApply={applyState}
        onFail={fail}
        recording={recording}
        recordSeconds={recordSeconds}
        recordNote={recordNote}
        meterRef={meterRef}
        fileRef={fileRef}
        toggleRecording={toggleRecording}
        sendVoice={sendVoice}
      />
      <SettingsDrawer
        open={overlay === 'settings'}
        project={project}
        close={() => setOverlay(null)}
        onRemember={rememberDraft}
        onSave={saveField}
        onApply={applyState}
        onFail={fail}
      />
      <div id="backdrop" className="backdrop" hidden={!overlay} onClick={() => setOverlay(null)} />
    </>
  );
}

export default App;
