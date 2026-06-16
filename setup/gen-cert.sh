#!/usr/bin/env bash
# 실시간 카메라(getUserMedia)는 보안 컨텍스트(HTTPS)에서만 동작합니다.
# 자체서명 인증서를 만들어 /app/cert.pem, /app/key.pem 에 저장합니다.
set -e

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
IP="${1:-10.42.0.1}"   # 핫스팟 게이트웨이 IP (nmcli 기본값)

openssl req -x509 -newkey rsa:2048 -nodes \
  -keyout "$APP_DIR/key.pem" \
  -out "$APP_DIR/cert.pem" \
  -days 3650 \
  -subj "/CN=$IP" \
  -addext "subjectAltName=IP:$IP"

echo "인증서 생성 완료:"
echo "  $APP_DIR/cert.pem"
echo "  $APP_DIR/key.pem"
echo "스마트폰에서 https://$IP:3001 접속 시 '안전하지 않음' 경고를 한 번 허용하면 됩니다."
