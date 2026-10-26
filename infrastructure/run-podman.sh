#!/bin/sh
set -eu
export CONTAINER_ENGINE=podman
exec "$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)/run.sh" "$@"
