// 실시간 카메라 + 작성자 이름 (메인 페이지 / 관리 페이지 공통)
// 페이지에 _post.html 과 #canvas, #capture, #camErr 요소가 있어야 한다.

const PHOTO_ASPECT = 4 / 3; // 가로 4 : 세로 3. style.css 의 .post video 비율과 같아야 함

// onCapture(blob): 촬영한 JPEG 를 받아 처리할 함수. Promise 를 돌려주면 끝날 때까지 촬영 버튼을 막음
// options.countdown: 촬영 전 카운트다운 초 (기본 0 = 바로 촬영). 숫자는 화면 가운데(#countdown)에 표시
// 카메라 방향은 <video data-facing> (관리 페이지 설정)으로 시작.
// 반환값: { setFacing('environment' | 'user') }  — 관리 페이지에서 설정을 바꿀 때 사용
function setupCamera(onCapture, options = {}) {
  const video = document.getElementById('video');
  const canvas = document.getElementById('canvas');
  const camErr = document.getElementById('camErr');
  const captureBtn = document.getElementById('capture');
  const countdownEl = document.getElementById('countdown');
  const countdownSec = options.countdown || 0;
  let facing = video.dataset.facing || 'environment';
  let stream = null;

  function showError(msg) {
    camErr.hidden = false;
    camErr.textContent = msg;
  }

  async function startCamera() {
    if (stream) stream.getTracks().forEach((t) => t.stop());
    stream = null;
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      showError('카메라를 쓸 수 없습니다. https:// 주소로 접속했는지 확인하세요.');
      return;
    }
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        video: { facingMode: facing, aspectRatio: { ideal: PHOTO_ASPECT } },
        audio: false,
      });
      video.srcObject = stream;
      // 전면(셀카)은 거울처럼 화면만 좌우 반전. 촬영·출력은 실제 방향 그대로
      video.classList.toggle('mirror', facing === 'user');
      camErr.hidden = true;
    } catch (err) {
      showError(
        '카메라 접근 실패: ' + err.message +
        ' (HTTP로 접속했다면 https:// 주소로 접속하고 인증서 경고를 허용하세요.)'
      );
    }
  }

  function cameraReady() {
    if (stream && video.videoWidth) return true;
    showError('카메라가 아직 준비되지 않았습니다. 잠시 후 다시 눌러주세요.');
    return false;
  }

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  // 화면 가운데에 3, 2, 1 … 을 1초씩 보여줌
  async function countdown(sec) {
    for (let n = sec; n > 0; n--) {
      countdownEl.textContent = n;
      countdownEl.hidden = false;
      countdownEl.classList.remove('tick');
      void countdownEl.offsetWidth; // 애니메이션 다시 시작
      countdownEl.classList.add('tick');
      await sleep(1000);
    }
    countdownEl.hidden = true;
  }

  // 화면(object-fit: cover)에 보이는 가운데 4:3 영역만 잘라서 JPEG 로
  function grabFrame() {
    const vw = video.videoWidth;
    const vh = video.videoHeight;
    let sw = vw;
    let sh = vh;
    if (vw / vh > PHOTO_ASPECT) sw = Math.round(vh * PHOTO_ASPECT);
    else sh = Math.round(vw / PHOTO_ASPECT);
    canvas.width = sw;
    canvas.height = sh;
    canvas.getContext('2d').drawImage(video, (vw - sw) / 2, (vh - sh) / 2, sw, sh, 0, 0, sw, sh);
    return new Promise((resolve) => canvas.toBlob(resolve, 'image/jpeg', 0.92));
  }

  let busy = false;
  captureBtn.addEventListener('click', async () => {
    if (busy || !cameraReady()) return;
    busy = true;
    captureBtn.disabled = true;
    camErr.hidden = true;
    try {
      if (countdownSec > 0) await countdown(countdownSec);
      if (!cameraReady()) return; // 카운트다운 중 카메라가 꺼진 경우
      const blob = await grabFrame();
      if (blob) await onCapture(blob);
    } finally {
      countdownEl.hidden = true;
      busy = false;
      captureBtn.disabled = false;
    }
  });

  startCamera();

  return {
    setFacing(f) {
      if (f === facing) return;
      facing = f;
      startCamera();
    },
  };
}

