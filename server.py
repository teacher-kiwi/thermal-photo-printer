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
import random
import threading
from functools import lru_cache
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

from flask import Flask, request, jsonify, render_template
from PIL import Image, ImageOps, ImageFilter, ImageDraw, ImageFont
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
    "frame": True,       # 인스타그램 프레임(이름 머리글 + 좋아요/댓글/공유 아이콘) 넣기
    "title": "사진 출력",  # 메인 화면 맨 위 제목
    "camera": "environment",  # 메인 화면 카메라: "environment"(후면) / "user"(전면)
    "default_name": "",  # 이름을 비워두고 출력하면 대신 찍힐 이름
    # 맨 아래 "ㅇㅇ님 외 N명이 좋아합니다" — N은 출력할 때마다 like_min~like_max 에서 무작위
    "like_name": "",     # 비워두면 "N명이 좋아합니다"
    "like_min": 100,
    "like_max": 999,
}

# adjust_image 에 넘기는 이미지 보정 항목 (나머지는 프레임 관련)
IMAGE_KEYS = ("auto", "target", "stretch", "brightness", "gamma", "sharpen", "dither")

# 설정값 허용 범위 (최소, 최대) — 관리 페이지 슬라이더 범위와 동일
SETTING_RANGES = {
    "target": (0.30, 0.85),
    "stretch": (0.0, 10.0),
    "brightness": (0.5, 2.0),
    "gamma": (0.5, 4.0),
    "sharpen": (0.0, 300.0),
}
DITHER_MODES = ("fs", "atkinson", "jjn", "stucki", "sierra", "bluenoise")
CAMERA_MODES = ("environment", "user")
NAME_MAX_LEN = 20
TITLE_MAX_LEN = 30
LIKES_LIMIT = (0, 999999)


def clean_name(name, max_len: int = NAME_MAX_LEN) -> str:
    """이름·제목 글자: 제어문자 제거, 앞뒤 공백 제거, 길이 제한."""
    name = "".join(ch for ch in str(name or "") if ch.isprintable())
    return name.strip()[:max_len]


def clean_settings(data) -> dict:
    """입력값을 검증해 허용 범위 안의 완전한 설정 dict로 만든다. 빠진 값은 기본값."""
    s = dict(DEFAULT_SETTINGS)
    if not isinstance(data, dict):
        return s
    for key in ("auto", "frame"):
        if key in data:
            s[key] = bool(data[key])
    for key, (lo, hi) in SETTING_RANGES.items():
        if key in data:
            try:
                s[key] = min(max(float(data[key]), lo), hi)
            except (TypeError, ValueError):
                pass
    if data.get("dither") in DITHER_MODES:
        s["dither"] = data["dither"]
    if data.get("camera") in CAMERA_MODES:
        s["camera"] = data["camera"]
    for key in ("default_name", "like_name"):
        if key in data:
            s[key] = clean_name(data[key])
    if "title" in data:  # 비우면 기본 제목
        s["title"] = clean_name(data["title"], TITLE_MAX_LEN) or DEFAULT_SETTINGS["title"]
    for key in ("like_min", "like_max"):
        if key in data:
            try:
                s[key] = min(max(int(float(data[key])), LIKES_LIMIT[0]), LIKES_LIMIT[1])
            except (TypeError, ValueError):
                pass
    if s["like_min"] > s["like_max"]:
        s["like_min"], s["like_max"] = s["like_max"], s["like_min"]
    return s


def pick_likes(s: dict, value=None) -> int:
    """좋아요 수: 화면에 보여준 값(value)이 범위 안이면 그대로, 아니면 범위에서 무작위."""
    lo, hi = s["like_min"], s["like_max"]
    try:
        n = int(value)
        if lo <= n <= hi:
            return n
    except (TypeError, ValueError):
        pass
    return random.randint(lo, hi)


def likes_runs(like_name: str, n: int):
    """좋아요 문구를 [(글자, 굵게?)] 조각으로. 이름과 숫자만 굵게 (camera.js 와 같은 형식)."""
    if like_name:
        return [(like_name, True), ("님 외 ", False), (f"{n:,}", True), ("명이 좋아합니다", False)]
    return [(f"{n:,}", True), ("명이 좋아합니다", False)]


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


