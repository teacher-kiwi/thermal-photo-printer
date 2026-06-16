# 영수증 프린터 사진 출력 (라즈베리파이)

스마트폰을 라즈베리파이 **핫스팟 와이파이**에 연결 → 브라우저에서 사진을 고르거나 카메라로 찍으면
USB로 연결된 **ESC/POS 영수증 프린터(CPP-3100, 80mm)** 로 출력됩니다.

```
[스마트폰] ──와이파이(핫스팟)──> [라즈베리파이 + Flask 서버] ──USB──> [영수증 프린터]
```

## 구성 파일
| 파일 | 설명 |
|------|------|
| `server.py` | Flask 웹서버 + USB 프린터 출력 |
| `templates/index.html`, `static/` | 스마트폰 웹 UI (사진 선택 + 실시간 카메라) |
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

## 개발 / 미리보기 (프린터·파이 없이)

페이지 UI나 출력물(밝기·감마·디더링)을 **노트북에서 미리 보며 수정**할 수 있습니다.
영수증에 찍히는 건 `prepare_image()`가 만드는 576px 1비트 이미지라서, 그걸 PNG로 보면 실제 출력물과 거의 동일합니다.

### 1) 목(mock) 모드로 웹서버 실행
```bash
MOCK_PRINTER=1 ./venv/bin/python server.py
```
- 브라우저로 `http://localhost:3001` 접속 (목 모드는 HTTP로도 충분)
- 사진을 고르고 "출력"을 누르면 인쇄 대신 **"출력 미리보기"** 이미지가 페이지에 표시됨
- 결과 PNG는 `static/preview/` 에 저장 (최신본은 `static/preview/latest.png`)

### 2) 명령 한 줄로 빠르게 튜닝
```bash
./venv/bin/python preview.py 사진.jpg                       # 사진_preview.png 생성
./venv/bin/python preview.py 사진.jpg --brightness 1.2 --gamma 2.2
```
마음에 드는 `--brightness`/`--gamma` 값을 찾았으면, `server.py` 의 `prepare_image()` 기본값에 반영하세요.

> 목 모드는 `python-escpos`/USB 없이도 돌아갑니다. UI·이미지 처리만 손볼 땐 이 모드로 반복하고,
> 다 됐을 때 파이에 올려 실제 출력으로 최종 확인하면 됩니다.

## 문제 해결
| 증상 | 확인 |
|------|------|
| `프린터를 찾지 못했습니다` | `lsusb` 확인 → `PRINTER_VID`/`PRINTER_PID` 환경변수나 udev 규칙 설정 |
| 권한 오류(USB) | udev 규칙 적용 + USB 재연결, 또는 서비스를 `User=root` 로 변경 |
| 카메라가 안 켜짐 | `http://` 가 아니라 `https://` 로 접속했는지, 인증서 경고를 허용했는지 확인 |
| 출력이 너무 진하다/연하다 | `server.py` 의 `prepare_image` 에서 `brightness`, `gamma` 값 조정 |
| 사진이 옆으로 누움 | EXIF 자동 회전 처리됨. 그래도 이상하면 촬영 방향 확인 |
