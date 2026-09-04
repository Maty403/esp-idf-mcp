# esp-idf-mcp

A [Model Context Protocol](https://modelcontextprotocol.io) (MCP) server that lets AI agents build, flash, and monitor **ESP-IDF** projects on real hardware — end to end, from source code to boot logs.

Instead of an agent only being able to *write* firmware code, this server closes the loop: compile → flash over serial → capture boot logs → run `pytest-embedded` hardware tests — all without leaving the agent's tool set.

## Features

- **Build without idf.py boilerplate** — configures with CMake and builds with Ninja directly; only reconfigures when `CMakeCache.txt` is missing.
- **Flash + auto-monitor in one call** — flashes with `esptool` (reads `build/flash_args`), waits out the hard-reset boot, then optionally opens the serial port, resets the board, and collects logs. Closes monitor sessions holding the port first, so flashing never deadlocks on a busy COM port.
- **Session-based serial monitor** — `monitor_open` / `monitor_read` / `monitor_send` / `monitor_close`: a persistent, non-blocking session you poll for *new* lines only, with regex-based `wait_for` early return.
- **One-shot log capture** — `serial_start`: open, reset, collect a boot window (or wait for a regex like `error|panic|Guru Meditation`), return, auto-close.
- **Agent-friendly output** — strips ANSI color escapes, collapses repeated lines (`(xN)`) to save tokens, keeps only the relevant tail of long build logs.
- **Survives USB re-enumeration** — after a reset the port can disappear and come back (ESP32-S2/USB-OTG); the read loop reconnects for up to 10 s and clears stale buffered logs.
- **Hardware-in-the-loop tests** — runs `pytest-embedded` suites against the real board.

Includes a Windows `usbser.sys` workaround (RTS-only control transfers need a DTR re-assert) so reset works on USB-CDC ports as well as CH340-style adapters.

## Tools

| Tool | Purpose |
| --- | --- |
| `build_project` | CMake configure (if needed) + Ninja build |
| `flash_project` | esptool flash, optional auto serial monitor with `wait_for` regex |
| `set_target` | `idf.py set-target` (esp32, esp32s3, esp32c2, …) |
| `add_dependency` / `remove_dependency` | Manage ESP component manager dependencies in `idf_component.yml` |
| `clean_project` | Incremental clean or fullclean |
| `monitor_open` / `monitor_read` / `monitor_send` / `monitor_close` | Persistent interactive serial session |
| `serial_start` | One-shot capture: open → reset → collect → close |
| `run_pytest` | Run `pytest-embedded` hardware tests |
| `project://devices` (resource) | List connected serial ports |

## Requirements

- [ESP-IDF](https://docs.espressif.com/projects/esp-idf/) (developed against **v6.0.2** on **Windows**; other versions/OSes should work if the env vars below point at your install)
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

All paths are read from environment variables at startup and fall back to defaults for a standard Windows ESP-IDF install:

| Env var | Default | Meaning |
| --- | --- | --- |
| `IDF_PATH` | `C:\esp\v6.0.2\esp-idf` | ESP-IDF framework root |
| `IDF_PYTHON_ENV_PATH` | `C:\Espressif\tools\python\v6.0.2\venv` | ESP-IDF Python virtualenv |
| `ESP_IDF_VERSION` | `6.0.2` | Version label |
| `IDF_COMPONENT_MANAGER` | `1` | Enables the component manager (`idf.py add-dependency`, managed components) |

Toolchain directories (xtensa/riscv GCC, CMake, Ninja, idf-exe) under `C:\Espressif\tools` are prepended to `PATH` when present and skipped when missing.

## Typical agent workflow

```
set_target(esp32c2) → build_project → flash_project(monitor_baud=74880,
wait_for="ip_ready") → iterate on code → run_pytest
```

## License

[MIT](LICENSE)
