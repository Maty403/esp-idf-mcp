# Changelog

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
