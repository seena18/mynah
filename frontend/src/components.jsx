import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { del, post, put } from './api.js';

const MIN_RECORD = 6;
const PARAMS = [
  ['temperature', 'Temperature', 0.05, 0.05, 1.5],
  ['top_p', 'Top-p', 0.01, 0.1, 1],
  ['top_k', 'Top-k', 50, 1, 4000],
  ['repetition_penalty', 'Repetition penalty', 0.05, 1, 3],
];

function autosize(node) {
  if (!node) return;
  node.style.height = 'auto';
  node.style.height = `${node.scrollHeight + 2}px`;
}

export function TopBar({
  state,
  project,
  onSwitch,
  onApply,
  onFail,
  onRemember,
  onSave,
  titleDraft,
  openVoices,
  openSettings,
}) {
  const title = titleDraft ?? project.title;
  const failed = state.voices.filter((voice) => voice.status === 'error').length;
  const compiling = state.voices.filter((voice) => voice.status === 'compiling').length;
  const engineLabel = {
    cold: 'idle',
    loading: 'loading model…',
    ready: 'ready',
    error: 'error',
  }[state.engine.state] || state.engine.state;
  const engineText = state.engine.state === 'loading' && state.engine.progress
    ? state.engine.progress
    : engineLabel;

  const newProject = () => {
    const name = window.prompt('Project name', 'Untitled');
    if (name === null) return;
    post('/api/projects', { title: name }).then(onApply).catch(onFail);
  };

  const deleteProject = () => {
    const count = project.chunks.length;
    const suffix = count ? ` and its ${count} line(s)` : '';
    if (window.confirm(`Delete "${project.title}"${suffix}? Takes shared with other projects are kept.`)) {
      del(`/api/projects/${project.id}`).then(onApply).catch(onFail);
    }
  };

  return (
    <header className="bar top">
      <span className="brand">mynah</span>
      <span className="sep" />
      <div className="project">
        <select id="project-select" title="Switch project" value={project.id} onChange={(event) => onSwitch(event.target.value)}>
          {state.projects.map((item) => (
            <option key={item.id} value={item.id}>{item.title} · {item.counts.ready || 0}/{item.chunks}</option>
          ))}
        </select>
        <input
          id="title"
          className="title"
          value={title}
          placeholder="Untitled"
          spellCheck="false"
          title="Rename project"
          onChange={(event) => onRemember(
            'title',
            event.target.value,
            (pid, value) => post(`/api/projects/${pid}`, { title: value }),
          )}
          onBlur={() => onSave('title').catch(onFail)}
          onKeyDown={(event) => { if (event.key === 'Enter') event.currentTarget.blur(); }}
        />
        <button id="new-project" className="ghost" title="New project" onClick={newProject}>+ New</button>
        <button id="delete-project" className="ghost danger icon" title="Delete this project" disabled={state.projects.length < 2} onClick={deleteProject}>✕</button>
      </div>
      <span className="grow" />
      <button id="open-voices" className="ghost" title="Manage voices (shared by all projects)" onClick={openVoices}>
        Voices
        <span id="voice-badge" className={`badge${failed ? ' attention' : ''}`} hidden={!(failed || compiling)}>{failed ? `${failed} failed` : `${compiling} compiling`}</span>
      </button>
      <button id="open-settings" className="ghost icon" title="Sampling & maintenance" onClick={openSettings}>⚙</button>
      <span
        id="engine"
        className={`pill ${state.engine.state === 'ready' ? 'ready' : state.engine.state === 'error' ? 'error' : 'loading'}`}
        title={state.engine.error || (state.engine.loader ? `weights: ${state.engine.loader}` : '')}
      >{engineText} · {state.engine.device}</span>
    </header>
  );
}

