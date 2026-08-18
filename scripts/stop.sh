#!/bin/zsh
set -euo pipefail

PROJECT_ROOT="${0:A:h:h}"
HERMES_HOME="$PROJECT_ROOT/.runtime/hermes-home"
HERMES_BIN="${HERMES_BIN:-/Users/aventador/.hermes/hermes-agent/venv/bin/hermes}"

export HERMES_HOME
exec "$HERMES_BIN" gateway stop
