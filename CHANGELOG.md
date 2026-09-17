# Changelog

## v0.4.1 (2026-09-18)

- **Persistent full serial log per session** — every line the monitor receives is flushed to `<project_dir>/.esp_monitor_full.log` (or a temp-dir file for standalone opens) as it arrives. The ring buffer only holds the recent 20 000 lines, but the file keeps everything: history survives buffer eviction, USB re-enumeration, board resets and session close. Session open, board reset and port re-connect write separator lines into the file; `monitor_open` returns the path.
- **`monitor_read(full=True)` redefined as a cursor-independent full dump** — it now emits every line retained in the ring buffer verbatim (no folding, no layer split, timestamps untouched) and does *not* advance the incremental cursor, so the folded and full views can be interleaved freely without one consuming the other's lines. Previously `full=True` shared the cursor with the folded view, so a folded read consumed the lines and `full=True` afterwards reported `0 new lines`. `max_lines` now caps the tail in full mode too (0 = everything retained).
- Ring buffer raised from 5 000 to 20 000 lines; evictions are still reported honestly in the header, and anything evicted lives in the log file.
- All remaining Chinese header docs and inline comments translated to English — the tool docstrings (what MCP clients show their agents) are now uniformly English.

## v0.4.0 (2026-09-18)

- `build_project` is back to a plain `idf.py build`: the in-place `sdkconfig.defaults` merge added in v0.3.0 was removed, so `sdkconfig.defaults` keeps its official upstream semantics again — the defaults files seed a fresh `sdkconfig` (generic one, plus `sdkconfig.defaults.<target>` when the generic one exists), while values already present in `sdkconfig` win. To fold menuconfig results back into the defaults files, use the upstream `idf.py save-defconfig`.
- `monitor_read` reworked. Output now starts at the runtime marker `main_task: Returned from app_main()`: everything before it (boot logs *and* `app_main()` initialization output) is folded into a count (`N lines folded before app start` in the header) instead of being printed, and the marker plus every following runtime line is shown one per line. Adjacent duplicates collapse to `<first line>  *N`, so the repeated content is always kept next to its count (compared with the leading `I (12345) ` prefix ignored, so lines logged at different milliseconds still collapse). New signature `monitor_read(session_id, full=False, timestamp=False, max_lines=200)`: `full=True` returns the increment verbatim — no folding, no layer split, timestamps untouched — as an escape hatch; `timestamp=True` keeps the ms prefix. The earlier `system_layer` / `app_layer` / `wait_for` parameters are gone.
- Serial robustness: the log-level regex now covers `V`/`D` as well as `I`/`W`/`E`; `deque` reads are retried when the reader thread mutates the buffer concurrently; port names are matched case-insensitively; a reconnect drops the half-parsed line; a failed reconnect now surfaces in `monitor_read` output instead of silently reporting zero new lines.
- Port selection: auto-detection only picks a port when exactly one non-COM1 port is connected — with several ports present it refuses and lists them instead of guessing, so a flash cannot land on the wrong board.
- `_run_sync` kills the whole process tree on timeout (`taskkill /F /T` on Windows), so `cmake`/`ninja` children cannot be left running in the background.
- README: added a full tool reference generated from the tools' own descriptions.

## v0.3.0 (2026-09-17)

- `build_project` now runs `idf.py build` — the exact same path as a manual build — instead of bare CMake + Ninja. Hand-edited `sdkconfig` values are picked up through the official ninja RERUN_CMAKE flow and no longer get silently reverted by a kconfgen rewrite.
- `sdkconfig.defaults*` changes are detected automatically: those files are not in ninja's reconfigure trigger list (an upstream blind spot), so `build_project` merges the defaults assignments into `sdkconfig` in place before every build (values overridden, missing lines appended, no file deleted; symbols not mentioned in the defaults are never touched; backup files such as `.bak`/`.old` are ignored). Defaults edits take effect without manually deleting `sdkconfig`.
- `flash_project` intentionally unchanged: pure `esptool` flash of existing build artifacts, no implicit build step — run `build_project` first after changing sources or config.

## v0.2.0 (2026-09-04)

- Zero-config ESP-IDF discovery (env vars first, else newest install under common roots).
- New `read_chip_info` tool (chip model, revision, features, crystal, MAC, flash details via esptool).
- Serial baud auto-detected from the project `sdkconfig` (`CONFIG_ESP_CONSOLE_UART_BAUDRATE`) — no hardcoded default.
- Persistent, session-based serial monitor (`monitor_open` / `monitor_read` / `monitor_send` / `monitor_close`) with regex `wait_for`, ANSI stripping and repeated-line collapsing; `flash_project` opens a monitor session automatically after flashing.
- Windows `usbser.sys` workaround so RTS-based reset also works on USB-CDC ports.