export function Toolbar({ voices, project, onApply, onFail, openVoices, openScript }) {
  const current = voices.find((voice) => voice.id === project.voice_id);
  const hint = !voices.length
    ? 'record or upload one to begin'
    : !current
      ? 'pick a voice to generate'
      : current.status === 'ready'
        ? `${current.seconds}s reference`
        : 'compiling…';

  return (
    <div className="toolbar">
      <label className="field">
        <span className="label">Voice</span>
        <select id="voice-select" value={project.voice_id || ''} onChange={(event) => {
          if (event.target.value === '__manage__') {
            openVoices();
            return;
          }
          post(`/api/projects/${project.id}`, { voice_id: event.target.value }).then(onApply).catch(onFail);
        }}>
          <option value="">— none —</option>
          {voices.filter((voice) => voice.status !== 'error').map((voice) => (
            <option key={voice.id} value={voice.id}>
              {voice.status === 'ready' ? voice.name : `${voice.name} (compiling…)`}
            </option>
          ))}
          <option value="__manage__">Manage voices…</option>
        </select>
      </label>
      <span id="voice-hint" className="muted">{hint}</span>
      <span className="grow" />
      <button id="paste-script" className="btn" onClick={openScript}>Paste script…</button>
      <button id="add-chunk" className="ghost" onClick={() => post(`/api/projects/${project.id}/chunks`, { text: '' }).then(onApply).catch(onFail)}>+ Line</button>
    </div>
  );
}

export function ChunkRow({
  chunk,
  index,
  projectId,
  isPlaying,
  isDragging,
  draftText,
  draftPause,
  onRemember,
  onSave,
  onGenerate,
  onDelete,
  onPlay,
  onDragStart,
  onDragEnd,
  onDragKey,
  onFail,
}) {
  const textRef = useRef(null);
  const text = draftText ?? chunk.text;
  const pause = draftPause ?? chunk.pause_after;

  useLayoutEffect(() => autosize(textRef.current), [text]);

  return (
    <li
      className={`chunk s-${chunk.status}${isPlaying ? ' now-playing' : ''}${isDragging ? ' dragging' : ''}`}
      data-chunk-id={chunk.id}
      data-fingerprint={chunk.fingerprint}
    >
      <span className="n" title={chunk.status}>
        <button
          className="drag"
          draggable="true"
          title="Drag to reorder"
          aria-label={`Reorder line ${index + 1}`}
          onDragStart={(event) => onDragStart(event, chunk.id)}
          onDragEnd={onDragEnd}
          onKeyDown={(event) => onDragKey(event, chunk.id)}
        />
        <span className="marker"><span className="dot" /><span className="idx">{index + 1}</span></span>
      </span>
      <textarea
        ref={textRef}
        rows="1"
        spellCheck="false"
        placeholder="Empty line — type something to say"
        value={text}
        onChange={(event) => {
          autosize(event.currentTarget);
          onRemember(
            `chunk:${chunk.id}:text`,
            event.target.value,
            (pid, value) => put(`/api/projects/${pid}/chunks/${chunk.id}`, { text: value }),
          );
        }}
        onBlur={() => onSave(`chunk:${chunk.id}:text`).catch(onFail)}
      />
      <span className="side">
        <button className="ghost icon play" title="Play take" disabled={chunk.status !== 'ready'} onClick={onPlay}>▶</button>
        <button className="ghost icon regen" title="Generate this line" disabled={['queued', 'rendering', 'empty', 'no-voice'].includes(chunk.status)} onClick={() => onGenerate(chunk.id).catch(onFail)}>↻</button>
        <input
          className="pause"
          type="number"
          step="0.1"
          min="0"
          max="10"
          title="Pause after, seconds"
          value={pause}
          onChange={(event) => onRemember(
            `chunk:${chunk.id}:pause`,
            event.target.value,
            (pid, value) => put(`/api/projects/${pid}/chunks/${chunk.id}`, { pause_after: Number(value) }),
          )}
          onBlur={() => onSave(`chunk:${chunk.id}:pause`).catch(onFail)}
        />
        <span className="unit">s</span>
        <button className="ghost icon danger drop" title="Delete line" onClick={onDelete}>✕</button>
      </span>
      <span className="err" hidden={!chunk.error}>{chunk.error || ''}</span>
    </li>
  );
}

