#!/bin/sh
# Install one of the systemd units from ./deploy onto this Linux host.
# Usage: sudo scripts/install_service.sh wake|console [INSTALL_DIR] [USER]
set -eu

WHAT="${1:-wake}"
INSTALL_DIR="${2:-$(cd "$(dirname "$0")/.." && pwd)}"
SERVICE_USER="${3:-${SUDO_USER:-$(id -un)}}"

case "$WHAT" in
    wake)   NAME=ova-wake ;;
    console) NAME=ova-console ;;
    *) echo "usage: $0 wake|console [INSTALL_DIR] [USER]"; exit 2 ;;
esac
UNIT="/etc/systemd/system/$NAME.service"
TEMPLATE="$(dirname "$0")/../deploy/$NAME.service"

sed "s|__INSTALL_DIR__|$INSTALL_DIR|g; s|__SERVICE_USER__|$SERVICE_USER|g" "$TEMPLATE" > "$UNIT"
systemctl daemon-reload
systemctl enable --now "$NAME"
echo "installed $UNIT"
systemctl status "$NAME" --no-pager | head -8