// 작성자 이름 입력칸.
// 비워두면 기본 이름(관리 페이지에서 저장)이 흐린 글씨로 보이고 그대로 출력된다.
// onChange(name): 이름이 바뀔 때 호출 (선택).
// 반환값: { get(): 입력한 이름, clear(): 이름 지우기, setDefault(name): 기본 이름 변경 }
function setupAuthorName(onChange) {
  const input = document.getElementById('authorName');

  input.addEventListener('input', () => {
    if (onChange) onChange(input.value.trim());
  });
  // 입력 끝나면 키보드 닫기
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter') input.blur(); });

  return {
    get: () => input.value.trim(),
    clear() {
      input.value = '';
      if (onChange) onChange('');
    },
    setDefault(name) {
      input.placeholder = name || '이름 입력';
    },
  };
}

// 좋아요 문구 "ㅇㅇ님 외 N명이 좋아합니다" (_post.html #likesLine)
// N은 범위 안에서 무작위. 화면에 보인 N을 출력 요청에 같이 보내 출력물과 같게 한다.
// 반환값: { get(): 현재 N, reroll(): 새로 뽑기, setConfig(name, min, max) }
function setupLikes() {
  const el = document.getElementById('likesLine');
  let name = el.dataset.name || '';
  let min = parseInt(el.dataset.min, 10) || 0;
  let max = parseInt(el.dataset.max, 10) || 0;
  let n = 0;

  // 이름과 숫자만 굵게 (server.py likes_runs 와 같은 형식)
  function bold(text) {
    const b = document.createElement('b');
    b.textContent = text;
    return b;
  }

  function render() {
    const count = n.toLocaleString('ko-KR');
    if (name) el.replaceChildren(bold(name), '님 외 ', bold(count), '명이 좋아합니다');
    else el.replaceChildren(bold(count), '명이 좋아합니다');
  }

  function reroll() {
    const lo = Math.min(min, max);
    const hi = Math.max(min, max);
    n = lo + Math.floor(Math.random() * (hi - lo + 1));
    render();
  }
  reroll();

  return {
    get: () => n,
    reroll,
    setConfig(newName, newMin, newMax) {
      name = newName;
      min = newMin;
      max = newMax;
      // 범위가 바뀌어 지금 숫자가 벗어나면 새로 뽑기
      if (n < Math.min(min, max) || n > Math.max(min, max)) reroll();
      else render();
    },
  };
}

// 해시태그 (_post.html #tagsBox). server.py hashtag_lines 와 같은 규칙:
// per-line 개씩 한 줄로 묶고, 화면 폭이 모자라면 태그 "사이"에서만 줄바꿈 (태그 중간은 안 끊김)
// 반환값: { setConfig(tags, perLine) }
function setupHashtags() {
  const el = document.getElementById('tagsBox');
  let tags = [];
  let perLine = 3;
  try { tags = JSON.parse(el.dataset.tags || '[]'); } catch (e) { tags = []; }
  perLine = parseInt(el.dataset.perLine, 10) || 3;

  function render() {
    el.replaceChildren();
    for (let i = 0; i < tags.length; i += perLine) {
      const line = document.createElement('div');
      line.className = 'tag-line';
      for (const t of tags.slice(i, i + perLine)) {
        const span = document.createElement('span');
        span.className = 'tag';
        span.textContent = '#' + t;
        line.append(span);
      }
      el.append(line);
    }
    el.hidden = tags.length === 0;
  }
  render();

  return {
    setConfig(newTags, newPerLine) {
      tags = newTags;
      perLine = Math.max(1, newPerLine || 1);
      render();
    },
  };
}
