#!/bin/sh
cd "$(dirname "$0")" || exit 1
./start-macos.sh --open-browser
studio_status=$?
if [ "$studio_status" -ne 0 ]; then
  printf '\nPress Enter to close this window. '
  read -r studio_acknowledgment
fi
exit "$studio_status"