export function ScriptModal({ open, project, close, onApply, onFail }) {
  const [script, setScript] = useState('');
  const [maxChars, setMaxChars] = useState(280);
  const textareaRef = useRef(null);

  useEffect(() => {
    if (open) setTimeout(() => textareaRef.current?.focus(), 30);
  }, [open]);

  const split = () => {
    const text = script.trim();
    if (!text) {
      textareaRef.current?.focus();
      return;
    }
    if (project.chunks.length && !window.confirm('Replace the current lines? Takes for identical lines are kept.')) return;
    post(`/api/projects/${project.id}/split`, { text, max_chars: Number(maxChars) })
      .then((next) => {
        onApply(next);
        close();
        setScript('');
      })
      .catch(onFail);
  };

  return (
    <div id="script-modal" className="modal" hidden={!open} role="dialog" aria-modal="true" aria-labelledby="script-title">
      <div className="sheet">
        <header>
          <h2 id="script-title">Paste script</h2>
          <span className="muted">A blank line starts a new chunk. Longer paragraphs split at sentence ends.</span>
          <span className="grow" />
          <button className="ghost icon" data-close title="Close" onClick={close}>✕</button>
        </header>
        <textarea
          id="script"
          ref={textareaRef}
          rows="14"
          spellCheck="false"
          placeholder="Paste your script here…"
          value={script}
          onChange={(event) => setScript(event.target.value)}
        />
        <footer>
          <label className="field inline">
            <span className="label">Max chars</span>
            <input id="max-chars" type="number" value={maxChars} min="60" max="900" step="20" onChange={(event) => setMaxChars(event.target.value)} />
          </label>
          <span className="muted">Longer chunks sound smoother — prosody restarts at every boundary.</span>
          <span className="grow" />
          <button className="ghost" data-close onClick={close}>Cancel</button>
          <button id="split" className="btn primary" onClick={split}>Split into chunks</button>
        </footer>
      </div>
    </div>
  );
}

function VoiceCard({ voice, current, projectId, onApply, onFail }) {
  const [name, setName] = useState(voice.name);
  const nameRef = useRef(null);

  useEffect(() => {
    if (document.activeElement !== nameRef.current) setName(voice.name);
  }, [voice.name]);

  return (
    <li className={`voice-card${current ? ' current' : ''}`}>
      <div className="v-head">
        <input
          ref={nameRef}
          className="v-name"
          spellCheck="false"
          title="Rename"
          value={name}
          onChange={(event) => setName(event.target.value)}
          onBlur={() => {
            if (name !== voice.name) put(`/api/voices/${voice.id}?p=${projectId}`, { name }).then(onApply).catch(onFail);
          }}
          onKeyDown={(event) => { if (event.key === 'Enter') event.currentTarget.blur(); }}
        />
        <span className={`v-meta ${voice.status === 'ready' ? '' : voice.status}`}>
          {voice.status === 'ready' ? `${voice.seconds}s` : voice.status === 'error' ? 'failed' : 'compiling…'}
        </span>
      </div>
      <audio controls preload="metadata" src={voice.status === 'error' ? undefined : `/api/voices/${voice.id}/reference.wav`} hidden={voice.status === 'error'} />
      <div className="v-actions">
        <button className="btn use" disabled={current || voice.status !== 'ready'} onClick={() => post(`/api/projects/${projectId}`, { voice_id: voice.id }).then(onApply).catch(onFail)}>
          {current ? 'In use here' : 'Use in this project'}
        </button>
        <span className="grow" />
        <button className="ghost danger del" onClick={() => {
          if (window.confirm(`Delete "${name}"? Projects using it will need another voice.`)) {
            del(`/api/voices/${voice.id}?p=${projectId}`).then(onApply).catch(onFail);
          }
        }}>Delete</button>
      </div>
      <p className="v-error" hidden={!voice.error}>{voice.error || ''}</p>
    </li>
  );
}

