#!/usr/bin/env bash
# 라즈베리파이 내장 와이파이를 핫스팟(AP)으로 켭니다. (Debian Trixie / NetworkManager)
# 사용법:  sudo ./setup/hotspot.sh [SSID] [PASSWORD]
set -e

SSID="${1:-ReceiptPi}"
PASS="${2:-print1234}"   # 최소 8자 이상

if [ ${#PASS} -lt 8 ]; then
  echo "비밀번호는 8자 이상이어야 합니다." >&2
  exit 1
fi

# 기존 동일 이름 핫스팟이 있으면 제거 후 재생성 (설정 갱신용)
nmcli connection delete Hotspot 2>/dev/null || true

# wlan0 을 AP 모드로 생성. 게이트웨이/스마트폰 IP는 10.42.0.x 대역이 됩니다.
nmcli device wifi hotspot ifname wlan0 con-name Hotspot ssid "$SSID" password "$PASS"

# 부팅 시 자동 시작
nmcli connection modify Hotspot connection.autoconnect yes

echo "핫스팟이 켜졌습니다."
echo "  SSID    : $SSID"
echo "  PASSWORD: $PASS"
echo "  접속 후 주소: https://10.42.0.1:3001"
nmcli -g IP4.ADDRESS device show wlan0 2>/dev/null || true