# ── 인스타그램 프레임 ──────────────────────────────
# 위: 프로필 원 + 작성자 이름 + ⋯ / 가운데: 사진 / 아래: ♡ 💬 ✈
# 글자·아이콘은 디더링하지 않고 선명한 흑백으로 그린다.
POST_HEADER_H = 84
POST_FOOTER_H = 72
POST_LIKES_H = 48
# 한글 글꼴 후보 (앞에서부터 있는 것 사용). FONT_PATH / FONT_BOLD_PATH 환경변수로 직접 지정 가능.
#   라즈베리파이:  sudo apt install fonts-nanum
FONT_CANDIDATES = {
    "regular": [
        os.environ.get("FONT_PATH", ""),
        "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/System/Library/Fonts/AppleSDGothicNeo.ttc",               # macOS
        "/System/Library/Fonts/Supplemental/AppleGothic.ttf",       # macOS
    ],
    "bold": [
        os.environ.get("FONT_BOLD_PATH", ""),
        "/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/System/Library/Fonts/AppleSDGothicNeo.ttc",               # macOS (안에서 Bold 찾음)
    ],
}


def _ttc_index(path: str, style: str):
    """.ttc(글꼴 묶음) 안에서 style("Regular"/"Bold") 인 글꼴 번호. 없으면 None."""
    for i in range(32):
        try:
            if ImageFont.truetype(path, 12, index=i).getname()[1] == style:
                return i
        except OSError:
            break
    return None


@lru_cache(maxsize=2)
def _font_file(weight: str):
    """weight("regular"/"bold")에 맞는 (경로, 번호). 없으면 None."""
    style = "Bold" if weight == "bold" else "Regular"
    for path in FONT_CANDIDATES[weight]:
        if not (path and os.path.exists(path)):
            continue
        if path.endswith(".ttc"):
            idx = _ttc_index(path, style)
            if idx is None:
                if weight == "bold":
                    continue  # 묶음 안에 굵은 글꼴이 없으면 다음 후보
                idx = 0
            return path, idx
        return path, 0
    return None


def _font(size: int, bold: bool = False):
    """반환: (글꼴, 테두리 두께). 굵은 글꼴이 없으면 보통 글꼴 + 테두리로 굵게 흉내."""
    found = _font_file("bold" if bold else "regular")
    if found:
        return ImageFont.truetype(found[0], size, index=found[1]), 0
    other = _font_file("regular" if bold else "bold")
    if other:
        return ImageFont.truetype(other[0], size, index=other[1]), (1 if bold else 0)
    if not getattr(_font, "_warned", False):
        _font._warned = True
        print("⚠ 한글 글꼴을 찾지 못해 이름이 깨질 수 있습니다. "
              "라즈베리파이라면 'sudo apt install fonts-nanum' 후 서버를 재시작하세요.")
    return ImageFont.load_default(size), (1 if bold else 0)


def _draw_runs(draw, xy, runs, size: int, max_w: int):
    """[(글자, 굵게?), ...] 를 이어서 한 줄로 그린다. 세로 기준은 가운데(anchor 'lm').

    폭을 넘으면 첫 번째 굵은 조각(이름)을 줄여 '…' 를 붙인다.
    """
    fonts = {b: _font(size, b) for b in (False, True)}

    def width(rs):
        return sum(draw.textlength(t, font=fonts[b][0]) + fonts[b][1] * 2 for t, b in rs)

    runs = list(runs)
    if width(runs) > max_w:
        i = next((k for k, (_, b) in enumerate(runs) if b), 0)
        text = runs[i][0]
        while text and width(runs[:i] + [(text + "…", runs[i][1])] + runs[i + 1:]) > max_w:
            text = text[:-1]
        runs[i] = (text + "…", runs[i][1])

    x, y = xy
    for text, bold in runs:
        font, stroke = fonts[bold]
        draw.text((x, y), text, font=font, fill=0, anchor="lm", stroke_width=stroke, stroke_fill=0)
        x += draw.textlength(text, font=font) + stroke * 2


