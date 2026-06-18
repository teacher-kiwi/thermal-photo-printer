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
from PIL import Image, ImageOps
import numpy as np

# python-escpos는 실제 출력 시에만 필요. 목(mock) 모드에선 없어도 동작하도록 보호.
try:
    from escpos.capabilities import CAPABILITIES
    _HAS_ESCPOS = True
except Exception:
    CAPABILITIES = None
    _HAS_ESCPOS = False

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

# 목(mock) 모드: 프린터 없이 출력될 모습을 PNG로 저장해 미리보기. (개발용)
#   MOCK_PRINTER=1 ./venv/bin/python server.py
MOCK_PRINTER = os.environ.get("MOCK_PRINTER", "") not in ("", "0", "false", "False")
PREVIEW_DIR = os.path.join(os.path.dirname(__file__), "static", "preview")

# 디더링 방식: "fs"(Floyd-Steinberg, 기본) 또는 "atkinson"
#   DITHER=atkinson ./venv/bin/python server.py
DITHER = os.environ.get("DITHER", "fs")

# 오토 레벨: 사진 밝기에 맞춰 감마·밝기 자동 조절 (기본 켜짐). AUTO=0 이면 고정값 사용.
AUTO_LEVELS = os.environ.get("AUTO", "1") not in ("0", "false", "False", "")
# 퍼센타일 스트레칭 강도(%). 하위/상위 N%를 검정/흰색으로. 0이면 끔. (auto일 때 적용)
STRETCH = float(os.environ.get("STRETCH", "2"))

# ── 커스텀 프린터 프로파일 등록 (프린터 열기 전에 실행) ──
if _HAS_ESCPOS:
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


# ── 디더링 ────────────────────────────────────────
def atkinson_dither(img_l: Image.Image) -> Image.Image:
    """앳킨슨 디더링. 오차의 6/8만 확산(1/4 버림)해 점이 듬성하고 또렷하다.

    열전사 프린터는 점이 번져 붙는 특성이 있어, 빽빽한 Floyd-Steinberg보다
    점이 분리돼 남는 앳킨슨이 덜 뭉개지는 경우가 많다.
    """
    a = np.array(img_l, dtype=np.float32)
    h, w = a.shape
    for y in range(h):
        row = a[y]
        for x in range(w):
            old = row[x]
            new = 255.0 if old >= 128 else 0.0
            err = (old - new) / 8.0
            row[x] = new
            # 이웃 6곳에 1/8씩 확산 (나머지 2/8은 버림)
            if x + 1 < w:
                row[x + 1] += err
            if x + 2 < w:
                row[x + 2] += err
            if y + 1 < h:
                if x - 1 >= 0:
                    a[y + 1, x - 1] += err
                a[y + 1, x] += err
                if x + 1 < w:
                    a[y + 1, x + 1] += err
            if y + 2 < h:
                a[y + 2, x] += err
    # 처리 후 모든 픽셀이 0/255이므로 재디더링 없이 1비트로 변환
    return Image.fromarray(a >= 128)


# ── 오토 레벨 (사진별 동적 보정) ──────────────────
def auto_levels(
    arr: np.ndarray,
    target: float = 0.60,
    gamma_range=(1.0, 3.2),
    bright_range=(0.85, 1.4),
):
    """그레이스케일 배열(0~255)의 평균 밝기를 보고 감마·밝기를 자동 산출.

    - 어두운 사진일수록 감마를 크게(많이 들어올림), 밝은 사진은 작게.
    - 감마 적용 후 평균을 다시 재서, 목표 밝기에 맞춰 선형 밝기를 보정.
    반환: (gamma, brightness)
    """
    norm = arr / 255.0
    m = float(np.clip(norm.mean(), 0.02, 0.98))      # 원본 평균 (log 안정화 위해 클램프)

    # ① 평균을 목표로 보내는 감마:  target = m^(1/gamma)
    gamma = float(np.log(m) / np.log(target))
    gamma = float(np.clip(gamma, *gamma_range))

    # ② 감마 적용 후 평균을 재측정 → 목표까지 선형 밝기로 보정
    after_mean = float(np.clip((norm ** (1.0 / gamma)).mean(), 0.02, 1.0))
    brightness = float(np.clip(target / after_mean, *bright_range))
    return gamma, brightness


