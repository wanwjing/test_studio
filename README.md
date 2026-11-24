# Android AI Test Studio

Local QA for authorized Android test apps. The bridge uses Python 3.11 or later and the standard library. The dashboard uses HTML, CSS and JavaScript and opens in a modern browser.

## Quick start

Run these commands from the cloned repository. Studio creates a local `studio-config.txt`
on first launch and uses mock AI mode by default.

| System | Launch command |
| --- | --- |
| macOS or Linux | `python3 studio.py --open-browser` |
| Windows | `py -3 studio.py --open-browser` |

Open `http://127.0.0.1:8765` if the browser does not open automatically. The dashboard
can start without an Android device. To run tests, install Android SDK Platform-Tools,
connect an authorized device or launch an emulator, and click **Refresh ADB**.

If you are using an AI coding tool, open the cloned folder and ask it to
“Follow AGENTS.md to set up and verify Android AI Test Studio on this computer.”
The [portable config example](studio-config.example.txt) shows the supported defaults.

## Offline setup

1. Install or transfer Python 3.11+ and Android SDK Platform-Tools before going offline. For emulator testing, prepare an AVD and its system image in advance.
2. Put Platform-Tools on PATH, or enter the absolute path to its `adb` executable in dashboard Settings.
3. On macOS run `./start-macos.sh`. On Windows run `start-windows.cmd`. You can also run `python3 studio.py` with the selected Python interpreter. No Python packages need to be installed.
4. Open `http://127.0.0.1:8765`. Run the startup script with `--diagnostics` to check loopback and ADB. Use `--port 8766` if the default port is in use.
5. Start the emulator or connect your Android test device with USB debugging enabled. Accept the device's authorization prompt. Check `adb devices -l` if the device does not appear.

SQLite is stored under `.android-ai-test-studio` in your home directory. Use `--data` to choose another local database.

## Central configuration

Edit the local `studio-config.txt` in the project folder as UTF-8 JSON. To configure Studio before first launch, copy `studio-config.example.txt` to `studio-config.txt` first. Git ignores the local file. Restart Studio after manual edits. Use `--config /path/to/another-config.txt` to load another profile.

- `server` groups the local host, listening port and browser startup preference. This release requires `127.0.0.1`. The macOS double-click launcher requests browser startup explicitly.
- `database_path` controls where plans, history, and screenshot evidence are stored. Relative paths are resolved from the configuration file's folder.
- `settings` groups the ADB executable, AI model configuration, execution limits, and report screenshot preference. An empty `adb` uses PATH. Dashboard setting changes are saved to the same file.
- `settings.llm.api_key_env` names the environment variable holding the API key. Keep the key itself in the environment.

Command-line `--port` and `--data` override the file for the current launch. The first launch without a configuration file imports eligible settings from the selected existing SQLite database.

## Launch and preview an Android emulator

In Device Explorer, select an Android Virtual Device and choose **Launch and connect**. Studio waits for Android to finish booting, selects the corresponding ADB device, and shows the emulator screen inside Live Screen Viewer and tries to refresh it every 300 ms while the tab is visible. Set `emulator_preview_interval_ms` in `studio-config.txt` to adjust the target. Actual refresh speed depends on ADB screenshot capture time. With `emulator_headless` set to `true` and **Show Android emulator window** unchecked, Android runs in the background and its screen appears in Live Screen Viewer. Use **Show live emulator** to return to the live preview after inspecting a test app. Studio automatically previews a single detected emulator when the dashboard opens. Use **Refresh ADB** if you start the emulator afterward, or select one in Device Explorer when multiple emulators are attached.

Studio finds the emulator next to Android SDK Platform-Tools or on PATH. Set `emulator_path` in `studio-config.txt` to override discovery. The optional `emulator_skin_overrides` object maps AVD names to display resolutions for devices with a missing skin. Startup logs are written to `emulator.log` beside the Studio database.

To control the emulator from Studio, choose **Show live emulator** in Live Screen Viewer. The preview enables controls for the selected emulator. Click the displayed Android screen to tap, or use **Emulator Back** and **Emulator Home**. Live controls are available for emulators and pause while a test run owns the device.

If ADB is awaiting approval on a local emulator, Live Screen Viewer uses the emulator console for screenshots and taps. Select **Always allow from this computer** and tap **Allow** in the viewer. Studio then connects ADB and loads the app list. This works offline and uses the installed Android SDK tools.