# 아이콘 모양: SVG path. 화면(_post.html)과 출력물이 같은 모양(icon_geometry)을 쓴다.
#   viewbox: 원본 SVG 의 좌표 크기 (viewBox="0 0 N N" 의 N)
#   stroke:  선 두께. 24 기준으로 환산했을 때 모든 아이콘이 같게 (24 기준 2 = 512 기준 42.67)
#   rotate:  (선택) 가운데를 중심으로 시계방향 회전 각도(도)
#   scale:   (선택) 가운데를 중심으로 크기 배율. 선 두께는 그대로
#   stretch_x / stretch_y: (선택) 회전한 "뒤에" 화면 기준 좌우/위아래로만 늘리는 배율
#   적용 순서: scale → rotate → stretch → 칸 가운데로 자동 맞춤
#   <line x1 y1 x2 y2> 는 "M x1 y1 x2 y2" path 로 바꿔 넣으면 된다.
ICONS = {
    # 하트 (SVG Repo)
    "heart": {"viewbox": 24, "stroke": 2, "scale": 1.1, "paths": [
        "M12 6.00019C10.2006 3.90317 7.19377 3.2551 4.93923 5.17534C2.68468 7.09558 2.36727 10.3061 "
        "4.13778 12.5772C5.60984 14.4654 10.0648 18.4479 11.5249 19.7369C11.6882 19.8811 11.7699 19.9532 "
        "11.8652 19.9815C11.9483 20.0062 12.0393 20.0062 12.1225 19.9815C12.2178 19.9532 12.2994 19.8811 "
        "12.4628 19.7369C13.9229 18.4479 18.3778 14.4654 19.8499 12.5772C21.6204 10.3061 21.3417 7.07538 "
        "19.0484 5.17534C16.7551 3.2753 13.7994 3.90317 12 6.00019Z",
    ]},
    # 말풍선 (SVG Repo "bubble-circle"). 원본에 transform="matrix(-1,0,0,1,0,0)"(좌우 반전)이 있어
    # 좌표를 미리 뒤집어 넣음 (x → 24-x, 상대 x 부호 반대, 호의 sweep 0↔1) → 꼬리가 오른쪽 아래
    #   원본: M7.6,20.4a9.5,9.5,0,1,0-4-4L2.5,21.5Z
    "comment": {"viewbox": 24, "stroke": 2, "scale": 0.95, "paths": [
        "M16.4,20.4a9.5,9.5,0,1,1,4-4L21.5,21.5Z",
    ]},
    # Ionicons v5 paper-plane-outline (MIT, https://ionic.io/ionicons)
    "send": {"viewbox": 512, "stroke": 2 * 512 / 24, "rotate": 20, "stretch_y": 1, "paths": [
        "M53.12,199.94l400-151.39a8,8,0,0,1,10.33,10.33l-151.39,400a8,8,0,0,1-15-.34"
        "L229.66,292.45a16,16,0,0,0-10.11-10.11L53.46,215A8,8,0,0,1,53.12,199.94Z",
        "M460 52 227 285",
    ]},
    # 북마크 (SVG Repo). 원본은 선 두께 1(기본값) → 다른 아이콘과 같게 2. 흰 배경 rect 는 뺌
    "bookmark": {"viewbox": 24, "stroke": 2, "paths": [
        "M5 19.6693V4C5 3.44772 5.44772 3 6 3H18C18.5523 3 19 3.44772 19 4V19.6693"
        "C19 20.131 18.4277 20.346 18.1237 19.9985L12 13L5.87629 19.9985C5.57227 20.346 5 20.131 5 19.6693Z",
    ]},
}


