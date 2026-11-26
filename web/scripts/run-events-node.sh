#!/bin/sh
set -eu

if [ "$#" -ne 2 ]; then
  exit 64
fi

node=$1
bundle=$2
case "$node" in
  /*) ;;
  *) exit 64 ;;
esac
case "$bundle" in
  /*) ;;
  *) exit 64 ;;
esac

if [ "$(/usr/bin/env -i "$node" --version)" != "v24.22.0" ]; then
  exit 64
fi

/usr/bin/env -i "$node" --no-use-env-proxy "$bundle/run-events-api.js"
