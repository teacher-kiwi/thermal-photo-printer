const statusEl = document.getElementById('status');
const autoEl = document.getElementById('auto');
const frameEl = document.getElementById('frame');
const titleEl = document.getElementById('title');
const testPrintBtn = document.getElementById('testPrint');
const defaultNameEl = document.getElementById('defaultName');
const likeNameEl = document.getElementById('likeName');
const likeMinEl = document.getElementById('likeMin');
const likeMaxEl = document.getElementById('likeMax');
const tagInputEl = document.getElementById('tagInput');
const tagChipsEl = document.getElementById('tagChips');
const tagsPerLineEl = document.getElementById('tagsPerLine');
const TAG_MAX_LEN = 30;   // server.py 와 같은 값
const TAG_MAX_COUNT = 20;
let tags = [];            // 해시태그 목록 (# 없이)
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
  const s = { auto: autoEl.checked, frame: frameEl.checked, default_name: defaultNameEl.value.trim() };
  for (const k of SLIDERS) s[k] = parseFloat(document.getElementById(k).value);
  s.dither = document.querySelector('input[name="dither"]:checked').value;
  s.title = titleEl.value.trim();
  s.camera = document.querySelector('input[name="camera"]:checked').value;
  s.like_name = likeNameEl.value.trim();
  s.like_min = parseInt(likeMinEl.value, 10) || 0;
  s.like_max = parseInt(likeMaxEl.value, 10) || 0;
  s.hashtags = tags.slice();
  s.tags_per_line = parseInt(tagsPerLineEl.value, 10) || 1;
  return s;
}

function writeForm(s) {
  autoEl.checked = s.auto;
  frameEl.checked = s.frame;
  defaultNameEl.value = s.default_name;
  author.setDefault(s.default_name);
  titleEl.value = s.title;
  document.querySelector(`input[name="camera"][value="${s.camera}"]`).checked = true;
  camera.setFacing(s.camera);
  likeNameEl.value = s.like_name;
  likeMinEl.value = s.like_min;
  likeMaxEl.value = s.like_max;
  likes.setConfig(s.like_name, s.like_min, s.like_max);
  tags = s.hashtags.slice();
  tagsPerLineEl.value = s.tags_per_line;
  renderTags();
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

// 저장하지 않은 변경이 있으면 새로고침/닫기 전에 브라우저가 한 번 확인
window.addEventListener('beforeunload', (e) => {
  if (saved && !sameSettings(readForm(), saved)) {
    e.preventDefault();
    e.returnValue = '';
  }
});

function sameSettings(a, b) {
  if (a.auto !== b.auto || a.frame !== b.frame || a.dither !== b.dither) return false;
  if (a.default_name !== b.default_name || a.like_name !== b.like_name) return false;
  if (a.title !== b.title || a.camera !== b.camera) return false;
  if (a.like_min !== b.like_min || a.like_max !== b.like_max) return false;
  if (a.tags_per_line !== b.tags_per_line || a.hashtags.join(' ') !== b.hashtags.join(' ')) return false;
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
      body: JSON.stringify({ id: imageId, settings: readForm(), name: author.get(), likes: likes.get() }),
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

document.querySelectorAll('input[type="range"], #auto, #frame, input[name="dither"]').forEach((el) => {
  el.addEventListener('input', onChange);
  el.addEventListener('change', onChange);
});

// 메인 화면 제목은 출력물과 무관 → 미리보기 대신 '저장 안 됨' 표시만 갱신
titleEl.addEventListener('input', refreshLabels);

// 메인 화면 카메라: 이 페이지 카메라도 같은 방향으로 바꿔서 바로 확인
document.querySelectorAll('input[name="camera"]').forEach((el) => {
  el.addEventListener('change', () => {
    camera.setFacing(el.value);
    refreshLabels();
  });
});

// 프레임 머리글의 이름이 바뀌어도 미리보기 갱신 (camera.js)
const author = setupAuthorName(onChange);

// 기본 이름: 프레임 이름칸의 흐린 글씨도 같이 바꿔서 바로 확인
defaultNameEl.addEventListener('input', () => {
  author.setDefault(defaultNameEl.value.trim());
  onChange();
});

// 좋아요 문구: 카메라 틀 아래 문구도 같이 바꿔서 바로 확인
const likes = setupLikes();
[likeNameEl, likeMinEl, likeMaxEl].forEach((el) => {
  el.addEventListener('input', () => {
    const s = readForm();
    likes.setConfig(s.like_name, s.like_min, s.like_max);
    onChange();
  });
});

// ── 해시태그: 추가/삭제하면 카메라 틀 아래와 미리보기에 바로 반영 ──
const hashtags = setupHashtags();

// server.py clean_hashtags 와 같은 규칙: 앞의 #·공백 제거, 30자 제한
function cleanTag(t) {
  return t.replace(/\s/g, '').replace(/^#+/, '').slice(0, TAG_MAX_LEN);
}

function renderTags() {
  tagChipsEl.replaceChildren(...tags.map((t, i) => {
    const chip = document.createElement('span');
    chip.className = 'chip';
    chip.textContent = '#' + t;
    const x = document.createElement('button');
    x.type = 'button';
    x.textContent = '×';
    x.title = '삭제';
    x.addEventListener('click', () => {
      tags.splice(i, 1);
      tagsChanged();
    });
    chip.append(x);
    return chip;
  }));
  hashtags.setConfig(tags, parseInt(tagsPerLineEl.value, 10) || 1);
}

function tagsChanged() {
  renderTags();
  onChange();
}

function addTags() {
  // "#가을 #축제" 처럼 여러 개를 한 번에 넣어도 됨 (띄어쓰기·쉼표·# 로 구분)
  for (const raw of tagInputEl.value.split(/[\s,#]+/)) {
    const t = cleanTag(raw);
    if (t && !tags.includes(t) && tags.length < TAG_MAX_COUNT) tags.push(t);
  }
  tagInputEl.value = '';
  tagsChanged();
}

document.getElementById('tagAdd').addEventListener('click', addTags);
tagInputEl.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.isComposing) {  // 한글 조합 중 Enter 는 무시
    e.preventDefault();
    addTags();
  }
});
tagsPerLineEl.addEventListener('input', tagsChanged);

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
    likes.reroll(); // 새 사진마다 새 숫자
    testPrintBtn.disabled = false;
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
const camera = setupCamera((blob) => uploadImage(blob, '카메라 촬영 ' + new Date().toLocaleTimeString()));

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

// ── 출력 테스트: 지금 미리보기와 같은 값으로 실제 출력 ──
testPrintBtn.addEventListener('click', async () => {
  if (!imageId) return;
  testPrintBtn.disabled = true;
  showStatus('출력 중…', 'busy');
  try {
    const res = await fetch('/admin/print', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: imageId, settings: readForm(), name: author.get(), likes: likes.get() }),
    });
    const data = await res.json().catch(() => ({}));
    if (res.ok && data.status === 'ok') {
      showStatus('✅ 출력 완료!', 'ok');
    } else {
      if (res.status === 404) imageId = null; // 서버 재시작 등으로 사진이 사라짐
      showStatus('❌ 출력 실패: ' + (data.message || res.status), 'err');
    }
  } catch (e) {
    showStatus('❌ 출력 실패: ' + e.message, 'err');
  } finally {
    testPrintBtn.disabled = !imageId;
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