def _arc_points(x1, y1, rx, ry, phi, large, sweep, x2, y2, n=24):
    """SVG 호(A) → 점 목록 (SVG 명세의 끝점→중심 변환)."""
    import math
    if rx == 0 or ry == 0:
        return [(x2, y2)]
    rx, ry = abs(rx), abs(ry)
    c, s_ = math.cos(math.radians(phi)), math.sin(math.radians(phi))
    dx, dy = (x1 - x2) / 2, (y1 - y2) / 2
    x1p, y1p = c * dx + s_ * dy, -s_ * dx + c * dy
    lam = x1p ** 2 / rx ** 2 + y1p ** 2 / ry ** 2
    if lam > 1:
        rx, ry = rx * math.sqrt(lam), ry * math.sqrt(lam)
    num = rx ** 2 * ry ** 2 - rx ** 2 * y1p ** 2 - ry ** 2 * x1p ** 2
    den = rx ** 2 * y1p ** 2 + ry ** 2 * x1p ** 2
    coef = math.sqrt(max(0.0, num / den)) if den else 0.0
    if large == sweep:
        coef = -coef
    cxp, cyp = coef * rx * y1p / ry, -coef * ry * x1p / rx
    cx = c * cxp - s_ * cyp + (x1 + x2) / 2
    cy = s_ * cxp + c * cyp + (y1 + y2) / 2

    def ang(ux, uy, vx, vy):
        return math.atan2(ux * vy - uy * vx, ux * vx + uy * vy)

    ux, uy = (x1p - cxp) / rx, (y1p - cyp) / ry
    vx, vy = (-x1p - cxp) / rx, (-y1p - cyp) / ry
    th1 = ang(1, 0, ux, uy)
    dth = ang(ux, uy, vx, vy)
    if not sweep and dth > 0:
        dth -= 2 * math.pi
    elif sweep and dth < 0:
        dth += 2 * math.pi
    pts = []
    for i in range(1, n + 1):
        th = th1 + dth * i / n
        pts.append((c * rx * math.cos(th) - s_ * ry * math.sin(th) + cx,
                    s_ * rx * math.cos(th) + c * ry * math.sin(th) + cy))
    return pts


def svg_path_points(d: str):
    """SVG path 문자열 → [(점 목록, 닫힘 여부), ...]. M L H V C A Z (대/소문자)만 지원."""
    import re
    tokens = re.findall(r"[MLHVCAZmlhvcaz]|[-+]?(?:\d*\.\d+|\d+\.?)(?:[eE][-+]?\d+)?", d)
    subpaths, pts = [], []
    x = y = sx = sy = 0.0
    cmd, i = None, 0

    def num():
        nonlocal i
        v = float(tokens[i])
        i += 1
        return v

    while i < len(tokens):
        if tokens[i].isalpha():
            cmd = tokens[i]
            i += 1
        rel = cmd.islower()
        C = cmd.upper()
        if C == "Z":
            if pts:
                subpaths.append((pts, True))
            pts, x, y = [], sx, sy
            continue
        if C == "M":
            if pts:
                subpaths.append((pts, False))
            nx, ny = num(), num()
            x, y = (x + nx, y + ny) if rel else (nx, ny)
            sx, sy, pts = x, y, [(x, y)]
            cmd = "l" if rel else "L"  # M 뒤에 이어지는 좌표는 선
        elif C == "L":
            nx, ny = num(), num()
            x, y = (x + nx, y + ny) if rel else (nx, ny)
            pts.append((x, y))
        elif C == "H":
            nx = num()
            x = x + nx if rel else nx
            pts.append((x, y))
        elif C == "V":
            ny = num()
            y = y + ny if rel else ny
            pts.append((x, y))
        elif C == "C":
            c = [num() for _ in range(6)]
            if rel:
                c = [c[k] + (x if k % 2 == 0 else y) for k in range(6)]
            x0, y0 = x, y
            for k in range(1, 17):
                t = k / 16
                a, b, cc, dd = (1 - t) ** 3, 3 * (1 - t) ** 2 * t, 3 * (1 - t) * t ** 2, t ** 3
                pts.append((a * x0 + b * c[0] + cc * c[2] + dd * c[4],
                            a * y0 + b * c[1] + cc * c[3] + dd * c[5]))
            x, y = c[4], c[5]
        elif C == "A":
            rx, ry, phi, large, sweep, nx, ny = (num() for _ in range(7))
            if rel:
                nx, ny = x + nx, y + ny
            pts += _arc_points(x, y, rx, ry, phi, int(large), int(sweep), nx, ny)
            x, y = nx, ny
        else:
            raise ValueError(f"지원하지 않는 SVG 명령: {cmd}")
    if pts:
        subpaths.append((pts, False))
    return subpaths


