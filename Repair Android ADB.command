#!/bin/sh
cd "$(dirname "$0")" || exit 1
./start-macos.sh --repair-adb
repair_status=$?
if [ "$repair_status" -eq 0 ]; then
  printf '\nReturn to Studio and click Connect via ADB.\n'
fi
printf '\nPress Enter to close this window. '
read -r studio_acknowledgment
exit "$repair_status"
