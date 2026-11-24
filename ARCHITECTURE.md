# Architecture

- `studio.py` serves the dashboard and dispatches API requests. Exact loopback Host checks, same-origin checks and a session token guard local requests.
- `android.py` handles discovery, app selection, foreground verification, hierarchy and screenshot parsing, and serial-scoped ADB actions.
- `llm.py` implements the versioned model configuration and HTTP adapter.
- `agent.py` records state transitions and orchestrates observation, validated execution, deterministic verification and selector recovery.
- `verification.py` evaluates user-reviewed assertions independently of model output.
- `storage.py` persists settings, plans, runs, strategies and evidence in SQLite.
- `static/` contains the dashboard and DOM-based reporting, including self-contained HTML export and browser print styles.
