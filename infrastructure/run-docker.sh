#!/bin/sh
set -eu
export CONTAINER_ENGINE=docker
exec "$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)/run.sh" "$@"
