const statusEl = document.getElementById('status');

function showStatus(msg, kind) {
  statusEl.hidden = false;
  statusEl.textContent = msg;
  statusEl.className = 'status ' + (kind || '');
}

// 서버로 이미지 전송 → 프린터 출력
async function sendImage(blob) {
  showStatus('출력 중…', 'busy');
  const form = new FormData();
  form.append('image', blob, 'photo.jpg');
  try {
    const res = await fetch('/print', { method: 'POST', body: form });
    const data = await res.json().catch(() => ({}));
    if (res.ok && data.status === 'ok') {
      showStatus('✅ 출력 완료!', 'ok');
    } else {
      showStatus('❌ 출력 실패: ' + (data.message || res.status), 'err');
    }
  } catch (e) {
    showStatus('❌ 전송 실패: ' + e.message, 'err');
  }
}

// ── 방법 1: 파일 선택 / 기본 카메라 ──
document.getElementById('file').addEventListener('change', (e) => {
  const file = e.target.files[0];
  if (file) sendImage(file);
  e.target.value = '';
});

// ── 방법 2: 실시간 카메라 (camera.js) ──
setupCamera(sendImage);
