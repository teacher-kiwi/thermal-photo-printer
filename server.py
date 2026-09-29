"""
영수증 프린터 웹 서버 (라즈베리파이 / Debian Trixie)

- USB로 연결된 ESC/POS 영수증 프린터(CPP-3100, 80mm/576px)로 사진 출력
- 스마트폰은 라즈베리파이 핫스팟 와이파이에 접속 후 브라우저로 접근
- 실시간 카메라 촬영을 지원하기 위해 자체서명 인증서로 HTTPS 구동
"""

import io
import os
import glob
import json
import uuid
import base64
import threading
from functools import lru_cache
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

from flask import Flask, request, jsonify, render_template
from PIL import Image, ImageOps, ImageFilter
import numpy as np

# python-escpos는 실제 출력 시에만 필요. 없어도 /admin 미리보기는 동작하도록 보호.
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

# ── 이미지 보정 설정 (/admin 에서 조절 후 저장) ────
# 저장값은 settings.json 에 보관. 기기마다 다르므로 git 에는 올리지 않는다.
SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")

DEFAULT_SETTINGS = {
    "auto": True,        # 오토 레벨: 사진 밝기에 맞춰 감마·밝기 자동 산출
    "target": 0.60,      # (auto) 목표 평균 밝기 0~1. 높을수록 밝게 출력
    "stretch": 2.0,      # (auto) 퍼센타일 스트레칭 %. 하위/상위 N%를 검정/흰색으로. 0이면 끔
    "brightness": 1.05,  # (수동) 선형 밝기 배수
    "gamma": 1.8,        # (수동) 감마. 클수록 그림자/중간톤이 밝아짐
    "sharpen": 0.0,      # 디더링 전 샤프닝 강도 %. 0이면 끔 (80~150 정도가 무난)
    "dither": "fs",      # 디더링 방식 (DITHER_MODES 참고)
}

# 설정값 허용 범위 (최소, 최대) — 관리 페이지 슬라이더 범위와 동일
SETTING_RANGES = {
    "target": (0.30, 0.85),
    "stretch": (0.0, 10.0),
    "brightness": (0.5, 2.0),
    "gamma": (0.5, 4.0),
    "sharpen": (0.0, 300.0),
}
DITHER_MODES = ("fs", "atkinson", "jjn", "stucki", "sierra", "bluenoise")


def clean_settings(data) -> dict:
    """입력값을 검증해 허용 범위 안의 완전한 설정 dict로 만든다. 빠진 값은 기본값."""
    s = dict(DEFAULT_SETTINGS)
    if not isinstance(data, dict):
        return s
    if "auto" in data:
        s["auto"] = bool(data["auto"])
    for key, (lo, hi) in SETTING_RANGES.items():
        if key in data:
            try:
                s[key] = min(max(float(data[key]), lo), hi)
            except (TypeError, ValueError):
                pass
    if data.get("dither") in DITHER_MODES:
        s["dither"] = data["dither"]
    return s


def load_settings() -> dict:
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            return clean_settings(json.load(f))
    except FileNotFoundError:
        return dict(DEFAULT_SETTINGS)
    except Exception as e:
        print(f"⚠ settings.json 을 읽지 못해 기본값을 사용합니다: {e}")
        return dict(DEFAULT_SETTINGS)


def save_settings(s: dict):
    tmp = SETTINGS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SETTINGS_FILE)  # 저장 도중 꺼져도 파일이 깨지지 않게


# 현재 출력에 쓰이는 설정. 저장 시 dict 통째로 교체하므로 읽는 쪽은 잠금 불필요.
settings = load_settings()

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
# 오차 확산 커널: (나눌 값, [(dy, dx, 가중치), ...])  — 현재 픽셀 기준 오른쪽/아래로만 퍼뜨림
ERROR_KERNELS = {
    # 오차의 6/8만 확산(1/4 버림) → 점이 듬성하고 또렷, 대비 강함
    "atkinson": (8, [
        (0, 1, 1), (0, 2, 1),
        (1, -1, 1), (1, 0, 1), (1, 1, 1),
        (2, 0, 1),
    ]),
    # Jarvis-Judice-Ninke: 3줄에 넓게 퍼뜨려 계조가 부드러움
    "jjn": (48, [
        (0, 1, 7), (0, 2, 5),
        (1, -2, 3), (1, -1, 5), (1, 0, 7), (1, 1, 5), (1, 2, 3),
        (2, -2, 1), (2, -1, 3), (2, 0, 5), (2, 1, 3), (2, 2, 1),
    ]),
    # Stucki: JJN과 비슷하지만 조금 더 선명
    "stucki": (42, [
        (0, 1, 8), (0, 2, 4),
        (1, -2, 2), (1, -1, 4), (1, 0, 8), (1, 1, 4), (1, 2, 2),
        (2, -2, 1), (2, -1, 2), (2, 0, 4), (2, 1, 2), (2, 2, 1),
    ]),
    # Sierra(3줄): JJN과 비슷한 품질, 탭이 조금 적음
    "sierra": (32, [
        (0, 1, 5), (0, 2, 3),
        (1, -2, 2), (1, -1, 4), (1, 0, 5), (1, 1, 4), (1, 2, 2),
        (2, -1, 2), (2, 0, 3), (2, 1, 2),
    ]),
}


