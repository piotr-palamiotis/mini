#!/bin/sh
# Local demo services. Mailpit: SMTP mailpit:1025 inside mini-net, web inbox at http://127.0.0.1:8025
# usage: demo/services.sh up|down|status
set -e
case "$1" in
  up)
    podman network exists mini-net || podman network create mini-net >/dev/null
    podman container exists mailpit || podman run -d --name mailpit --network mini-net \
        -p 127.0.0.1:8025:8025 -p 127.0.0.1:1025:1025 --memory 128m docker.io/axllent/mailpit:latest >/dev/null
    podman start mailpit >/dev/null
    echo "Mailpit inbox: http://127.0.0.1:8025" ;;
  down) podman rm -f mailpit >/dev/null 2>&1 || true ;;
  stop) podman stop mailpit >/dev/null ;;
  status) podman ps --filter name=mailpit --format '{{.Names}} {{.Status}}' ;;
  *) echo "usage: $0 up|down|stop|status"; exit 2 ;;
esac
