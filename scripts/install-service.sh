#!/bin/zsh
set -euo pipefail

PROJECT_ROOT="${0:A:h:h}"
HERMES_HOME="$PROJECT_ROOT/.runtime/hermes-home"
HERMES_BIN="${HERMES_BIN:-/Users/aventador/.hermes/hermes-agent/venv/bin/hermes}"
CONFIG_SOURCE="$PROJECT_ROOT/config/hermes.config.local.yaml"
CONFIG_TARGET="$HERMES_HOME/config.yaml"

mkdir -p "$HERMES_HOME"
if [[ -L "$CONFIG_TARGET" ]]; then
  if [[ "${CONFIG_TARGET:A}" != "${CONFIG_SOURCE:A}" ]]; then
    print -u2 "现有 config.yaml 指向了其他配置，请先检查：$CONFIG_TARGET"
    exit 1
  fi
elif [[ -e "$CONFIG_TARGET" ]]; then
  print -u2 "现有 config.yaml 不是受管控配置链接，请先检查：$CONFIG_TARGET"
  exit 1
else
  ln -s "$CONFIG_SOURCE" "$CONFIG_TARGET"
fi

if [[ ! -f "$HERMES_HOME/.env" ]]; then
  print -u2 "缺少运行密钥文件：$HERMES_HOME/.env"
  exit 1
fi

export HERMES_HOME
exec "$HERMES_BIN" gateway install --force --start-now --start-on-login