export function VoicesDrawer({
  open,
  voices,
  project,
  close,
  onApply,
  onFail,
  recording,
  recordSeconds,
  recordNote,
  meterRef,
  fileRef,
  toggleRecording,
  sendVoice,
}) {
  return (
    <aside id="voices-drawer" className="drawer" hidden={!open} aria-label="Voices">
      <header>
        <h2>Voices</h2>
        <span className="muted">shared by every project</span>
        <span className="grow" />
        <button className="ghost icon" data-close title="Close" onClick={close}>✕</button>
      </header>
      <div className="drawer-actions">
        <button id="record" className={`btn${recording ? ' recording' : ''}`} onClick={toggleRecording}>{recording ? '■ Stop' : '● Record'}</button>
        <button id="upload" className="btn" onClick={() => fileRef.current?.click()}>Upload…</button>
        <input id="file" ref={fileRef} type="file" accept="audio/*,video/webm" hidden onChange={(event) => {
          if (event.target.files[0]) sendVoice(event.target.files[0]).catch(onFail);
          event.target.value = '';
        }} />
        <canvas id="rec-meter" ref={meterRef} className="meter" hidden={!recording} data-enough={recordSeconds >= MIN_RECORD ? 'yes' : ''} />
        <span id="rec-timer" className="timer mono" hidden={!recording} style={{ color: recordSeconds >= MIN_RECORD ? 'var(--ok)' : 'var(--bad)' }}>{recordSeconds.toFixed(1)}s</span>
        <span className="grow" />
        <span id="rec-hint" className="muted small" hidden={recording}>needs &gt; 5 s of clean speech</span>
      </div>
      <p id="rec-note" className="note" hidden={!recordNote} style={{ color: 'var(--warn)' }}>{recordNote}</p>
      <ul id="voice-list" className="voice-list">
        {voices.map((voice) => (
          <VoiceCard
            key={voice.id}
            voice={voice}
            current={voice.id === project.voice_id}
            projectId={project.id}
            onApply={onApply}
            onFail={onFail}
          />
        ))}
      </ul>
      <div id="voices-empty" className="empty small" hidden={voices.length > 0}>
        <h2>No voices yet</h2>
        <p>Record a few sentences, or upload a clip. Anything ffmpeg can read works.</p>
      </div>
    </aside>
  );
}

export function SettingsDrawer({ open, project, close, onRemember, onSave, onApply, onFail }) {
  return (
    <aside id="settings-drawer" className="drawer" hidden={!open} aria-label="Sampling and maintenance">
      <header>
        <h2>Sampling</h2>
        <span className="muted">for this project</span>
        <span className="grow" />
        <button className="ghost icon" data-close title="Close" onClick={close}>✕</button>
      </header>
      <div className="settings">
        {PARAMS.map(([key, label, step, min, max]) => {
          const editKey = `param:${key}`;
          const current = window.edits?.get(editKey)?.value ?? project.params[key];
          return (
            <label className="field row" key={key}>
              <span className="label">{label}</span>
              <input
                id={`p-${key}`}
                type="number"
                step={step}
                min={min}
                max={max}
                value={current}
                onChange={(event) => onRemember(
                  editKey,
                  event.target.value,
                  (pid, value) => post(`/api/projects/${pid}`, { params: { [key]: Number(value) } }),
                )}
                onBlur={() => onSave(editKey).catch(onFail)}
              />
            </label>
          );
        })}
        <p className="muted small">Changing any of these makes every chunk stale — they are part of a take&apos;s fingerprint.</p>
      </div>
      <header className="sub"><h2>Maintenance</h2></header>
      <div className="settings">
        <div className="row">
          <button id="tidy" className="ghost" onClick={() => post(`/api/tidy?p=${project.id}`).then(onApply).catch(onFail)}>Delete unused takes</button>
          <span className="muted small">Old versions of edited lines, from every project.</span>
        </div>
      </div>
    </aside>
  );
}
