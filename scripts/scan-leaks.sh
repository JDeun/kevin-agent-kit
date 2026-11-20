#!/bin/sh
set -eu
directory=$(CDPATH="" cd -- "$(dirname -- "$0")" && pwd)
exec python3 "$directory/scan_leaks.py" "$@"
