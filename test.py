from flask import Flask, request, jsonify, render_template
import io
import subprocess
import time
from PIL import Image, ImageEnhance
import numpy as np
from escpos.printer import Serial
from escpos.capabilities import CAPABILITIES

# ── 설정 ──────────────────────────────────────────
PRINTER_MAC = "DC:1D:30:02:CA:75"
PORT        = "/dev/cu.CPP-3100_CA75"
BAUDRATE    = 9600

# CAPABILITIES에 커스텀 프로파일 등록 (open_printer 호출 전에 실행)
CAPABILITIES["profiles"]["CPP-3100"] = {
    "name": "CPP-3100",
    "vendor": "Custom",
    "media": {
        "dpi": 203,
        "width": {
            "mm": 80,
            "pixels": 576
        }
    },
    "fonts": {
        "0": {"name": "Font A", "columns": 48},
        "1": {"name": "Font B", "columns": 64},
    },
    "codePages": {
        "0": "CP437",
        "16": "CP1252",
        "17": "CP866",
        "45": "CP1250",
        "46": "CP1251",
    },
    "colors": {"0": "black"},
    "features": {
        "barcodeA":       True,
        "barcodeB":       True,
        "bitImageColumn": True,
        "bitImageRaster": True,
        "graphics":       True,
        "highDensity":    True,
        "paperFullCut":   True,
        "paperPartCut":   False,
        "pdf417Code":     False,
        "pulseBel":       False,
        "pulseStandard":  True,
        "qrCode":         True,
        "starCommands":   False,
    },
}


# ── 블루투스 헬퍼 ──────────────────────────────────
def bt_is_connected() -> bool:
    result = subprocess.run(
        ["blueutil", "--is-connected", PRINTER_MAC],
        capture_output=True, text=True,
    )
    return result.stdout.strip() == "1"


def bt_reset():
    print("블루투스 데몬 재시작 중...")
    subprocess.run(["sudo", "pkill", "bluetoothd"], capture_output=True)
    time.sleep(3)
    print("완료")


def bt_connect(timeout: int = 15) -> bool:
    print("BT 연결 시도 중...", end="", flush=True)
    subprocess.run(["blueutil", "--connect", PRINTER_MAC], capture_output=True)
    for _ in range(timeout):
        if bt_is_connected():
            print(" 연결됨!")
            return True
        print(".", end="", flush=True)
        time.sleep(1)
    print(" 타임아웃")
    return False


# ── 프린터 열기 / 닫기 ────────────────────────────

def open_printer() -> Serial:
    if not bt_is_connected():
        bt_reset()
        bt_connect()

    # python-escpos Serial 프린터
    # devfile  : 시리얼 포트 경로
    # baudrate : 통신 속도
    # profile  : ESC/POS 프로파일 (기본값 'default' 사용)
    p = Serial(
        devfile=PORT,
        baudrate=BAUDRATE,
        bytesize=8,
        parity="N",
        stopbits=1,
        timeout=3,
        dsrdtr=True,   # DTR 활성화 (원본 ser.dtr = True)
        profile="CPP-3100",
    )
    time.sleep(0.5)
    return p


def close_printer(p: Serial):
    time.sleep(0.5)
    p.close()
    time.sleep(1)

    if not bt_is_connected():
        bt_reset()


    # return img
def prepare_image(
    img: Image.Image,
    max_width: int = 576,
    brightness: float = 1.05,
    gamma: float = 1.8,
) -> Image.Image:

    # 리사이즈
    ratio = max_width / img.width
    img = img.resize((max_width, int(img.height * ratio)), Image.LANCZOS)

    # 그레이스케일
    img = img.convert("L")

    # 밝기 조정
    img = ImageEnhance.Brightness(img).enhance(brightness)

    # 감마 보정 — 어두운 픽셀일수록 더 많이 밝아짐
    arr = np.array(img, dtype=np.float32)
    arr = 255.0 * (arr / 255.0) ** (1.0 / gamma)
    img = Image.fromarray(arr.astype(np.uint8))

    # Floyd-Steinberg 디더링
    img = img.convert("1")

    return img    


# ── 출력 함수 ─────────────────────────────────────
def print_image(p: Serial, img: Image.Image):
    print("이미지 변환 중...")
    img = prepare_image(img)
    print("변환 완료...")
    p.set(align="center")   # 가운데 정렬
    p.image(img)            # python-escpos 가 GS v 0 으로 전송
    print("이미지 전송...")
    p.set(align="left")     # 정렬 초기화
    p.cut()                 # 커터
    print("출력 완료!")


def print_text(p: Serial, text: str):
    p.set(align="left")
    # python-escpos 는 내부적으로 인코딩을 처리하므로
    # codepage 설정이 필요하면 profile 로 지정하거나 아래처럼 직접 전달
    p.text(text + "\n\n\n\n")
    p.ln(4)


app = Flask(__name__)
p = None

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/print', methods=['POST'])
def handle_print():
    try:
        file = request.files['image']
        img = Image.open(io.BytesIO(file.read()))
        print_image(p, img)
        return jsonify({'status': 'ok'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500
    
# ── 메인 ─────────────────────────────────────────
if __name__ == "__main__":
    print("--- 블루투스 프린터 (python-escpos) ---")
    try:
        p = open_printer()
        print("✅ 연결 완료!")

        app.run(host='0.0.0.0', port=3001) 

    except Exception as e:
        print(f"에러: {e}")
    finally:
        if p is not None:
            close_printer(p)
            print("--- 종료 완료 ---")

