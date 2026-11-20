#!/bin/sh
set -eu
directory=$(CDPATH="" cd -- "$(dirname -- "$0")" && pwd)
action=$(basename -- "$0" .sh)
exec python3 "$directory/package.py" "$action" "$@"
