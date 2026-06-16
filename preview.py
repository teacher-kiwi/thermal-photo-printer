"""
출력 결과 미리보기 (프린터 불필요)

사진을 영수증 프린터가 실제로 찍을 1비트 이미지로 변환해 PNG로 저장합니다.
밝기/감마/디더링 같은 이미지 처리를 프린터 없이 빠르게 튜닝할 때 사용하세요.

사용법:
    ./venv/bin/python preview.py 사진.jpg              # 사진_preview.png 생성
    ./venv/bin/python preview.py 사진.jpg out.png      # 파일명 지정
    ./venv/bin/python preview.py 사진.jpg --brightness 1.2 --gamma 2.2

server.py 의 prepare_image() 를 그대로 쓰므로, server.py 를 고치면 결과도 같이 바뀝니다.
"""

import sys
import argparse

from PIL import Image
from server import prepare_image, PRINT_WIDTH


def main():
    ap = argparse.ArgumentParser(description="영수증 출력 미리보기 생성")
    ap.add_argument("image", help="입력 사진 경로")
    ap.add_argument("output", nargs="?", help="출력 PNG 경로 (기본: <입력>_preview.png)")
    ap.add_argument("--brightness", type=float, default=1.05)
    ap.add_argument("--gamma", type=float, default=1.8)
    args = ap.parse_args()

    out = args.output or (args.image.rsplit(".", 1)[0] + "_preview.png")

    img = Image.open(args.image)
    processed = prepare_image(
        img, max_width=PRINT_WIDTH, brightness=args.brightness, gamma=args.gamma
    )
    processed.save(out)
    print(f"미리보기 저장: {out}  ({processed.width}x{processed.height}, 1비트)")
    print("이 이미지가 영수증에 찍히는 실제 픽셀입니다.")


if __name__ == "__main__":
    main()