@lru_cache(maxsize=None)
def icon_geometry(name: str):
    """ICONS[name] → 24×24 기준으로 맞추고 배율·회전·늘리기를 적용한 모양.

    반환: (선 두께, [(점 목록, 닫힘 여부), ...])  — 모두 24 기준 좌표
    """
    import math
    icon = ICONS[name]
    k = 24.0 / icon["viewbox"]
    scale = icon.get("scale", 1.0)
    a = math.radians(icon.get("rotate", 0))  # 화면 좌표(y 아래)라 +각도 = 시계방향
    ca, sa = math.cos(a), math.sin(a)
    sx, sy = icon.get("stretch_x", 1.0), icon.get("stretch_y", 1.0)

    def tf(px, py):
        x, y = (px * k - 12) * scale, (py * k - 12) * scale
        x, y = x * ca - y * sa, x * sa + y * ca  # 회전
        return (12 + x * sx, 12 + y * sy)         # 회전 후 좌우/위아래 늘리기

    shapes = []
    for path in icon["paths"]:
        for pts, closed in svg_path_points(path):
            shapes.append(([tf(px, py) for px, py in pts], closed))

    # 회전하면 모양이 한쪽으로 쏠리므로, 전체 테두리 상자의 가운데를 (12, 12)에 맞춤
    xs = [px for pts, _ in shapes for px, _ in pts]
    ys = [py for pts, _ in shapes for _, py in pts]
    dx, dy = 12 - (min(xs) + max(xs)) / 2, 12 - (min(ys) + max(ys)) / 2
    shapes = [([(px + dx, py + dy) for px, py in pts], closed) for pts, closed in shapes]
    return icon["stroke"] * k, shapes


def icon_svg(name: str) -> dict:
    """화면용: 출력물과 같은 모양을 <svg viewBox="0 0 24 24"> 안의 polygon/polyline 으로."""
    stroke, shapes = icon_geometry(name)
    return {
        "stroke": round(stroke, 3),
        "shapes": [(" ".join(f"{x:.2f},{y:.2f}" for x, y in pts), closed) for pts, closed in shapes],
    }


def _draw_icon(d, name: str, x: float, y: float, size: int):
    """ICONS[name] 을 (x, y) 에 size 크기로 그린다. 선 끝·꺾임은 둥글게."""
    stroke, shapes = icon_geometry(name)
    k = size / 24.0
    w = max(2, round(stroke * k))
    for pts, closed in shapes:
        pts = [(x + px * k, y + py * k) for px, py in pts]
        if closed:
            pts = pts + pts[:1]
        d.line(pts, fill=0, width=w, joint="curve")
        if not closed:  # 둥근 선 끝
            for px, py in (pts[0], pts[-1]):
                d.ellipse((px - w / 2, py - w / 2, px + w / 2, py + w / 2), fill=0)


POST_ICONS = ("heart", "comment", "send")  # 왼쪽부터
POST_ICONS_RIGHT = ("bookmark",)            # 오른쪽 끝에 붙임
POST_ICONS_SVG = (                          # 화면(_post.html)용
    [dict(icon_svg(n), right=False) for n in POST_ICONS]
    + [dict(icon_svg(n), right=True) for n in POST_ICONS_RIGHT]
)


def _draw_header(width: int, name: str) -> Image.Image:
    img = Image.new("L", (width, POST_HEADER_H), 255)
    d = ImageDraw.Draw(img)
    cy = POST_HEADER_H // 2

    # 프로필 원 (스토리 테두리처럼 이중 원) + 이름 첫 글자
    cx, r = 44, 28
    d.ellipse((cx - r, cy - r, cx + r, cy + r), outline=0, width=3)
    r2 = r - 7
    d.ellipse((cx - r2, cy - r2, cx + r2, cy + r2), outline=0, width=2)
    if name:
        font, stroke = _font(22, bold=True)
        d.text((cx, cy), name[0], font=font, fill=0, anchor="mm", stroke_width=stroke, stroke_fill=0)

    # 더보기 점 3개
    for i in range(3):
        x = width - 52 + i * 14
        d.ellipse((x - 3, cy - 3, x + 3, cy + 3), fill=0)

    # 이름 (굵게)
    if name:
        x0 = cx + r + 18
        _draw_runs(d, (x0, cy), [(name, True)], 28, width - x0 - 72)
    return img


