# Test results

- `python3 -m unittest discover -s tests -v` passed 10 tests and skipped one real loopback health check because binds are prohibited.
- `python3 -m compileall -q .` passed.
- `node --check static/app.js` passed with the installed Node 12 checker.
- `python3 studio.py --data /tmp/android-test-studio-verify/studio.sqlite` initialized storage, then failed at socket bind with `Operation not permitted`.
- Both discovered ADB installations failed to start their daemon at the local listener. Real emulator smoke tests and browser PDF export remain unverified.
