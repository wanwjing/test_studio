#!/bin/sh
set -eu
cd "$(dirname "$0")"
exec "${STUDIO_PYTHON:-python3}" studio.py "$@"