def _draw_footer(width: int, likes: list) -> Image.Image:
    img = Image.new("L", (width, POST_FOOTER_H + POST_LIKES_H), 255)
    d = ImageDraw.Draw(img)
    size = 44
    gap = 26
    y = (POST_FOOTER_H - size) // 2

    # ♡ 좋아요  💬 댓글  ✈ 공유
    for i, name in enumerate(POST_ICONS):
        _draw_icon(d, name, 16 + i * (size + gap), y, size)
    # 🔖 북마크: 오른쪽 끝 (왼쪽 여백과 같은 16px)
    for i, name in enumerate(reversed(POST_ICONS_RIGHT)):
        _draw_icon(d, name, width - 16 - size - i * (size + gap), y, size)

    # 좋아요 문구 (아이콘 줄 아래)
    _draw_runs(d, (20, POST_FOOTER_H + POST_LIKES_H // 2 - 6), likes, 24, width - 40)
    return img


def _to_1bit(img_l: Image.Image) -> Image.Image:
    return img_l.point(lambda v: 255 if v >= 128 else 0).convert("1", dither=Image.Dither.NONE)


def compose_post(photo: Image.Image, name: str, likes: list) -> Image.Image:
    """디더링된 1비트 사진에 인스타그램 프레임(머리글·아이콘줄·좋아요 문구)을 붙인다."""
    w = photo.width
    header = _to_1bit(_draw_header(w, clean_name(name)))
    footer = _to_1bit(_draw_footer(w, likes))
    post = Image.new("1", (w, header.height + photo.height + footer.height), 1)
    post.paste(header, (0, 0))
    post.paste(photo.convert("1"), (0, header.height))
    post.paste(footer, (0, header.height + photo.height))
    return post


def make_receipt(base: Image.Image, s: dict, name: str = "", likes=None):
    """normalize_image 결과 + 설정 → 최종 출력 이미지. 반환: (이미지, gamma, brightness)

    likes: 화면에 보여준 좋아요 수. 없거나 범위 밖이면 무작위로 뽑는다.
    """
    out, gamma, brightness = adjust_image(base, **{k: s[k] for k in IMAGE_KEYS})
    if s.get("frame"):
        # 이름을 비워두면 관리 페이지에서 저장한 기본 이름 사용
        author = clean_name(name) or s["default_name"]
        out = compose_post(out, author, likes_runs(s["like_name"], pick_likes(s, likes)))
    return out, gamma, brightness


def print_image(img: Image.Image, name: str = "", likes=None):
    """현재 저장된 보정 설정으로 사진을 출력한다."""
    processed, _, _ = make_receipt(normalize_image(img), settings, name, likes)
    send_to_printer(processed)


def send_to_printer(processed: Image.Image):
    """완성된 1비트 이미지를 프린터로 보내고 자른다."""
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
    return render_template("index.html", settings=settings, icons=POST_ICONS_SVG)


@app.route("/print", methods=["POST"])
def handle_print():
    if "image" not in request.files:
        return jsonify({"status": "error", "message": "이미지가 없습니다."}), 400
    try:
        file = request.files["image"]
        img = Image.open(io.BytesIO(file.read()))
        name = clean_name(request.form.get("name", ""))
        likes = request.form.get("likes")  # 화면에 보여준 좋아요 수
        # 큐에 넣고 내 차례의 출력이 끝날 때까지 대기
        print_queue.submit(print_image, img, name, likes).result()
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
    return render_template("admin.html", settings=settings, icons=POST_ICONS_SVG)


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
    out, gamma, brightness = make_receipt(base, s, data.get("name", ""), data.get("likes"))
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


@app.route("/admin/print", methods=["POST"])
def admin_print():
    """출력 테스트: 관리 페이지의 현재 값(저장 전이어도)으로 미리보기와 같은 이미지를 실제 출력."""
    data = request.get_json(silent=True) or {}
    with _admin_images_lock:
        base = _admin_images.get(data.get("id"))
    if base is None:
        return jsonify({"status": "error", "message": "사진을 다시 올려주세요."}), 404

    s = clean_settings(data.get("settings"))
    name, likes = data.get("name", ""), data.get("likes")

    def job():
        out, _, _ = make_receipt(base, s, name, likes)
        send_to_printer(out)

    try:
        # 메인 화면 출력과 같은 큐 → 동시에 와도 순서대로
        print_queue.submit(job).result()
        return jsonify({"status": "ok"})
    except Exception as e:
        app.logger.exception("출력 테스트 실패")
        return jsonify({"status": "error", "message": str(e)}), 500


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
