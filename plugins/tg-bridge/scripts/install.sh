#!/usr/bin/env bash
# Ставит tg-bridge: ~/.local/bin/tg и systemd --user сервис демона.
#   ./install.sh            # токен уже в ~/.config/tg-bridge/env (TG_TOKEN=..., опционально TG_PROXY=...)
#   ./install.sh uninstall
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
CONF=${TG_BRIDGE_CONFIG:-$HOME/.config/tg-bridge}
UNIT_DIR=$HOME/.config/systemd/user
BIN=$HOME/.local/bin/tg

if [ "${1:-}" = uninstall ]; then
  systemctl --user disable --now tg-bridge.service 2>/dev/null || true
  rm -f "$UNIT_DIR/tg-bridge.service" "$BIN"
  systemctl --user daemon-reload
  echo "OK: удалено (конфиг $CONF и ~/.local/share/tg-bridge оставлены)"
  exit 0
fi

mkdir -p "$CONF" "$(dirname "$BIN")" "$UNIT_DIR"
[ -e "$CONF/env" ] || { printf 'TG_TOKEN=\n' > "$CONF/env"; chmod 600 "$CONF/env"; }
grep -q '^TG_TOKEN=.' "$CONF/env" || { echo "ERR: впиши токен от @BotFather в $CONF/env"; exit 1; }

# обёртка, а не симлинк: python3 -I и путь к скрипту не зависят от PATH/cwd вызывающего
cat > "$BIN" <<EOF
#!/bin/sh
exec /usr/bin/env python3 -I "$HERE/tg.py" "\$@"
EOF
chmod +x "$BIN"

cat > "$UNIT_DIR/tg-bridge.service" <<EOF
[Unit]
Description=tg-bridge: Telegram bot long-poll for agent asks, inbox and buttons
Wants=network-online.target
After=network-online.target

[Service]
ExecStart=$BIN daemon
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload
systemctl --user enable --now tg-bridge.service >/dev/null
systemctl --user restart tg-bridge.service
echo "OK: $BIN, сервис tg-bridge запущен"
"$BIN" whoami
[ -s "$CONF/chat" ] || echo "Теперь напиши боту /start — демон привяжет этот чат."
