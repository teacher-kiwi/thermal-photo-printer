# 영수증 프린터 사진 출력 (라즈베리파이)

스마트폰을 라즈베리파이 **핫스팟 와이파이**에 연결 → 브라우저에서 사진을 고르거나 카메라로 찍으면
USB로 연결된 **ESC/POS 영수증 프린터(CPP-3100, 80mm)** 로 출력됩니다.

```
[스마트폰] ──와이파이(핫스팟)──> [라즈베리파이 + Flask 서버] ──USB──> [영수증 프린터]
```

## 구성 파일
| 파일 | 설명 |
|------|------|
| `server.py` | Flask 웹서버 + 이미지 보정·디더링 + USB 프린터 출력 |
| `templates/index.html`, `static/app.js` | 태블릿 웹 UI (사진 선택 + 실시간 카메라) |
| `templates/admin.html`, `static/admin.js` | 보정값 조절 페이지 (`/admin`) |
| `static/camera.js`, `static/style.css` | 두 페이지 공통 (실시간 카메라, 스타일) |
| `print_test.py` | 웹서버 없이 프린터 연결만 테스트 |
| `preview.py` | 명령줄에서 출력 미리보기 PNG 생성 |
| `setup/hotspot.sh` | 와이파이 핫스팟 켜기 (NetworkManager) |
| `setup/gen-cert.sh` | 실시간 카메라용 HTTPS 자체서명 인증서 생성 |
| `setup/receipt-printer.service` | 부팅 시 서버 자동 실행 (systemd) |
| `setup/99-escpos-printer.rules` | USB 프린터 권한 부여 (udev) |

---

## 라즈베리파이에서 설치 (한 번만)

> 기존 `venv/` 폴더는 맥에서 만든 것이라 파이에서 동작하지 않습니다. 새로 만드세요.

```bash
cd /app

# 1) 시스템 패키지 (libusb: USB 직접 접근용)
sudo apt update
sudo apt install -y python3-venv libusb-1.0-0

# 2) 파이썬 가상환경 + 의존성
rm -rf venv
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

### 프린터 인식 확인
```bash
lsusb        # 예) Bus 001 Device 005: ID 0416:5011 Custom CPP-3100
```
- 위처럼 `ID xxxx:yyyy` 가 보이면 **USB 프린터**입니다.
  - `setup/99-escpos-printer.rules` 의 `idVendor`/`idProduct` 를 그 값으로 고치고 설치하세요.
- `lsusb` 에 안 보이고 `ls /dev/ttyUSB* /dev/ttyACM*` 에 잡히면 **시리얼 프린터**입니다.
  - `sudo usermod -aG dialout pi` 후 재로그인하면 됩니다. (서버가 자동으로 시리얼도 시도합니다)

```bash
# USB 권한 규칙 적용 (USB 프린터인 경우)
sudo cp setup/99-escpos-printer.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger
# 프린터 USB 를 뽑았다 다시 꽂기
```

### HTTPS 인증서 생성 (실시간 카메라를 쓰려면 필수)
```bash
./setup/gen-cert.sh 10.42.0.1
```
> 인증서가 없으면 서버는 HTTP로 뜨고, "사진 고르기"는 되지만 페이지 내 실시간 카메라는 차단됩니다.

---

## 핫스팟 켜기
```bash
sudo ./setup/hotspot.sh ReceiptPi print1234
#                        └ SSID    └ 비밀번호(8자 이상)
```
- 스마트폰 와이파이에서 `ReceiptPi` 에 접속 → 게이트웨이 IP는 `10.42.0.1`
- 핫스팟은 부팅 시 자동으로 켜지도록 설정됩니다.

> 핫스팟을 켜면 파이 자체의 인터넷 와이파이는 끊깁니다(무선 어댑터가 1개라서).
> 인터넷이 동시에 필요하면 유선 랜이나 USB 와이파이 동글을 추가하세요.

---

## 서버 실행

### 테스트 실행
```bash
./venv/bin/python server.py
```

### 자동 실행 등록 (부팅 시 시작)
```bash
sudo cp setup/receipt-printer.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now receipt-printer
journalctl -u receipt-printer -f      # 로그 확인
```

---

## 사용
1. 스마트폰을 핫스팟 `ReceiptPi` 에 연결
2. 브라우저에서 **`https://10.42.0.1:3001`** 접속
3. 처음 한 번 "안전하지 않음/인증서 경고"를 **허용** (자체서명 인증서라 정상)
4. "사진 고르기" 로 갤러리/카메라 선택, 또는 "실시간 카메라"로 촬영 → 영수증 출력

---

## 보정값 조절 (`/admin`)

브라우저에서 **`https://10.42.0.1:3001/admin`** 에 접속하면 출력하지 않고 화면으로만 결과를 보면서
밝기·감마·디더링 같은 보정값을 조절할 수 있습니다.

1. "사진 고르기 / 촬영"으로 테스트 사진을 올리거나, **실시간 카메라**에서 "촬영해서 미리보기"
   (메인 페이지와 같은 방식으로 촬영하지만 출력은 하지 않음. HTTPS 필요)
2. 슬라이더를 움직이면 오른쪽에 **영수증에 실제로 찍힐 1비트 이미지**가 바로 갱신됨
   (출력 미리보기를 누르면 실제 픽셀 크기로 확대)
3. 마음에 들면 **저장** → 다음 출력부터 적용

| 항목 | 설명 |
|------|------|
| 자동 보정 | 켜면 사진마다 감마·밝기를 자동 계산 (목표 밝기, 대비 늘리기로 조절) |
| 밝기 / 감마 | 자동 보정을 끄면 이 고정값을 사용 |
| 샤프닝 | 디더링 전에 윤곽을 살림 (0이면 끔) |
| 디더링 | Floyd-Steinberg(기본) / 블루 노이즈 / Atkinson / JJN / Stucki / Sierra |

- 저장값은 `settings.json` 에 보관됩니다. 기기마다 다른 값이라 git 에는 올라가지 않습니다(`.gitignore`).
- 맥에서도 `./venv/bin/python server.py` 로 띄우고 `http://localhost:3001/admin` 에서 똑같이 쓸 수 있습니다.
  (프린터 없이 미리보기만 됨)

### 명령 한 줄로 미리보기
```bash
./venv/bin/python preview.py 사진.jpg                       # 사진_preview_fs_auto.png 생성
./venv/bin/python preview.py 사진.jpg --no-auto --brightness 1.2 --gamma 2.2
```

## 문제 해결
| 증상 | 확인 |
|------|------|
| `프린터를 찾지 못했습니다` | `lsusb` 확인 → `PRINTER_VID`/`PRINTER_PID` 환경변수나 udev 규칙 설정 |
| 권한 오류(USB) | udev 규칙 적용 + USB 재연결, 또는 서비스를 `User=root` 로 변경 |
| 카메라가 안 켜짐 | `http://` 가 아니라 `https://` 로 접속했는지, 인증서 경고를 허용했는지 확인 |
| 출력이 너무 진하다/연하다 | `/admin` 에서 보정값 조절 후 저장 |
| 사진이 옆으로 누움 | EXIF 자동 회전 처리됨. 그래도 이상하면 촬영 방향 확인 |
