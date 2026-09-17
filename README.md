# esp-idf-mcp


A [Model Context Protocol](https://modelcontextprotocol.io) (MCP) server that lets AI agents build, flash, and monitor **ESP-IDF** projects on real hardware — end to end, from source code to boot logs.

Instead of an agent only being able to *write* firmware code, this server closes the loop: compile → flash over serial → capture boot logs → run `pytest-embedded` hardware tests — all without leaving the agent's tool set.

## Features

- **Zero-config ESP-IDF discovery** — IDF root, tools directory and the Python venv are located automatically (env var first, else the newest install under `D:\esp`, `C:\esp` or `~/esp`); toolchain paths are matched by version glob, so upgrading ESP-IDF or a toolchain never requires editing the script.
- **Builds through `idf.py build`** — the exact same path as a manual build, with no extra configuration rewriting. `sdkconfig` and `sdkconfig.defaults` therefore keep their official upstream semantics: the defaults files seed a fresh `sdkconfig` (the generic `sdkconfig.defaults`, plus `sdkconfig.defaults.<target>` when the generic one exists), while values already present in `sdkconfig` win on later builds. Hand edits in `sdkconfig` are picked up through the official ninja `RERUN_CMAKE` flow.
- **Flash + auto-monitor in one call** — flashes with `esptool` (reads `build/flash_args`), waits out the hard-reset boot, then opens a persistent monitor session on that port (the board is reset, so the session only holds the fresh boot log) and returns immediately — poll it with `monitor_read`. The target port is the one you pass in (auto-detected as the first non-COM1 port when omitted), and only that port's monitor session is closed before flashing (skipped when none is open) — other boards are never disturbed.
- **Chip info from real hardware** — `read_chip_info` reports chip model, revision, features, crystal, MAC address, and flash vendor/device/size via esptool, then hard-resets the board back into the running app.
- **Baud auto-detected** — every serial capture reads `CONFIG_ESP_CONSOLE_UART_BAUDRATE` from the project `sdkconfig` (no hardcoded default), so logs are never garbled.
- **Session-based serial monitor** — `monitor_open` / `monitor_read` / `monitor_send` / `monitor_close`: a persistent, non-blocking session you poll for *new* lines only. `monitor_close(session_id)` releases exactly the session — or every session on a given port — that you name. `flash_project` opens one automatically after every flash, so the boot log is always ready to poll.
- **Application-layer log view** — `monitor_read` splits the log stream at the boot marker `main_task: Calling app_main()`: system/boot lines before it are folded into a count (`N system lines folded` in the header), while the marker and everything after it (app, `wifi`, `mqtt`, plain `printf`) is printed line by line. An increment that contains no marker (a plain continuation) is printed as-is, so no line is ever swallowed.
- **Agent-friendly output** — strips ANSI color escapes, collapses adjacent duplicates into `<line>  *N` (compared with the leading `I (12345) ` prefix ignored, so the same message logged at different milliseconds still collapses), keeps only the relevant tail of long output, and can keep or strip the ms prefix via the `timestamp` switch.
- **Survives USB re-enumeration** — after a reset the port can disappear and come back (ESP32-S2/USB-OTG); the read loop reconnects for up to 10 s and clears stale buffered logs.
- **Hardware-in-the-loop tests** — runs `pytest-embedded` suites against the real board.

Includes a Windows `usbser.sys` workaround (RTS-only control transfers need a DTR re-assert) so reset works on USB-CDC ports as well as CH340-style adapters.

## Tools

| Tool | Purpose |
| --- | --- |
| `build_project` | `idf.py build` (same as a manual build, no configuration rewriting) |
| `flash_project` | esptool flash; when `port` is omitted it is auto-detected (only if exactly one non-COM1 port is connected — otherwise the call refuses and lists the candidates); closes only that port's monitor session before flashing, then opens a persistent monitor session on it (baud from `sdkconfig`) for `monitor_read` |
| `read_chip_info` | Chip model, revision, features, crystal, MAC, flash vendor/device/size |
| `set_target` | `idf.py set-target` (esp32, esp32s3, esp32c2, …) |
| `add_dependency` / `remove_dependency` | Manage ESP component manager dependencies in `idf_component.yml` |
| `clean_project` | Incremental clean or fullclean |
| `monitor_open` / `monitor_read` / `monitor_send` / `monitor_close` | Persistent interactive serial session; `monitor_read` shows the application layer from `Calling app_main()` on (system lines folded to a count), folds adjacent duplicates and offers a `timestamp` switch; `monitor_close(session_id)` (or a bare port name) releases exactly that session / port |
| `run_pytest` | Run `pytest-embedded` hardware tests |
| `project://devices` (resource) | List connected serial ports |

### Tool reference

The description each tool exposes to the agent, in full:

**`build_project(project_dir, full_log=False)`** — Build ESP-IDF project via `idf.py build` (same as manual). `full_log=True` returns the complete output instead of the tail. Configuration is untouched: `sdkconfig.defaults` seeds a fresh `sdkconfig`, values already in `sdkconfig` win.

**`flash_project(project_dir, port=None, monitor=True, wait_after_flash=2.0)`** — Flash the built project using `esptool` directly (reads `build/flash_args`). Monitor sessions on the target port are closed automatically before flashing — no manual `monitor_close` needed. When `port` is omitted it is auto-detected, but only if exactly one non-COM1 port is connected; with several ports connected the call refuses and lists them, so a flash can never land on the wrong board. `monitor=True` opens a persistent session afterwards (baud read from `sdkconfig`); `wait_after_flash` lets the hard-reset boot finish before the monitor opens, so the session holds a clean boot log.

**`read_chip_info(port=None, baud=115200)`** — Chip model, revision, features, crystal frequency, MAC address and flash vendor/device/size via `esptool`. The board is briefly put into download mode and hard-reset back into the running app afterwards. PSRAM details are **not** available here — read the boot log with `monitor_open` instead.

**`monitor_open(port, reset=True, project_dir=None)`** — Open a persistent serial monitor session and return immediately (non-blocking). `reset=True` hard-resets the board after clearing the buffers, so the session starts from a fresh boot. `project_dir` enables console-baud auto-detection from `sdkconfig` (`CONFIG_ESP_CONSOLE_UART_BAUDRATE`); standalone opens fall back to 115200. The session id is `PORT@BAUD` (e.g. `COM17@115200`).

**`monitor_read(session_id, timestamp=False, max_lines=200)`** — Read the lines that arrived since the last read. The stream is split at the application start marker `main_task: Calling app_main()` (or the first log line tagged `main`): system lines before it are folded into a count — reported as `N system lines folded` in the header — while the marker and everything after it is printed line by line. Adjacent duplicates collapse into `<line>  *N`, and comparison ignores the leading `I (12345) ` prefix, so the same message logged at different milliseconds still collapses. `timestamp=True` keeps that prefix (default strips it to `TAG: msg`). `max_lines` caps the returned lines after folding. An increment with no marker at all is printed as-is.

**`monitor_send(session_id, data, press_enter=True)`** — Write text to the device's serial input (shell commands, menu selections); `press_enter` appends CRLF.

**`monitor_close(session_id)`** — Release one session (`COM17@115200`) or every session on a bare port (`COM17`). There is no release-all mode.

**`set_target(project_dir, target)`** — `idf.py set-target` for esp32 / esp32s3 / esp32c2 / … Note that `idf.py` renames the existing `sdkconfig` to `sdkconfig.old` and generates a fresh one for the new target.

**`add_dependency(project_dir, dependency, component=None, path=None)` / `remove_dependency(project_dir, dependency)`** — Manage the ESP component-manager manifest `idf_component.yml`; components are fetched or pruned on the next build.

**`clean_project(project_dir, full=False)`** — `idf.py clean` or `fullclean` (build artifacts only; `sdkconfig` is never touched).

**`run_pytest(project_dir, ...)`** — Run `pytest-embedded` hardware tests against the real board (flash + interact + assert on serial output).

**Resource `project://devices`** — JSON list of the connected serial ports.

## Requirements

- [ESP-IDF](https://docs.espressif.com/projects/esp-idf/) (developed against **v6.1** on **Windows**; other layouts work as long as the auto-detection or the env vars below find your install)
- Python ≥ 3.9 with `mcp` and `pyserial`
- Any MCP client (Claude Desktop, Cursor, VS Code, …)

## Setup

### Option A — install as a package

```bash
pip install .
```

### Option B — run the single file directly

Just point your MCP client at `esp_idf_mcp.py` with any Python that has `mcp` + `pyserial` installed (the ESP-IDF venv works well).

### Register with your MCP client

```json
{
  "mcpServers": {
    "esp-idf": {
      "command": "python",
      "args": ["C:\\path\\to\\esp_idf_mcp.py"]
    }
  }
}
```

(or `"command": "esp-idf-mcp"` with no args if installed via pip)

## Configuration

ESP-IDF is located at startup automatically: environment variable first, then the newest install found under `D:\esp\<ver>\esp-idf`, `C:\esp\<ver>\esp-idf` or `~/esp/<ver>/esp-idf`. The tools directory falls back to `C:\Espressif\tools`, then `D:\espressif\tools`. Every path can be overridden from outside:

| Env var | Meaning |
| --- | --- |
| `IDF_PATH` | ESP-IDF framework root (overrides auto-detection) |
| `IDF_PYTHON_ENV_PATH` | ESP-IDF Python virtualenv (default: newest under `<tools>\python\*\venv`) |
| `IDF_TOOLS_PATH` | Espressif tools directory (default: `C:\Espressif\tools`, then `D:\espressif\tools`) |
| `ESP_IDF_VERSION` | Version label — derived from the IDF directory name with the leading `v` stripped (`v6.1` → `6.1`), so the component manager can parse it |
| `IDF_COMPONENT_MANAGER` | Forced to `1` so `idf.py add-dependency` and managed components resolve |

Toolchain directories (xtensa/riscv GCC, CMake, Ninja, ccache, idf-exe, esp-rom-elfs) under the tools path are matched by version glob and prepended to `PATH`. A one-line diagnostic (`IDF_PATH=… TOOLS=… PYENV=…`) is printed to stderr at startup.

## Typical agent workflow

```
read_chip_info → set_target(esp32s3) → build_project → flash_project(port="COM17")
→ monitor_read(session_id) → iterate on code → run_pytest
```

## License

[MIT](LICENSE)
                                                                      ——————反方向的K
