const statusEl = document.getElementById('status');
const printingEl = document.getElementById('printing');
const author = setupAuthorName();
const likes = setupLikes();
setupHashtags();

let statusTimer = null;
function showStatus(msg, kind) {
  statusEl.hidden = false;
  statusEl.textContent = msg;
  statusEl.className = 'status ' + (kind || '');
  clearTimeout(statusTimer);
  statusTimer = setTimeout(() => { statusEl.hidden = true; }, kind === 'err' ? 5000 : 2500);
}

// 서버로 이미지 전송 → 프린터 출력 (끝날 때까지 로딩 창)
async function sendImage(blob) {
  printingEl.hidden = false;
  const form = new FormData();
  form.append('image', blob, 'photo.jpg');
  form.append('name', author.get()); // 비워두면 서버가 기본 이름 사용
  form.append('likes', likes.get()); // 화면에 보인 좋아요 수 그대로 출력
  try {
    const res = await fetch('/print', { method: 'POST', body: form });
    const data = await res.json().catch(() => ({}));
    if (res.ok && data.status === 'ok') {
      showStatus('✅ 출력 완료!', 'ok');
      author.clear(); // 다음 사람을 위해 이름 비우기 (→ 기본 이름)
      likes.reroll(); // 다음 사진은 새 숫자
    } else {
      showStatus('❌ 출력 실패: ' + (data.message || res.status), 'err');
    }
  } catch (e) {
    showStatus('❌ 전송 실패: ' + e.message, 'err');
  } finally {
    printingEl.hidden = true;
  }
}

// 실시간 카메라 (camera.js): 촬영 누르면 3초 카운트다운 후 찍고 출력
setupCamera(sendImage, { countdown: 3 });
