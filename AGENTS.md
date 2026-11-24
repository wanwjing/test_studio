# Android AI Test Studio

## First setup after cloning

1. Run commands from the repository root. Check for Python 3.11 or newer using
   `python3 --version` on macOS/Linux or `py -3 --version` on Windows. The
   bridge uses the Python standard library and the UI uses plain HTML, CSS,
   and JavaScript. No pip or npm install is needed.
2. Preserve any existing `studio-config.txt`. Studio creates this ignored
   local file on its first launch. For manual setup, copy
   `studio-config.example.txt` to `studio-config.txt` only if it is missing.
   Keep the server on `127.0.0.1` and use the default mock model for setup.
3. Run `python3 -m unittest discover -s tests` or
   `py -3 -m unittest discover -s tests` on Windows.
4. Launch with `python3 studio.py --open-browser` or
   `py -3 studio.py --open-browser` on Windows. Verify
   `http://127.0.0.1:8765/api/health` returns `"status": "ok"` and report the
   dashboard URL. Use `--port 8766` if another service occupies the default
   port. If an older Studio process holds the port, check for active tests
   before restarting it.
5. For Android execution, check `adb devices -l`. If ADB is installed outside
   PATH, set `settings.adb` to that computer's absolute executable path in
   the local config or save it from Studio Settings. If ADB is missing,
   identify Android SDK Platform-Tools as the missing prerequisite. An
   emulator also needs a configured AVD and system image. Wait for the user
   to authorize the connected device and app before running tests.

## Files to keep local

Keep local configuration, API keys, databases, and run evidence out of Git.
Configure optional model credentials through the environment variable named
by `settings.llm.api_key_env`. If verifying a fresh install on a machine that
already uses Studio, pass temporary paths via `--config` and `--data` so the
existing installation and reports are preserved.
