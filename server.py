"""
영수증 프린터 웹 서버 (라즈베리파이 / Debian Trixie)

- USB로 연결된 ESC/POS 영수증 프린터(CPP-3100, 80mm/576px)로 사진 출력
- 스마트폰은 라즈베리파이 핫스팟 와이파이에 접속 후 브라우저로 접근
- 실시간 카메라 촬영을 지원하기 위해 자체서명 인증서로 HTTPS 구동
"""

import io
import os
import glob

from flask import Flask, request, jsonify, render_template
from PIL import Image, ImageEnhance
import numpy as np
from escpos.capabilities import CAPABILITIES

# ── 설정 ──────────────────────────────────────────
# USB 프린터의 Vendor/Product ID. lsusb 로 확인 후 환경변수로 덮어쓸 수 있습니다.
#   예) export PRINTER_VID=0x0416 PRINTER_PID=0x5011
PRINTER_VID = int(os.environ.get("PRINTER_VID", "0"), 0)
PRINTER_PID = int(os.environ.get("PRINTER_PID", "0"), 0)

# USB가 시리얼(CDC)로 잡히는 프린터일 경우 사용할 시리얼 포트 후보
SERIAL_GLOBS = ["/dev/ttyUSB*", "/dev/ttyACM*"]
BAUDRATE = int(os.environ.get("PRINTER_BAUD", "9600"))

PROFILE = "CPP-3100"
PRINT_WIDTH = 576  # 80mm, 203dpi 기준 가로 픽셀 수

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "3001"))
CERT_FILE = os.environ.get("CERT_FILE", "cert.pem")
KEY_FILE = os.environ.get("KEY_FILE", "key.pem")

# ── 커스텀 프린터 프로파일 등록 (프린터 열기 전에 실행) ──
CAPABILITIES["profiles"][PROFILE] = {
    "name": PROFILE,
    "vendor": "Custom",
    "media": {"dpi": 203, "width": {"mm": 80, "pixels": PRINT_WIDTH}},
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
        "barcodeA": True,
        "barcodeB": True,
        "bitImageColumn": True,
        "bitImageRaster": True,
        "graphics": True,
        "highDensity": True,
        "paperFullCut": True,
        "paperPartCut": False,
        "pdf417Code": False,
        "pulseBel": False,
        "pulseStandard": True,
        "qrCode": True,
        "starCommands": False,
    },
}


# ── 프린터 열기 ────────────────────────────────────
def _find_usb_printer():
    """USB 장치 중 프린터 클래스(0x07)를 찾아 (vid, pid) 반환."""
    import usb.core

    for dev in usb.core.find(find_all=True):
        try:
            for cfg in dev:
                for intf in cfg:
                    if intf.bInterfaceClass == 7:  # USB Printer class
                        return dev.idVendor, dev.idProduct
        except Exception:
            continue
    return None


def open_printer():
    """USB → 시리얼 순으로 프린터 연결을 시도한다."""
    from escpos.printer import Usb, Serial

    vid, pid = PRINTER_VID, PRINTER_PID
    if not (vid and pid):
        found = _find_usb_printer()
        if found:
            vid, pid = found

    if vid and pid:
        print(f"USB 프린터 연결 시도: {vid:#06x}:{pid:#06x}")
        return Usb(vid, pid, profile=PROFILE)

    # USB 프린터 클래스로 안 잡히면 시리얼 포트 시도
    for pattern in SERIAL_GLOBS:
        for dev in sorted(glob.glob(pattern)):
            print(f"시리얼 프린터 연결 시도: {dev}")
            return Serial(
                devfile=dev,
                baudrate=BAUDRATE,
                bytesize=8,
                parity="N",
                stopbits=1,
                timeout=3,
                profile=PROFILE,
            )

    raise RuntimeError(
        "프린터를 찾지 못했습니다. USB 연결과 전원을 확인하고, "
        "lsusb 결과의 ID를 PRINTER_VID/PRINTER_PID 환경변수로 지정하세요."
    )


# ── 이미지 전처리 ──────────────────────────────────
def prepare_image(
    img: Image.Image,
    max_width: int = PRINT_WIDTH,
    brightness: float = 1.05,
    gamma: float = 1.8,
) -> Image.Image:
    # EXIF 회전 보정 (스마트폰 사진은 회전정보가 들어있는 경우가 많음)
    try:
        from PIL import ImageOps

        img = ImageOps.exif_transpose(img)
    except Exception:
        pass

    # 가로폭에 맞춰 리사이즈
    ratio = max_width / img.width
    img = img.resize((max_width, int(img.height * ratio)), Image.LANCZOS)

    # 그레이스케일 + 밝기 + 감마 보정
    img = img.convert("L")
    img = ImageEnhance.Brightness(img).enhance(brightness)
    arr = np.array(img, dtype=np.float32)
    arr = 255.0 * (arr / 255.0) ** (1.0 / gamma)
    img = Image.fromarray(arr.astype(np.uint8))

    # Floyd-Steinberg 디더링 → 1비트
    return img.convert("1")


def print_image(img: Image.Image):
    """매 출력마다 연결을 열고 닫아 USB 핸들 꼬임을 방지한다."""
    p = open_printer()
    try:
        img = prepare_image(img)
        p.set(align="center")
        p.image(img)
        p.set(align="left")
        p.cut()
    finally:
        try:
            p.close()
        except Exception:
            pass


# ── Flask ─────────────────────────────────────────
app = Flask(__name__)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/print", methods=["POST"])
def handle_print():
    if "image" not in request.files:
        return jsonify({"status": "error", "message": "이미지가 없습니다."}), 400
    try:
        file = request.files["image"]
        img = Image.open(io.BytesIO(file.read()))
        print_image(img)
        return jsonify({"status": "ok"})
    except Exception as e:
        app.logger.exception("출력 실패")
        return jsonify({"status": "error", "message": str(e)}), 500


def main():
    ssl_context = None
    if os.path.exists(CERT_FILE) and os.path.exists(KEY_FILE):
        ssl_context = (CERT_FILE, KEY_FILE)
        scheme = "https"
    else:
        scheme = "http"
        print(
            "⚠ 인증서가 없어 HTTP로 실행합니다. 실시간 카메라를 쓰려면 "
            "setup/gen-cert.sh 로 인증서를 만든 뒤 다시 실행하세요."
        )

    print(f"--- 영수증 프린터 서버 시작: {scheme}://<라즈베리파이 IP>:{PORT} ---")
    app.run(host=HOST, port=PORT, ssl_context=ssl_context)


if __name__ == "__main__":
    main()
