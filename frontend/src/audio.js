export function ink(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

export function surface(canvas) {
  if (!canvas) return null;
  const ratio = window.devicePixelRatio || 1;
  const width = canvas.clientWidth;
  const height = canvas.clientHeight;
  if (canvas.width !== Math.round(width * ratio) || canvas.height !== Math.round(height * ratio)) {
    canvas.width = Math.round(width * ratio);
    canvas.height = Math.round(height * ratio);
  }
  const pen = canvas.getContext('2d');
  pen.setTransform(ratio, 0, 0, ratio, 0, 0);
  pen.clearRect(0, 0, width, height);
  return { pen, width, height };
}

export function peaksFrom(buffer, buckets) {
  const data = buffer.getChannelData(0);
  const per = Math.max(1, Math.floor(data.length / buckets));
  const peaks = new Float32Array(buckets);
  let loudest = 0;
  for (let i = 0; i < buckets; i += 1) {
    let top = 0;
    for (let j = i * per; j < (i + 1) * per && j < data.length; j += 3) {
      const value = Math.abs(data[j]);
      if (value > top) top = value;
    }
    peaks[i] = top;
    if (top > loudest) loudest = top;
  }
  if (loudest > 0) {
    for (let i = 0; i < buckets; i += 1) peaks[i] /= loudest;
  }
  return peaks;
}

export function drawWave(canvas, peaks, progress) {
  const drawing = surface(canvas);
  if (!drawing) return;
  const { pen, width, height } = drawing;
  const bar = 2;
  const gap = 2;
  const count = Math.max(1, Math.floor(width / (bar + gap)));
  const middle = height / 2;
  const played = ink('--accent');
  const rest = ink('--line-strong');
  for (let i = 0; i < count; i += 1) {
    const value = peaks ? peaks[Math.floor((i / count) * peaks.length)] || 0 : 0;
    const tall = Math.max(2, value * (height - 3));
    pen.fillStyle = (i + 0.5) / count <= progress ? played : rest;
    pen.fillRect(i * (bar + gap), middle - tall / 2, bar, tall);
  }
}

export function formatTime(seconds) {
  const safe = Number.isFinite(seconds) && seconds > 0 ? seconds : 0;
  return `${Math.floor(safe / 60)}:${String(Math.floor(safe % 60)).padStart(2, '0')}`;
}