def error_diffusion(img_l: Image.Image, kernel: str) -> Image.Image:
    """오차 확산 디더링 (numpy, 대각선 단위 병렬 처리).

    픽셀을 하나씩 도는 대신 x + 3y 가 같은 픽셀들을 한 번에 처리한다.
    커널은 오른쪽 2칸 / 아래 2줄(좌우 2칸)까지만 퍼뜨리므로, 어떤 픽셀에 오차를 주는
    픽셀은 항상 x + 3y 값이 더 작다 → 앞 단계에서 이미 끝나 있음.
    그래서 결과는 한 픽셀씩 순서대로 처리한 것과 같고, 반복 횟수만
    (가로×세로) → (가로 + 3×세로) 로 줄어든다.
    """
    div, taps = ERROR_KERNELS[kernel]
    taps = [(dy, dx, wt / div) for dy, dx, wt in taps]

    a = np.asarray(img_l, dtype=np.float32)
    h, w = a.shape
    pad = 2  # 가장자리 밖으로 나가는 오차를 받아서 버릴 여백
    buf = np.zeros((h + pad, w + 2 * pad), dtype=np.float32)
    buf[:h, pad:pad + w] = a
    out = np.zeros((h, w), dtype=bool)

    rows = np.arange(h)
    for t in range(w + 3 * (h - 1)):
        y0 = max(0, -(-(t - w + 1) // 3))  # ceil((t - w + 1) / 3)
        y1 = min(h - 1, t // 3)
        ys = rows[y0:y1 + 1]
        xs = t - 3 * ys + pad

        v = buf[ys, xs]
        on = v >= 128
        out[ys, xs - pad] = on
        err = v - np.where(on, 255.0, 0.0).astype(np.float32)
        for dy, dx, wt in taps:
            buf[ys + dy, xs + dx] += err * wt

    return Image.fromarray(out)


@lru_cache(maxsize=1)
def blue_noise_mask(n: int = 64, sigma: float = 1.5, seed: int = 7) -> np.ndarray:
    """void-and-cluster 방식으로 n×n 블루 노이즈 임계값 마스크(0~255)를 만든다.

    처음 한 번만 계산하고(라즈베리파이에서 수 초) 이후엔 캐시를 쓴다.
    """
    N = n * n
    rng = np.random.default_rng(seed)

    # (0,0) 중심의 가우시안. 가장자리가 반대편과 이어지도록(타일링) 거리를 계산
    d = np.minimum(np.arange(n), n - np.arange(n)).astype(np.float64)
    kern = np.exp(-(d[:, None] ** 2 + d[None, :] ** 2) / (2 * sigma ** 2))

    def splat(E, idx, sign):
        y, x = divmod(int(idx), n)
        E += sign * np.roll(kern, (y, x), axis=(0, 1))

    def tightest_cluster(B, E):  # 점(1)들 중 주변이 가장 빽빽한 곳
        return int(np.where(B, E.ravel(), -np.inf).argmax())

    def largest_void(B, E):  # 빈칸(0)들 중 주변이 가장 비어 있는 곳
        return int(np.where(B, np.inf, E.ravel()).argmin())

    # ① 초기 패턴: 무작위 10% → 가장 빽빽한 점을 가장 빈 곳으로 옮기며 고르게 정리
    B = np.zeros(N, dtype=bool)
    B[rng.choice(N, N // 10, replace=False)] = True
    E = np.zeros((n, n))
    for i in np.flatnonzero(B):
        splat(E, i, +1)
    for _ in range(N):
        c = tightest_cluster(B, E)
        B[c] = False
        splat(E, c, -1)
        v = largest_void(B, E)
        B[v] = True
        splat(E, v, +1)
        if v == c:
            break

    rank = np.zeros(N, dtype=np.int64)
    proto_B, proto_E, ones = B.copy(), E.copy(), int(B.sum())

    # ② 초기 점들에 순위 매기기: 빽빽한 곳부터 빼면서 큰 순위 → 작은 순위
    for r in range(ones - 1, -1, -1):
        c = tightest_cluster(B, E)
        B[c] = False
        splat(E, c, -1)
        rank[c] = r

    # ③ 나머지 빈칸 채우기: 가장 빈 곳부터 채우면서 순위 부여
    B, E = proto_B, proto_E
    for r in range(ones, N):
        v = largest_void(B, E)
        B[v] = True
        splat(E, v, +1)
        rank[v] = r

    return ((rank.reshape(n, n) + 0.5) / N * 255.0).astype(np.float32)


def blue_noise_dither(img_l: Image.Image) -> Image.Image:
    """블루 노이즈 디더링: 마스크를 바둑판처럼 깔고 픽셀 값과 비교만 한다 (매우 빠름).

    점이 고르게 흩어져 FS의 벌레 모양 무늬가 없고, 열전사 번짐에도 강하다.
    """
    a = np.asarray(img_l, dtype=np.float32)
    h, w = a.shape
    mask = blue_noise_mask()
    n = mask.shape[0]
    tiled = np.tile(mask, (-(-h // n), -(-w // n)))[:h, :w]
    return Image.fromarray(a > tiled)


def dither_image(img_l: Image.Image, mode: str) -> Image.Image:
    """그레이스케일 → 1비트."""
    if mode in ERROR_KERNELS:
        return error_diffusion(img_l, mode)
    if mode == "bluenoise":
        return blue_noise_dither(img_l)
    return img_l.convert("1")  # "fs": PIL 기본값 = Floyd-Steinberg (C 구현)


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
def normalize_image(img: Image.Image, max_width: int = PRINT_WIDTH) -> Image.Image:
    """1단계: 회전·투명 배경 처리 후 출력 폭에 맞춰 축소한 RGB/L 이미지.

    보정값과 무관한 단계라, /admin 미리보기는 이 결과를 캐시해 두고
    슬라이더를 움직일 때마다 2단계(adjust_image)만 다시 돌린다.
    """
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
    elif img.mode not in ("RGB", "L"):
        img = img.convert("RGB")

    # 가로폭에 맞춰 리사이즈
    ratio = max_width / img.width
    return img.resize((max_width, max(1, int(img.height * ratio))), Image.LANCZOS)


def adjust_image(
    img: Image.Image,
    brightness: float = 1.05,
    gamma: float = 1.8,
    dither: str = "fs",
    auto: bool = True,
    stretch: float = 2.0,
    target: float = 0.60,
    sharpen: float = 0.0,
):
    """2단계: 그레이스케일 → 샤프닝 → 보정 → 디더링.

    반환: (1비트 이미지, 실제 적용된 gamma, 실제 적용된 brightness)
    """
    # 그레이스케일
    img = img.convert("L")

    # 샤프닝: 576px로 줄이며 흐려진 윤곽을 살림 (디더링 후 형태가 또렷해짐)
    if sharpen > 0:
        img = img.filter(ImageFilter.UnsharpMask(radius=1.5, percent=int(sharpen), threshold=2))

    # ⓪ 퍼센타일 스트레칭: 하위/상위 stretch% 를 검정/흰색으로 매핑해 대비를 일정하게.
    #    히스토그램 기반이라 흰 배경 같은 아웃라이어에 강함. (auto일 때만)
    if auto and stretch > 0:
        img = ImageOps.autocontrast(img, cutoff=stretch)

    arr = np.array(img, dtype=np.float32)

    # auto면 스트레칭된 결과의 밝기에 맞춰 감마·밝기를 동적으로 산출, 아니면 고정값 사용
    if auto:
        gamma, brightness = auto_levels(arr, target=target)

    # ① 감마: 그림자/중간톤을 비선형으로 들어올림
    arr = 255.0 * (arr / 255.0) ** (1.0 / gamma)
    # ② 밝기: 전체를 선형으로 배수 (ImageEnhance.Brightness와 동일)
    arr = np.clip(arr * brightness, 0, 255)
    img = Image.fromarray(arr.astype(np.uint8))

    # 디더링 → 1비트
    return dither_image(img, dither), gamma, brightness


def prepare_image(
    img: Image.Image,
    max_width: int = PRINT_WIDTH,
    brightness: float = 1.05,
    gamma: float = 1.8,
    dither: str = "fs",
    auto: bool = True,
    stretch: float = 2.0,
    target: float = 0.60,
    sharpen: float = 0.0,
) -> Image.Image:
    """1단계 + 2단계. 원본 사진 → 영수증에 찍힐 1비트 이미지."""
    out, _, _ = adjust_image(
        normalize_image(img, max_width),
        brightness=brightness, gamma=gamma, dither=dither,
        auto=auto, stretch=stretch, target=target, sharpen=sharpen,
    )
    return out


def print_image(img: Image.Image):
    """현재 저장된 보정 설정으로 사진을 출력한다."""
    processed = prepare_image(img, **settings)

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


# ── Flask ─────────────────────────────────────────
app = Flask(__name__)
# 템플릿(html)을 고치면 서버 재시작 없이 바로 반영. (기본값은 첫 로딩 후 캐시라
# static/*.js 만 새 버전이 되고 html 은 옛 버전으로 남아 서로 어긋날 수 있음)
app.config["TEMPLATES_AUTO_RELOAD"] = True

# 출력 작업 큐: 워커 1개라 요청이 동시에 여러 개 와도 들어온 순서대로 하나씩 처리된다.
# (프린터를 두 번 동시에 열어 충돌하거나 이미지가 섞여 찍히는 것을 막음)
print_queue = ThreadPoolExecutor(max_workers=1)


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
        # 큐에 넣고 내 차례의 출력이 끝날 때까지 대기
        print_queue.submit(print_image, img).result()
        return jsonify({"status": "ok"})
    except Exception as e:
        app.logger.exception("출력 실패")
        return jsonify({"status": "error", "message": str(e)}), 500


# ── 관리 페이지: 보정값 조절 + 화면 미리보기 (출력하지 않음) ──
# 업로드한 사진은 1단계(normalize) 결과만 메모리에 잠깐 보관해 두고,
# 슬라이더를 움직일 때마다 2단계만 다시 계산한다. (최근 몇 장만 유지)
_admin_images = OrderedDict()
_admin_images_lock = threading.Lock()
ADMIN_CACHE_SIZE = 5


@app.route("/admin")
def admin():
    return render_template("admin.html")


@app.route("/admin/settings", methods=["GET"])
def admin_get_settings():
    return jsonify({
        "settings": settings,
        "defaults": DEFAULT_SETTINGS,
        "ranges": SETTING_RANGES,
    })


@app.route("/admin/settings", methods=["POST"])
def admin_save_settings():
    global settings
    new = clean_settings(request.get_json(silent=True))
    try:
        save_settings(new)
    except Exception as e:
        app.logger.exception("설정 저장 실패")
        return jsonify({"status": "error", "message": str(e)}), 500
    settings = new
    print(f"[admin] 설정 저장: {new}")
    return jsonify({"status": "ok", "settings": new})


@app.route("/admin/upload", methods=["POST"])
def admin_upload():
    if "image" not in request.files:
        return jsonify({"status": "error", "message": "이미지가 없습니다."}), 400
    try:
        img = Image.open(io.BytesIO(request.files["image"].read()))
        base = normalize_image(img)
    except Exception as e:
        app.logger.exception("미리보기 이미지 처리 실패")
        return jsonify({"status": "error", "message": str(e)}), 400

    image_id = uuid.uuid4().hex
    with _admin_images_lock:
        _admin_images[image_id] = base
        while len(_admin_images) > ADMIN_CACHE_SIZE:
            _admin_images.popitem(last=False)
    return jsonify({"status": "ok", "id": image_id})


@app.route("/admin/preview", methods=["POST"])
def admin_preview():
    data = request.get_json(silent=True) or {}
    with _admin_images_lock:
        base = _admin_images.get(data.get("id"))
    if base is None:
        return jsonify({"status": "error", "message": "사진을 다시 올려주세요."}), 404

    s = clean_settings(data.get("settings"))
    out, gamma, brightness = adjust_image(base, **s)
    buf = io.BytesIO()
    out.save(buf, format="PNG")
    return jsonify({
        "status": "ok",
        "image": "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode(),
        "width": out.width,
        "height": out.height,
        "gamma": gamma,
        "brightness": brightness,
    })


def main():
    # 블루 노이즈 마스크는 첫 계산에 수 초 걸리므로 서버 시작 시 미리 만들어 둠
    threading.Thread(target=blue_noise_mask, daemon=True).start()

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
    print(f"    보정값 조절: {scheme}://<라즈베리파이 IP>:{PORT}/admin")
    app.run(host=HOST, port=PORT, ssl_context=ssl_context)


if __name__ == "__main__":
    main()
