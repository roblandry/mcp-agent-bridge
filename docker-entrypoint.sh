#!/bin/sh
set -eu

case "${BRIDGE_MODE:-mcp}" in
  mcp)
    exec python /app/server.py
    ;;
  a2a)
    exec python /app/a2a_bridge.py
    ;;
  *)
    echo "unknown BRIDGE_MODE=${BRIDGE_MODE}; expected mcp or a2a" >&2
    exit 2
    ;;
esac
