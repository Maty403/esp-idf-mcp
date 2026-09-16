# esp-idf-mcp


A [Model Context Protocol](https://modelcontextprotocol.io) (MCP) server that lets AI agents build, flash, and monitor **ESP-IDF** projects on real hardware — end to end, from source code to boot logs.

Instead of an agent only being able to *write* firmware code, this server closes the loop: compile → flash over serial → capture boot logs → run `pytest-embedded` hardware tests — all without leaving the agent's tool set.

## Features

- **Zero-config ESP-IDF discovery** — IDF root, tools directory and the Python venv are located automatically (env var first, else the newest install under `D:\esp`, `C:\esp` or `~/esp`); toolchain paths are matched by version glob, so upgrading ESP-IDF or a toolchain never requires editing the script.
- **Builds through `idf.py build`** — the exact same path as a manual build, so hand-edited `sdkconfig` values are picked up through the official ninja RERUN_CMAKE flow instead of being silently reverted by a kconfgen rewrite. `sdkconfig.defaults*` changes (an upstream blind spot — ninja never notices them) are detected with a stateless mtime check against `build/build.ninja` and automatically chained with `idf.py reconfigure`, so defaults edits take effect on the next build with zero extra cost when nothing changed.
- **Flash + auto-monitor in one call** — flashes with `esptool` (reads `build/flash_args`), waits out the hard-reset boot, then opens a persistent monitor session on that port (the board is reset, so the session only holds the fresh boot log) and returns immediately — poll it with `monitor_read`. The target port is the one you pass in (auto-detected as the first non-COM1 port when omitted), and only that port's monitor session is closed before flashing (skipped when none is open) — other boards are never disturbed.
- **Chip info from real hardware** — `read_chip_info` reports chip model, revision, features, crystal, MAC address, and flash vendor/device/size via esptool, then hard-resets the board back into the running app.
- **Baud auto-detected** — every serial capture reads `CONFIG_ESP_CONSOLE_UART_BAUDRATE` from the project `sdkconfig` (no hardcoded default), so logs are never garbled.
- **Session-based serial monitor** — `monitor_open` / `monitor_read` / `monitor_send` / `monitor_close`: a persistent, non-blocking session you poll for *new* lines only, with regex-based `wait_for` early return. `monitor_close(session_id)` releases exactly the session — or every session on a given port — that you name. `flash_project` opens one automatically after every flash, so the boot log is always ready to poll.
- **Agent-friendly output** — strips ANSI color escapes, collapses repeated lines (`(xN)`) to save tokens, keeps only the relevant tail of long build logs.
- **Survives USB re-enumeration** — after a reset the port can disappear and come back (ESP32-S2/USB-OTG); the read loop reconnects for up to 10 s and clears stale buffered logs.
- **Hardware-in-the-loop tests** — runs `pytest-embedded` suites against the real board.

Includes a Windows `usbser.sys` workaround (RTS-only control transfers need a DTR re-assert) so reset works on USB-CDC ports as well as CH340-style adapters.

## Tools

| Tool | Purpose |
| --- | --- |
| `build_project` | `idf.py build` (auto-reconfigures when `sdkconfig.defaults*` changed since the last configure) |
| `flash_project` | esptool flash (target port auto-detected as the first non-COM1 port when omitted); closes only that port's monitor session before flashing, then opens a persistent monitor session on it (baud from `sdkconfig`) for `monitor_read` |
| `read_chip_info` | Chip model, revision, features, crystal, MAC, flash vendor/device/size |
| `set_target` | `idf.py set-target` (esp32, esp32s3, esp32c2, …) |
| `add_dependency` / `remove_dependency` | Manage ESP component manager dependencies in `idf_component.yml` |
| `clean_project` | Incremental clean or fullclean |
| `monitor_open` / `monitor_read` / `monitor_send` / `monitor_close` | Persistent interactive serial session; `monitor_close(session_id)` (or a bare port name) releases exactly that session / port |
| `run_pytest` | Run `pytest-embedded` hardware tests |
| `project://devices` (resource) | List connected serial ports |

## Requirements

- [ESP-IDF](https://docs.espressif.com/projects/esp-idf/) (developed against **v6.1** on **Windows**; other layouts work as long as the auto-detection or the env vars below find your install)
- Python ≥ 3.9 with `mcp` and `pyserial`
- An MCP client (ZCode, Claude Desktop, Cursor, …)

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
→ monitor_read(session_id, wait_for="ip_ready") → iterate on code → run_pytest
```

## License

[MIT](LICENSE)
                                                                      ——————反方向的K
