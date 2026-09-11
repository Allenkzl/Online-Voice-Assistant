#!/bin/sh
# Deploy this checkout to the Reachy Mini and switch the dialogue engine.
#
#   ZHIPUAI_API_KEY=xxx ./scripts/deploy_to_robot.sh            # 切到端到端
#   ENGINE=pipeline ./scripts/deploy_to_robot.sh                # 回退半在线
#   DEPLOY_CODE=0 ./scripts/deploy_to_robot.sh                  # 只改配置
#
# Facts about the robot (2026-09-10):
#   * /home/pollen/ova is NOT a git repo  -> code is rsynced, not pulled
#   * there is no config.json; config lives in /etc/ova.env (600)
#   * ova-wake runs as User=pollen, which is what makes the reachymini_* ALSA
#     aliases (defined in pollen's ~/.asoundrc) visible to the service
#   * pollen has passwordless sudo
set -eu

ROBOT="${ROBOT:-pollen@reachy-mini.local}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_ed25519_rebot_codex}"
REMOTE="${REMOTE:-/home/pollen/ova}"
ENGINE="${ENGINE:-e2e}"
DEPLOY_CODE="${DEPLOY_CODE:-1}"
WAKE_THRESHOLD="${WAKE_THRESHOLD:-0.28}"
WAKE_HITS="${WAKE_HITS:-4}"
WAKE_ACK_BEFORE_REPLY="${WAKE_ACK_BEFORE_REPLY:-0}"
LOCAL="$(cd "$(dirname "$0")/.." && pwd)"

SSH="ssh -i $SSH_KEY -o ConnectTimeout=10 -o BatchMode=yes $ROBOT"
RSH="ssh -i $SSH_KEY -o ConnectTimeout=10 -o BatchMode=yes"

echo "=== 0) 体检 ==="
$SSH 'sudo grep -E "^WAKE_(ENGINE|DIALOGUE|ASR_MODEL_DIR|THRESHOLD|HITS|ACK_BEFORE_REPLY)=" /etc/ova.env 2>/dev/null || true
  sudo grep -q "^ZHIPUAI_API_KEY=." /etc/ova.env && echo "ZHIPUAI_API_KEY: 已存在" || echo "ZHIPUAI_API_KEY: 缺失"
  echo "ova-wake: $(systemctl is-active ova-wake) | ova-console: $(systemctl is-active ova-console)"'

if [ "$DEPLOY_CODE" = "1" ]; then
  echo
  echo "=== 1) 备份 ==="
  $SSH "mkdir -p $REMOTE/backups && cd $REMOTE && \
    tar czf backups/pre-deploy-\$(date +%Y%m%d-%H%M%S).tar.gz src scripts config docs deploy tests 2>/dev/null; \
    sudo cp /etc/ova.env /etc/ova.env.bak-\$(date +%Y%m%d-%H%M%S); echo 备份完成"

  echo
  echo "=== 2) 同步代码（不碰 models/assets/config.json）==="
  for d in src scripts config docs deploy tests; do
    [ -d "$LOCAL/$d" ] || continue
    rsync -az --delete --exclude '__pycache__' --exclude '*.pyc' -e "$RSH" \
      "$LOCAL/$d/" "$ROBOT:$REMOTE/$d/"
  done
  for f in README.md MODEL_NOTICE.md requirements.txt AGENTS.md pyproject.toml; do
    [ -f "$LOCAL/$f" ] && rsync -az -e "$RSH" "$LOCAL/$f" "$ROBOT:$REMOTE/"
  done
  # 清理可能由 sudo 运行留下的 root 属主缓存（否则后续 rsync 报权限错）
  $SSH "sudo find $REMOTE/src -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true"
fi

echo
echo "=== 3) 写入配置（/etc/ova.env，幂等）==="
KEY_LINE=""
if [ -n "${ZHIPUAI_API_KEY:-}" ]; then
  KEY_LINE="ZHIPUAI_API_KEY=$ZHIPUAI_API_KEY"
fi
$SSH "sudo K='$ENGINE' TH='$WAKE_THRESHOLD' HT='$WAKE_HITS' ACK='$WAKE_ACK_BEFORE_REPLY' KL='$KEY_LINE' sh -c 'set -eu
  touch /etc/ova.env
  touch /tmp/.ova_env_new
  if [ -n \"\$KL\" ]; then
    grep -v -E \"^(WAKE_ENGINE|WAKE_THRESHOLD|WAKE_HITS|WAKE_ACK_BEFORE_REPLY|ZHIPUAI_API_KEY)=\" /etc/ova.env > /tmp/.ova_env_new
  else
    grep -v -E \"^(WAKE_ENGINE|WAKE_THRESHOLD|WAKE_HITS|WAKE_ACK_BEFORE_REPLY)=\" /etc/ova.env > /tmp/.ova_env_new
  fi
  echo \"WAKE_ENGINE=\$K\" >> /tmp/.ova_env_new
  echo \"WAKE_THRESHOLD=\$TH\" >> /tmp/.ova_env_new
  echo \"WAKE_HITS=\$HT\" >> /tmp/.ova_env_new
  echo \"WAKE_ACK_BEFORE_REPLY=\$ACK\" >> /tmp/.ova_env_new
  [ -n \"\$KL\" ] && echo \"\$KL\" >> /tmp/.ova_env_new || true
  cat /tmp/.ova_env_new > /etc/ova.env
  rm -f /tmp/.ova_env_new
  chmod 600 /etc/ova.env
  echo \"engine=\$(grep ^WAKE_ENGINE= /etc/ova.env | cut -d= -f2) threshold=\$(grep ^WAKE_THRESHOLD= /etc/ova.env | cut -d= -f2) hits=\$(grep ^WAKE_HITS= /etc/ova.env | cut -d= -f2) ack=\$(grep ^WAKE_ACK_BEFORE_REPLY= /etc/ova.env | cut -d= -f2) key=\$(grep -c ^ZHIPUAI_API_KEY= /etc/ova.env)\"'"

echo
echo "=== 3.5) 清理旧 systemd 命令行阈值（让 /etc/ova.env 生效）==="
$SSH "sudo sh -c 'set -eu
  unit=/etc/systemd/system/ova-wake.service
  if grep -q -- \"--threshold\" \"\$unit\"; then
    sed -i \"s#^ExecStart=.*#ExecStart=$REMOTE/.venv/bin/python -m ova wake#\" \"\$unit\"
    systemctl daemon-reload
    echo 已移除 ExecStart 中写死的 --threshold/--hits
  else
    echo ExecStart 已由环境变量控制
  fi'"

echo
echo "=== 4) 重启并验证 ==="
$SSH "sudo systemctl restart ova-wake && sleep 6 && echo \"ova-wake: \$(systemctl is-active ova-wake)\" && \
  sudo journalctl -u ova-wake --since '-20s' --no-pager | grep -E 'AUDIO input=|MODEL wake=|E2E_READY|ASR_READY|ERROR|Traceback' | tail -6"

echo
echo "完成：engine=$ENGINE threshold=$WAKE_THRESHOLD hits=$WAKE_HITS ack_before_reply=$WAKE_ACK_BEFORE_REPLY"
echo "实时观察：ssh -i $SSH_KEY $ROBOT 'sudo journalctl -u ova-wake -f | grep -E \"ENGINE |E2E_|REPLY_READY|PLAYBACK|ASR_RESULT\"'"
echo "回退：    ENGINE=pipeline $0"
