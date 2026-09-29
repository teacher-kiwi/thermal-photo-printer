const statusEl = document.getElementById('status');
const autoEl = document.getElementById('auto');
const dirtyEl = document.getElementById('dirty');
const busyEl = document.getElementById('busy');
const appliedEl = document.getElementById('applied');
const resultImg = document.getElementById('result');
const originalImg = document.getElementById('original');

const SLIDERS = ['target', 'stretch', 'brightness', 'gamma', 'sharpen'];

const DITHER_HINTS = {
  fs: '기본. 빠르고 무난하지만 점이 번져 어둡게 뭉치기 쉬움',
  bluenoise: '점이 고르게 흩어져 줄무늬가 없고 번짐에 강함. 가장 빠름',
  atkinson: '점이 또렷하고 대비가 강함. 어두운 부분은 뭉개지기 쉬움',
  jjn: '오차를 넓게 퍼뜨려 계조가 부드러움. 인물 사진에 무난',
  stucki: 'JJN과 비슷하지만 조금 더 선명',
  sierra: 'JJN과 비슷한 품질',
};

let saved = null;     // 서버에 저장된 설정
let defaults = null;  // 기본값
let imageId = null;   // 서버에 올려둔 테스트 사진 id
let originalUrl = null;

let statusTimer = null;
function showStatus(msg, kind) {
  statusEl.hidden = false;
  statusEl.textContent = msg;
  statusEl.className = 'status ' + (kind || '');
  clearTimeout(statusTimer);
  if (kind !== 'busy') statusTimer = setTimeout(() => { statusEl.hidden = true; }, 2500);
}

// ── 화면 ↔ 설정값 ──
function readForm() {
  const s = { auto: autoEl.checked };
  for (const k of SLIDERS) s[k] = parseFloat(document.getElementById(k).value);
  s.dither = document.querySelector('input[name="dither"]:checked').value;
  return s;
}

function writeForm(s) {
  autoEl.checked = s.auto;
  for (const k of SLIDERS) document.getElementById(k).value = s[k];
  document.querySelector(`input[name="dither"][value="${s.dither}"]`).checked = true;
  refreshLabels();
}

function refreshLabels() {
  const s = readForm();
  for (const k of SLIDERS) {
    document.getElementById(k + 'Out').textContent =
      k === 'sharpen' ? s[k].toFixed(0) : k === 'stretch' ? s[k].toFixed(1) : s[k].toFixed(2);
  }
  document.getElementById('ditherHint').textContent = DITHER_HINTS[s.dither] || '';
  // 자동 보정일 땐 수동 값(밝기·감마)이, 수동일 땐 자동 값이 쓰이지 않으므로 흐리게
  document.querySelectorAll('.field[data-mode]').forEach((el) => {
    const off = (el.dataset.mode === 'auto') !== s.auto;
    el.classList.toggle('off', off);
    el.querySelectorAll('input').forEach((i) => { i.disabled = off; });
  });
  dirtyEl.hidden = !saved || sameSettings(s, saved);
}

function sameSettings(a, b) {
  if (a.auto !== b.auto || a.dither !== b.dither) return false;
  return SLIDERS.every((k) => Math.abs(a[k] - b[k]) < 1e-6);
}

// ── 미리보기: 요청은 한 번에 하나만. 도중에 값이 바뀌면 끝난 뒤 최신 값으로 한 번 더 ──
let inFlight = false;
let pending = false;

async function updatePreview() {
  if (!imageId) return;
  if (inFlight) { pending = true; return; }
  inFlight = true;
  pending = false;
  busyEl.hidden = false;
  try {
    const res = await fetch('/admin/preview', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: imageId, settings: readForm() }),
    });
    const data = await res.json().catch(() => ({}));
    if (res.ok && data.status === 'ok') {
      resultImg.src = data.image;
      appliedEl.textContent =
        `적용된 값 — 감마 ${data.gamma.toFixed(2)} · 밝기 ${data.brightness.toFixed(2)}` +
        ` · ${data.width}×${data.height}px`;
    } else {
      if (res.status === 404) imageId = null; // 서버 재시작 등으로 사진이 사라짐
      showStatus('❌ ' + (data.message || res.status), 'err');
    }
  } catch (e) {
    showStatus('❌ 미리보기 실패: ' + e.message, 'err');
  } finally {
    inFlight = false;
    busyEl.hidden = true;
    if (pending) updatePreview();
  }
}

let debounceTimer = null;
function onChange() {
  refreshLabels();
  clearTimeout(debounceTimer);
  debounceTimer = setTimeout(updatePreview, 120);
}

document.querySelectorAll('input[type="range"], #auto, input[name="dither"]').forEach((el) => {
  el.addEventListener('input', onChange);
  el.addEventListener('change', onChange);
});

// ── 테스트 사진 업로드 (파일 선택 / 실시간 카메라 공통) ──
async function uploadImage(blob, name) {
  showStatus('사진 올리는 중…', 'busy');
  const form = new FormData();
  form.append('image', blob, name);
  try {
    const res = await fetch('/admin/upload', { method: 'POST', body: form });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || data.status !== 'ok') {
      showStatus('❌ ' + (data.message || res.status), 'err');
      return;
    }
    imageId = data.id;
    if (originalUrl) URL.revokeObjectURL(originalUrl);
    originalUrl = URL.createObjectURL(blob);
    originalImg.src = originalUrl;
    document.getElementById('empty').hidden = true;
    document.getElementById('compare').hidden = false;
    document.getElementById('fileHint').textContent = name;
    statusEl.hidden = true;
    updatePreview();
  } catch (err) {
    showStatus('❌ 업로드 실패: ' + err.message, 'err');
  }
}

document.getElementById('file').addEventListener('change', (e) => {
  const file = e.target.files[0];
  e.target.value = '';
  if (file) uploadImage(file, file.name || 'photo.jpg');
});

// ── 실시간 카메라 (camera.js, 메인 페이지와 같은 방식으로 촬영) ──
setupCamera((blob) => uploadImage(blob, '카메라 촬영 ' + new Date().toLocaleTimeString()));

// 출력 미리보기를 누르면 실제 픽셀 크기(1:1) ↔ 화면 맞춤 전환
resultImg.addEventListener('click', () => resultImg.classList.toggle('actual'));

// ── 저장 / 되돌리기 ──
document.getElementById('save').addEventListener('click', async () => {
  try {
    const res = await fetch('/admin/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(readForm()),
    });
    const data = await res.json().catch(() => ({}));
    if (res.ok && data.status === 'ok') {
      saved = data.settings;
      writeForm(saved);
      showStatus('✅ 저장됨 — 다음 출력부터 적용', 'ok');
    } else {
      showStatus('❌ 저장 실패: ' + (data.message || res.status), 'err');
    }
  } catch (e) {
    showStatus('❌ 저장 실패: ' + e.message, 'err');
  }
});

document.getElementById('revert').addEventListener('click', () => {
  writeForm(saved);
  updatePreview();
});

document.getElementById('reset').addEventListener('click', () => {
  writeForm(defaults);
  updatePreview();
});

// ── 초기화: 슬라이더 범위와 저장된 값 불러오기 ──
(async function init() {
  try {
    const res = await fetch('/admin/settings');
    const data = await res.json();
    for (const k of SLIDERS) {
      const el = document.getElementById(k);
      [el.min, el.max] = data.ranges[k];
    }
    saved = data.settings;
    defaults = data.defaults;
    writeForm(saved);
  } catch (e) {
    showStatus('❌ 설정을 불러오지 못했습니다: ' + e.message, 'err');
  }
})();
