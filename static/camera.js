// 실시간 카메라 (메인 페이지 / 관리 페이지 공통)
// 페이지에 #video, #canvas, #capture, #flip, #camErr 요소가 있어야 한다.
// onCapture(blob): 촬영한 JPEG 를 받아 처리할 함수
function setupCamera(onCapture) {
  const video = document.getElementById('video');
  const canvas = document.getElementById('canvas');
  const camErr = document.getElementById('camErr');
  let facing = 'environment';
  let stream = null;

  function showError(msg) {
    camErr.hidden = false;
    camErr.textContent = msg;
  }

  async function startCamera() {
    if (stream) stream.getTracks().forEach((t) => t.stop());
    stream = null;
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      showError('이 브라우저에서는 실시간 카메라를 쓸 수 없습니다. 위 "사진 고르기"를 이용하세요.');
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
      showError(
        '카메라 접근 실패: ' + err.message +
        ' (HTTP로 접속했다면 https:// 주소로 접속하고 인증서 경고를 허용하세요.)'
      );
    }
  }

  document.getElementById('capture').addEventListener('click', () => {
    if (!stream || !video.videoWidth) {
      showError('카메라가 아직 준비되지 않았습니다. 잠시 후 다시 눌러주세요.');
      return;
    }
    camErr.hidden = true;
    canvas.width = video.videoWidth;
    canvas.height = video.videoHeight;
    canvas.getContext('2d').drawImage(video, 0, 0);
    canvas.toBlob((blob) => blob && onCapture(blob), 'image/jpeg', 0.92);
  });

  document.getElementById('flip').addEventListener('click', () => {
    facing = facing === 'environment' ? 'user' : 'environment';
    startCamera();
  });

  startCamera();
}
