const video = document.getElementById('video');
const canvas = document.getElementById('canvas');
const statusEl = document.getElementById('status');
const camErr = document.getElementById('camErr');

let facing = 'environment';
let stream = null;

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
      showStatus(data.mock ? '🧪 미리보기 생성됨' : '✅ 출력 완료!', 'ok');
      if (data.preview) {
        const card = document.getElementById('previewCard');
        const img = document.getElementById('preview');
        img.src = data.preview + '?t=' + Date.now(); // 캐시 무력화
        card.hidden = false;
      }
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

// ── 방법 2: 실시간 카메라 ──
async function startCamera() {
  if (stream) stream.getTracks().forEach((t) => t.stop());
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    camErr.hidden = false;
    camErr.textContent = '이 브라우저에서는 실시간 카메라를 쓸 수 없습니다. 위 "사진 고르기"를 이용하세요.';
    return;
  }
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: facing },
      audio: false,
    });
    video.srcObject = stream;
    camErr.hidden = true;
  } catch (err) {
    camErr.hidden = false;
    camErr.textContent =
      '카메라 접근 실패: ' + err.message +
      ' (HTTP로 접속했다면 https:// 주소로 접속하고 인증서 경고를 허용하세요.)';
  }
}

document.getElementById('capture').addEventListener('click', () => {
  if (!stream) return;
  canvas.width = video.videoWidth;
  canvas.height = video.videoHeight;
  canvas.getContext('2d').drawImage(video, 0, 0);
  canvas.toBlob((blob) => blob && sendImage(blob), 'image/jpeg', 0.92);
});

document.getElementById('flip').addEventListener('click', () => {
  facing = facing === 'environment' ? 'user' : 'environment';
  startCamera();
});

startCamera();
