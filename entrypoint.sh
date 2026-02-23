#!/usr/bin/env sh
set -eu

DATA_DIR="${DATA_DIR:-/data}"
CONFIG_PATH_DEFAULT="/config/config.toml"
CONFIG_PATH="${CONFIG_PATH:-$CONFIG_PATH_DEFAULT}"
CONFIG_DIR="$(dirname "$CONFIG_PATH")"
CONFIG_EXAMPLE_SRC="/app/config.example.toml"
CONFIG_EXAMPLE_DST="${CONFIG_DIR}/config.example.toml"

mkdir -p "$DATA_DIR"
mkdir -p "$CONFIG_DIR"

if [ -f "$CONFIG_EXAMPLE_SRC" ] && [ ! -f "$CONFIG_EXAMPLE_DST" ]; then
  cp "$CONFIG_EXAMPLE_SRC" "$CONFIG_EXAMPLE_DST"
fi

if [ ! -f "$CONFIG_PATH" ]; then
  echo "INFO: Optional config file not found at $CONFIG_PATH."
  echo "INFO: Copy $CONFIG_EXAMPLE_DST to $CONFIG_PATH and adjust values if needed."
fi

exec trakt-tracker "$@"