# ── 이미지 전처리 ──────────────────────────────────
def prepare_image(
    img: Image.Image,
    max_width: int = PRINT_WIDTH,
    brightness: float = 1.05,
    gamma: float = 1.8,
    dither: str = "fs",
    auto: bool = True,
    stretch: float = 2.0,
) -> Image.Image:
    # EXIF 회전 보정 (스마트폰 사진은 회전정보가 들어있는 경우가 많음)
    try:
        img = ImageOps.exif_transpose(img)
    except Exception:
        pass

    # 투명(알파) 배경은 흰색으로 합성 → 영수증에서 검게 찍히지 않고 빈 공간으로 출력
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        img = img.convert("RGBA")
        bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
        img = Image.alpha_composite(bg, img).convert("RGB")

    # 가로폭에 맞춰 리사이즈
    ratio = max_width / img.width
    img = img.resize((max_width, int(img.height * ratio)), Image.LANCZOS)

    # 그레이스케일
    img = img.convert("L")

    # ⓪ 퍼센타일 스트레칭: 하위/상위 stretch% 를 검정/흰색으로 매핑해 대비를 일정하게.
    #    히스토그램 기반이라 흰 배경 같은 아웃라이어에 강함. (auto일 때만)
    if auto and stretch > 0:
        img = ImageOps.autocontrast(img, cutoff=stretch)

    arr = np.array(img, dtype=np.float32)

    # auto면 스트레칭된 결과의 밝기에 맞춰 감마·밝기를 동적으로 산출, 아니면 고정값 사용
    if auto:
        gamma, brightness = auto_levels(arr)
        print(f"[auto] stretch={stretch}% gamma={gamma:.2f} brightness={brightness:.2f}")

    # ① 감마: 그림자/중간톤을 비선형으로 들어올림
    arr = 255.0 * (arr / 255.0) ** (1.0 / gamma)
    # ② 밝기: 전체를 선형으로 배수 (ImageEnhance.Brightness와 동일)
    arr = np.clip(arr * brightness, 0, 255)
    img = Image.fromarray(arr.astype(np.uint8))

    # 디더링 → 1비트
    if dither == "atkinson":
        return atkinson_dither(img)
    return img.convert("1")  # PIL 기본값 = Floyd-Steinberg


def save_preview(processed: Image.Image) -> str:
    """출력될 1비트 이미지를 영수증 모양 PNG로 저장하고 정적 URL을 반환."""
    import datetime

    os.makedirs(PREVIEW_DIR, exist_ok=True)

    # 흰 종이 위에 좌우 여백을 줘서 실제 영수증처럼 보이게
    margin = 24
    paper = Image.new("RGB", (processed.width + margin * 2, processed.height + margin * 2), "white")
    paper.paste(processed.convert("RGB"), (margin, margin))

    name = "receipt_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f") + ".png"
    paper.save(os.path.join(PREVIEW_DIR, name))
    # 최신본은 고정 이름으로도 저장 (빠른 확인용)
    paper.save(os.path.join(PREVIEW_DIR, "latest.png"))
    return "/static/preview/" + name


def print_image(img: Image.Image):
    """사진을 출력한다. MOCK_PRINTER면 인쇄 대신 미리보기 PNG를 만든다.

    실제 출력될 픽셀과 동일한 1비트 이미지를 미리보기로 저장하므로,
    이미지 처리(밝기/감마/디더링) 결과를 프린터 없이 확인할 수 있다.
    반환값: 목 모드면 미리보기 URL, 실제 출력이면 None.
    """
    processed = prepare_image(img, dither=DITHER, auto=AUTO_LEVELS, stretch=STRETCH)

    if MOCK_PRINTER:
        return save_preview(processed)

    p = open_printer()
    try:
        p.set(align="center")
        p.image(processed)
        p.set(align="left")
        p.cut()
    finally:
        try:
            p.close()
        except Exception:
            pass
    return None


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
        preview = print_image(img)
        return jsonify({"status": "ok", "preview": preview, "mock": MOCK_PRINTER})
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

    if MOCK_PRINTER:
        print("🧪 목(mock) 모드: 실제 인쇄 대신 static/preview/ 에 미리보기 PNG를 만듭니다.")
    print(f"--- 영수증 프린터 서버 시작: {scheme}://<라즈베리파이 IP>:{PORT} ---")
    app.run(host=HOST, port=PORT, ssl_context=ssl_context)


if __name__ == "__main__":
    main()
