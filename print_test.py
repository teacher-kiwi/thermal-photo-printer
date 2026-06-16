"""
영수증 프린터 연결 테스트 (라즈베리파이)

웹서버/핫스팟 없이 프린터 연결만 확인합니다.

사용법:
    ./venv/bin/python print_test.py            # 텍스트 + QR 테스트 출력
    ./venv/bin/python print_test.py 사진.jpg   # 사진까지 출력

프린터를 못 찾으면:
    lsusb 로 ID 확인 후
    PRINTER_VID=0x0416 PRINTER_PID=0x5011 ./venv/bin/python print_test.py
"""

import sys
import datetime

# server.py 의 연결/이미지 로직을 그대로 재사용
from server import open_printer, prepare_image, PRINT_WIDTH


def main():
    print("프린터 연결 중...")
    try:
        p = open_printer()
    except Exception as e:
        print(f"❌ 연결 실패: {e}")
        print("   - USB 케이블/전원 확인")
        print("   - lsusb 로 ID 확인 후 PRINTER_VID/PRINTER_PID 지정")
        print("   - 권한 오류면 sudo 로 실행하거나 udev 규칙 적용")
        sys.exit(1)

    print("✅ 연결 성공! 테스트 출력 중...")
    try:
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        p.set(align="center", bold=True, double_height=True, double_width=True)
        p.text("TEST OK\n")
        p.set(align="center", bold=False, double_height=False, double_width=False)
        p.text("영수증 프린터 연결 테스트\n")
        p.text(now + "\n")
        p.text("-" * 32 + "\n")

        # 정렬/크기 확인용
        p.set(align="left")
        p.text("왼쪽 정렬 abcdefg 1234567890\n")
        p.set(align="center")
        p.text("가운데 정렬\n")
        p.set(align="right")
        p.text("오른쪽 정렬\n")
        p.set(align="center")

        # QR 코드 (그래픽 출력 경로 확인)
        try:
            p.qr("https://www.raspberrypi.com", size=6)
        except Exception as e:
            p.text(f"(QR 생략: {e})\n")

        # 인자로 이미지를 주면 사진 출력까지 테스트
        if len(sys.argv) > 1:
            from PIL import Image

            path = sys.argv[1]
            print(f"이미지 출력: {path}")
            img = prepare_image(Image.open(path), max_width=PRINT_WIDTH)
            p.image(img)

        p.set(align="left")
        p.ln(2)
        p.cut()
        print("✅ 출력 완료! 영수증을 확인하세요.")
    finally:
        try:
            p.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