## Run a test

Refresh devices, select a device and app, confirm authorization, and launch the app. To install apps from your computer, select one or more standalone APKs under **Local APK files** and click **Install APKs**. Studio installs them on the selected device and refreshes the application list. Refresh the screen to obtain a screenshot and hierarchy. To act manually, click the screenshot, tap a selected element, or send fixture text into a focused field.

Generate a plan from an objective under **AI plan tools** or open **Edit plan JSON** in **Plan Library**. Review every step and its expected result before starting. The editor accepts actions with `schema_version: 1` and a step-level `assertions` array. A typical assertion is `{"type":"element_visible","target":{"strategy":"resource_id","value":"com.example:id/result"}}`. A step may use an `instruction` in place of the action for runtime model selection. Save the plan to use it in regression runs.

In **Plan Library**, open a saved plan, choose **Use Google Maps template**, or import JSON plans. Save your changes, then use **Add to batch** or select plans directly in **Test Workspace**. The preview shows the objective, setup requirements, actions, and expected results. Check the readiness list for the selected device, required app, plan validation, and authorization, then choose **Run selected plans**. The [Google Maps template](static/google-maps-test-plan.json) opens Maps, taps **Layers**, and checks for **Map type**. Plans with explicit actions run offline through ADB, with local results and screenshots. A missing or ambiguous target blocks the step and stops the run.

Follow progress in **Test Workspace**. When the run finishes, use **View report** to inspect evidence or **Print / save PDF** to export it. The separate **Reports** menu lists previous runs with their start time, result, and passed-step count. Select a run to see its report. **Save HTML** downloads a self-contained report. **Print / save PDF** opens the browser print dialog.

### Explore an APK and generate a test case

Connect a model in Settings, install the APK on an emulator, select its app, and confirm authorization. Under **AI plan tools**, enter an optional objective such as “Open the settings screen,” then choose **Explore APK**. Studio restarts the app process, keeps its data, and explores up to 12 taps, scrolls, or Back actions. With vision enabled, exploration sends screenshots to the configured model alongside the UI hierarchy.

Studio saves the observed path as a test case and immediately replays it. Review the generated assertions in **Plan Library** and the replay result in Reports. Visual coordinate taps require the recorded screenshot to match on replay, so animations or changing status bars can block those steps. Use a dedicated test emulator and fixture data.

## Connect a local model

Mock mode is the default. It supports a connection check and deterministic report text. Enter reviewed actions in the plan to test without an inference server.

In **Settings**, edit the AI model fields directly or import a version 1 model JSON file to populate them. Use **View or edit settings JSON** to see the full editable settings. You can also start Studio with `--llm-config /path/to/plugin.json`. For an existing plugin, map its HTTP endpoint and model to this format. Editor extensions without a compatible HTTP API need a separate bridge.

```json
{
  "schema_version": 1,
  "provider": "openai-compatible",
  "endpoint_url": "http://127.0.0.1:8080/v1/chat/completions",
  "model": "local-model",
  "vision": false,
  "timeout_seconds": 20,
  "temperature": 0.1,
  "max_output_tokens": 2048
}
```

Use `provider: "ollama"` with the server's full chat endpoint for the Ollama format. Set `api_key_env` to the name of an environment variable if authentication is needed. Use **Test connection** before applying. Both connection testing and activation run a minimal inference request.

If a device goes offline, refresh the device list after reconnecting and start a fresh run. If screenshot capture works through ADB but Studio reports missing hierarchy, check whether the target screen permits UI Automator inspection. If model tests fail, check the model name, URL, supported response format, and any required credential environment variable.

## Verify

Run `python3 -m unittest discover -s tests -v`. The device and HTTP transport fixtures use mocks or in-process handlers. For real emulator checks, install a QA fixture app with an empty editable text field and a navigation button leading to an identifiable destination. Supply its package and selectors:

```sh
python3 smoke.py --authorized --serial emulator-5554 --package com.example.fixture \
  --field com.example.fixture:id/name --next com.example.fixture:id/continue_button \
  --destination com.example.fixture:id/destination
```

See [test results](TEST_RESULTS.md) and [known issues](KNOWN_ISSUES.md) for the recorded verification limits.
