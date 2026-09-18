"""ESP-IDF MCP server: build, flash and serial monitor tools for ESP-IDF projects."""
import atexit
import json
import os
import re
import tempfile
import subprocess
import sys
import threading
import time
from collections import deque
from typing import Optional

import serial

try:
    from serial.tools import list_ports
except ImportError:
    list_ports = None

# === ESP-IDF auto-discovery: env var first, else scan known install roots for the newest version ===
import glob as _glob


def _newest(pattern):
    """Newest matching dir for a wildcard path pattern (None if no match)."""
    matches = sorted(m for m in _glob.glob(pattern) if os.path.isdir(m))
    return matches[-1] if matches else None


def _find_idf_path():
    """env IDF_PATH -> newest */esp-idf under common install roots."""
    env = os.environ.get('IDF_PATH')
    if env and os.path.isfile(os.path.join(env, 'tools', 'idf.py')):
        return env
    for base in (r'D:\esp', r'C:\esp', os.path.join(os.path.expanduser('~'), 'esp')):
        if not os.path.isdir(base):
            continue
        found = [os.path.join(base, v, 'esp-idf') for v in os.listdir(base)
                 if os.path.isfile(os.path.join(base, v, 'esp-idf', 'tools', 'idf.py'))]
        if found:
            return sorted(found)[-1]  # highest version wins
    raise SystemExit('ESP-IDF not found: set IDF_PATH, or install under D:\\esp\\<ver>\\esp-idf')


def _find_tools_dir():
    """env IDF_TOOLS_PATH -> classic installer locations."""
    env = os.environ.get('IDF_TOOLS_PATH')
    if env and os.path.isdir(env):
        return env
    for cand in (r'C:\Espressif\tools', r'D:\espressif\tools'):
        if os.path.isdir(cand):
            return cand
    raise SystemExit('Espressif tools dir not found: set IDF_TOOLS_PATH')


os.environ['PYTHON_DEPS_CHECKED'] = '1'
IDF_PATH = _find_idf_path()
os.environ['IDF_PATH'] = IDF_PATH
# The dir name looks like 'v6.1', but idf_component_manager's Version.coerce chokes on the
# leading 'v' (raises "Version string lacks a numerical component"), which breaks
# clean_project / set_target / add_dependency (the idf.py-based tools).
# So strip the leading 'v' here: 'v6.1' -> '6.1'
os.environ['ESP_IDF_VERSION'] = os.path.basename(os.path.dirname(IDF_PATH)).lstrip('vV')
# Enable the Component Manager: lets idf.py add-dependency / build resolve and fetch
# components (e.g. esp-nn, esp-tflite-micro). Leaving it off breaks dependency resolution.
os.environ['IDF_COMPONENT_MANAGER'] = '1'

_tools = _find_tools_dir()
os.environ['IDF_TOOLS_PATH'] = _tools
# IDF python venv: env var first, else the newest under tools/ (the installer generates one per IDF version)
_python_env = os.environ.get('IDF_PYTHON_ENV_PATH') or _newest(os.path.join(_tools, 'python', '*', 'venv'))
if not _python_env or not os.path.isdir(_python_env):
    raise SystemExit('IDF python venv not found: set IDF_PYTHON_ENV_PATH')
os.environ['IDF_PYTHON_ENV_PATH'] = _python_env
IDF_PYTHON = os.path.join(_python_env, 'Scripts', 'python.exe')  # used for every subprocess (esptool/idf.py/pytest)

# Toolchain dirs matched by version wildcard (IDF/toolchain upgrades need no code change), injected into PATH
_extra_paths = [
    _newest(os.path.join(_tools, 'xtensa-esp-elf', '*', 'xtensa-esp-elf', 'bin')),
    _newest(os.path.join(_tools, 'riscv32-esp-elf', '*', 'riscv32-esp-elf', 'bin')),
    _newest(os.path.join(_tools, 'esp32ulp-elf', '*', 'esp32ulp-elf', 'bin')),
    _newest(os.path.join(_tools, 'cmake', '*', 'bin')),
    _newest(os.path.join(_tools, 'ninja', '*')),
    _newest(os.path.join(_tools, 'idf-exe', '*')),
    # ninja needs ccache findable as the compiler launcher, otherwise it fails with
    # "CreateProcess failed: The system cannot find the file specified"
    # EIM layout nests one level deeper: ccache\\<ver>\\ccache-<ver>-windows-x86_64\\ccache.exe
    _newest(os.path.join(_tools, 'ccache', '*', 'ccache-*')) or _newest(os.path.join(_tools, 'ccache', '*')),
    os.path.join(IDF_PATH, 'tools'),
    os.path.join(_python_env, 'Scripts'),
]

# Required to generate the esp_rom gdbinit; project.cmake emits a CMake warning without it
_rom_elfs = _newest(os.path.join(_tools, 'esp-rom-elfs', '*'))
if _rom_elfs and not os.environ.get('ESP_ROM_ELF_DIR'):
    os.environ['ESP_ROM_ELF_DIR'] = _rom_elfs
os.environ['PATH'] = os.pathsep.join(p for p in _extra_paths if p) + os.pathsep + os.environ.get('PATH', '')
print(f'[esp-idf-mcp] IDF_PATH={IDF_PATH}  TOOLS={_tools}  PYENV={_python_env}', file=sys.stderr)

from mcp.server.fastmcp import FastMCP

mcp = FastMCP('ESP-IDF')


def _get_idf_py():
    """Get the path to idf.py"""
    return os.path.join(os.environ['IDF_PATH'], 'tools', 'idf.py')


### Persistent logs: one file per call, no cross-talk, kept for later inspection; only the newest _LOG_KEEP are retained ###
_LOG_DIR = os.path.join(tempfile.gettempdir(), 'esp-idf-mcp-logs')
_LOG_KEEP = 20


def _kill_tree(proc):
    """Kill the child process and its whole tree: idf.py spawns cmake/ninja, and killing only the direct child leaves orphans
    (still burning CPU and locking the build dir, which fails the next build)."""
    if proc is None:
        return
    try:
        if os.name == 'nt':
            subprocess.run(['taskkill', '/F', '/T', '/PID', str(proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            proc.kill()
    except Exception:
        pass


def _run_sync(cmd, cwd, timeout=600):
    """Run command synchronously; output goes to a unique persistent log file.
    Returns (returncode, output, log_file). On timeout the whole process tree is killed."""
    os.makedirs(_LOG_DIR, exist_ok=True)
    fd, log_file = tempfile.mkstemp(prefix=time.strftime('%Y%m%d_%H%M%S_'), suffix='.log', dir=_LOG_DIR)
    os.close(fd)
    proc = None
    try:
        with open(log_file, 'w', encoding='utf-8') as f:
            print(f'Running: {" ".join(cmd)} in {cwd}', file=sys.stderr)
            proc = subprocess.Popen(
                cmd, stdout=f, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                env=os.environ.copy(), cwd=cwd
            )
            try:
                rc = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                _kill_tree(proc)
                rc = -1
        with open(log_file, 'r', encoding='utf-8', errors='replace') as f:
            output = f.read()
        for old in sorted(_glob.glob(os.path.join(_LOG_DIR, '*.log')))[:-_LOG_KEEP]:
            try:
                os.remove(old)
            except OSError:
                pass
        if rc == -1:
            return -1, f'Timed out after {timeout}s (process tree killed)', log_file
        return rc, output, log_file
    except Exception as e:
        _kill_tree(proc)
        return -1, f'{e}', log_file


@mcp.tool(structured_output=False)
def build_project(project_dir: str, full_log: bool = False) -> str:
    """Build ESP-IDF project via idf.py (same as manual `idf.py build`).
    Args:
        project_dir: Absolute path to the ESP-IDF project directory
        full_log: If True, return the complete build output (no truncation). If False (default), return only the tail of the output.
    """
    rc, out, log_file = _run_sync([IDF_PYTHON, _get_idf_py(), '-C', project_dir, 'build'], project_dir)
    if rc == 0:
        return f'Successfully built project.\n{out if full_log else out[-300:]}'
    else:
        return f'Build failed (exit {rc}): {out if full_log else out[-500:]}{os.linesep}full log: {log_file}'


@mcp.tool(structured_output=False)
def flash_project(project_dir: str, port: Optional[str] = None, monitor: bool = True, wait_after_flash: float = 2.0) -> str:
    """Flash the built project with esptool, then open a monitor session on the port (default).
    Args:
        project_dir: Absolute path to the ESP-IDF project directory
        port: Serial port (e.g. COM13); auto-detected when omitted (only if exactly one non-COM1 port). Monitor sessions on this port are closed before flashing.
        monitor: Open a monitor session after flashing (default True)
        wait_after_flash: Seconds to let the hard-reset boot finish before opening the monitor (default 2.0)
    """
    # Pre-flash: auto-detect the port when omitted; once known, close only this port's monitor session (skip if none open)
    if not port:
        port = _autodetect_port()
        if not port:
            return _no_port_message()
    closed = _close_monitors_on_port(port)
    closed_note = f' (auto-closed monitor: {", ".join(closed)})' if closed else ''
    build_dir = os.path.join(project_dir, 'build')
    flash_args_file = os.path.join(build_dir, 'flash_args')
    cmd = [IDF_PYTHON, '-m', 'esptool', '--port', port, '--before', 'default-reset', '--after', 'hard-reset']
    cmd.append('write-flash')
    if os.path.exists(flash_args_file):
        with open(flash_args_file, 'r') as f:
            args = f.read().strip().split()
        cmd.extend(args)
    rc, out, log_file = _run_sync(cmd, build_dir)
    if rc != 0:
        return f'Flash failed (exit {rc}): {out[-500:]}{os.linesep}full log: {log_file}'
    result = f'Successfully flashed to {port}.{closed_note} {out[-200:]}'
    # After flashing, open a persistent monitor session (reset=True: the board is reset, so the session only holds the fresh boot log),
    # and return immediately; release it with monitor_close when done.
    if monitor:
        if wait_after_flash > 0:
            time.sleep(wait_after_flash)
        result += '\n' + monitor_open(port, reset=True, project_dir=project_dir)
    return result

# Chip-info snippet executed in a venv-python subprocess (esptool logs go to stdout,
# which must stay out of this process or it would pollute the MCP stdio JSON-RPC channel)
_CHIP_INFO_SNIPPET = r'''
import json, sys

port, baud = sys.argv[1], int(sys.argv[2])
info = {}
try:
    from esptool.cmds import detect_chip, detect_flash_size
    esp = detect_chip(port=port, baud=baud)
    info['chip'] = esp.CHIP_NAME
    try:
        rev = esp.get_chip_revision()
        info['chip_revision'] = f'v{rev // 100}.{rev % 100} (raw {rev})'
    except Exception as e:
        info['chip_revision_error'] = str(e)
    try:
        info['features'] = esp.get_chip_features()
    except Exception as e:
        info['features_error'] = str(e)
    try:
        info['crystal_freq_mhz'] = esp.get_crystal_freq()
    except Exception as e:
        info['crystal_freq_error'] = str(e)
    try:
        info['mac'] = ':'.join(f'{b:02x}' for b in esp.read_mac())
    except Exception as e:
        info['mac_error'] = str(e)
    try:
        fid = esp.flash_id()
        info['flash_vendor_id'] = f'0x{fid & 0xFF:02x}'
        info['flash_device_id'] = f'0x{(fid >> 8) & 0xFFFF:04x}'
        # esptool 5.x detect_flash_size returns strings like '4MB' (or None)
        info['flash_size'] = detect_flash_size(esp)
    except Exception as e:
        info['flash_error'] = str(e)
    try:
        esp.hard_reset()  # hard-reset after reading so the app resumes
    except Exception:
        pass
except Exception as e:
    info['error'] = f'{type(e).__name__}: {e}'
finally:
    print(json.dumps(info))
    sys.exit(0 if 'error' not in info else 1)
'''


@mcp.tool(structured_output=False)
def read_chip_info(port: str = '', baud: int = 115200) -> str:
    """Read hardware info from a connected ESP32 board via esptool: chip model, revision, features, crystal freq, MAC address, flash vendor/device/size. The board is briefly put into download mode and hard-reset back to the running app afterwards. Note: PSRAM info is NOT available here (check the boot log via monitor_open).
    Args:
        port: Serial port (e.g. COM13). Empty = auto-detect (first port, excluding COM1).
        baud: Baud rate for the esptool connection (default 115200).
    """
    if not port:
        port = _autodetect_port()
        if not port:
            return _no_port_message()
    closed = _close_monitors_on_port(port)
    closed_note = f' (auto-closed monitor: {", ".join(closed)})' if closed else ''
    rc, out, log_file = _run_sync([IDF_PYTHON, '-c', _CHIP_INFO_SNIPPET, port, str(baud)], tempfile.gettempdir(), timeout=120)
    # esptool connection logs are mixed into the output; take the last JSON line
    json_line = next((l for l in reversed(out.strip().splitlines()) if l.startswith('{')), None)
    if json_line is None:
        return f'Failed to read chip info on {port} (exit {rc}):{closed_note}{os.linesep}{out[-500:]}{os.linesep}full log: {log_file}'
    info = json.loads(json_line)
    if 'error' in info:
        return f'Failed to read chip info on {port}:{closed_note}{os.linesep}{info["error"]}'
    lines = [f'Chip info for {port}{closed_note}:',
             f'  chip:         {info.get("chip", "?")}']
    for key, label in [('chip_revision', 'revision:     '), ('features', 'features:     '),
                       ('crystal_freq_mhz', 'crystal:      '), ('mac', 'MAC:          '),
                       ('flash_vendor_id', 'flash vendor: '), ('flash_device_id', 'flash device: '),
                       ('flash_size', 'flash size:   ')]:
        if key in info:
            lines.append(f'  {label}{info[key]}')
    for key, label in [('chip_revision_error', 'revision'), ('features_error', 'features'),
                       ('crystal_freq_error', 'crystal'), ('mac_error', 'MAC'), ('flash_error', 'flash')]:
        if key in info:
            lines.append(f'  {label}: unavailable ({info[key]})')
    return os.linesep.join(lines)


@mcp.tool(structured_output=False)
def set_target(project_dir: str, target: str) -> str:
    """Set the ESP-IDF target using idf.py set-target.
    Warning: idf.py renames the existing sdkconfig to sdkconfig.old and generates a fresh one
    for the new target, so target-specific settings are not carried over.
    Args:
        project_dir: Absolute path to the ESP-IDF project directory
        target: Target chip (e.g. esp32, esp32s3, esp32c2)
    """
    cmd = [IDF_PYTHON, _get_idf_py(), 'set-target', target]
    rc, out, log_file = _run_sync(cmd, project_dir, timeout=120)
    if rc == 0:
        return (f'Target set to: {target}. Note: idf.py renamed the previous sdkconfig to '
                f'sdkconfig.old and generated a new one; build_project re-applies '
                f'sdkconfig.defaults, so keep project settings there.')
    else:
        return f'Failed to set target (exit {rc}): {out[-500:]}{os.linesep}full log: {log_file}'


@mcp.tool(structured_output=False)
def add_dependency(project_dir: str, dependency: str, component: str = 'main', path: str = '') -> str:
    """Add component dependency to the project's idf_component.yml. The component is downloaded on the next build.
    Args:
        project_dir: Absolute path to the ESP-IDF project directory
        dependency: Component dependency string (e.g. 'espressif/button^2.5.0', 'elrebo-de/deep_sleep^1.2.1')
        component: Name of the component in the project whose manifest gets the dependency (default 'main').
        path: Path to the component directory whose manifest gets the dependency. Takes precedence over `component` if set (official --path option).
    """
    cmd = [IDF_PYTHON, _get_idf_py(), 'add-dependency', dependency]
    if path:
        cmd.extend(['--path', path])
    elif component != 'main':
        cmd.extend(['--component', component])
    rc, out, log_file = _run_sync(cmd, project_dir, timeout=120)
    if rc == 0:
        return f'Dependency added to manifest: {dependency} (component downloaded on next build)'
    else:
        return f'Failed to add dependency (exit {rc}): {out[-500:]}{os.linesep}full log: {log_file}'


@mcp.tool(structured_output=False)
def remove_dependency(project_dir: str, dependency: str) -> str:
    """Remove a dependency (e.g. 'espressif/button') from all manifest files in the project. Component files are pruned on the next build.
    Args:
        project_dir: Absolute path to the ESP-IDF project directory
        dependency: Component dependency name (e.g. 'espressif/button', 'button')
    """
    cmd = [IDF_PYTHON, _get_idf_py(), 'remove-dependency', dependency]
    rc, out, log_file = _run_sync(cmd, project_dir, timeout=120)
    if rc == 0:
        return f'Dependency removed from manifests: {dependency} (pruned on next build)'
    else:
        return f'Failed to remove dependency (exit {rc}): {out[-500:]}{os.linesep}full log: {log_file}'


@mcp.tool(structured_output=False)
def clean_project(project_dir: str, full: bool = False) -> str:
    """Clean build artifacts using idf.py.
    Args:
        project_dir: Absolute path to the ESP-IDF project directory
        full: If True, remove entire build directory (fullclean). If False, incremental clean.
    """
    action = 'fullclean' if full else 'clean'
    cmd = [IDF_PYTHON, _get_idf_py(), action]
    rc, out, log_file = _run_sync(cmd, project_dir, timeout=120)
    if rc == 0:
        return f'Project {action} successfully'
    else:
        return f'Clean failed (exit {rc}): {out[-500:]}{os.linesep}full log: {log_file}'


_ANSI_RE = re.compile(r'\x1b\[[0-9;]*[A-Za-z]')  # ANSI escapes from colored ESP_LOG output - pure noise for the agent
# Leading level+ms prefix ('I (12345) '): ESP_LOG level chars are V/D/I/W/E; stripped before duplicate comparison
_LOG_PREFIX_RE = re.compile(r'^([IWEVD]) \((\d+)\) ')
# App-layer start: any main_task line mentioning app_main - 'Calling app_main()', 'Returned from app_main()'
# and any other wording. Hardcoding one exact phrasing is brittle: an IDF or log-level change would hide the marker and fold whole batches.
_APP_START_RE = re.compile(r'app_main\b')
# Boot-phase markers: if the increment contains these, the post-reset boot has not reached app_main yet and the whole batch is folded
_BOOT_RE = re.compile(r'ESP-ROM|rst:0x[0-9a-fA-F]')
# Phrases baked into every monitor_read(match=...) filter: a filtered view must never hide a crash.
# `E (...)` catches every ESP_LOG error-level line; the rest are panic-handler prints that carry no
# log-level prefix, plus reset reasons (canonical wording per docs: api-guides/fatal-errors.html).
# Not exposed as a parameter — the agent just sees these lines come through.
_CRITICAL_LOG_RE = re.compile(
    r'^E \(\d+\)'
    r'|Guru Meditation Error|panic\'ed'
    r'|abort\(\) was called|assertion .* failed|assert failed'
    r'|Stack canary watchpoint triggered|Stack smashing protect failure|CORRUPT HEAP'
    r'|Brownout detector was triggered|watchdog got triggered|Interrupt [Ww]atchdog'
    r'|Backtrace:|core ?dump|Rebooting\.\.\.|^rst:|ESP_ERROR_CHECK failed'
)


def _strip_ts(line: str) -> str:
    """Strip the leading 'I (12345) ' prefix; used for duplicate comparison only, display keeps the original text."""
    m = _LOG_PREFIX_RE.match(line)
    return line[m.end():] if m else line


def _fold_repeats(lines, show_ts: bool):
    """Fold adjacent duplicate lines into `line  *N` (N>1). Comparison strips the leading
    ms prefix, so identical messages logged at different milliseconds still collapse; show_ts=False displays lines without the `I (12345) ` prefix."""
    runs = []  # [text, count, first line verbatim]
    for line in lines:
        key = _strip_ts(line)
        if runs and runs[-1][0] == key:
            runs[-1][1] += 1
        else:
            runs.append([key, 1, line])
    return [f'{(first if show_ts else key)}  *{n}' if n > 1 else (first if show_ts else key)
            for key, n, first in runs]


class SerialSession:
    def __init__(self, port, baud, max_lines=20000, log_path=None):
        self.port = port
        self.baud = baud
        self.buffer = deque(maxlen=max_lines)
        self.total = 0        # total lines received
        self.read_cursor = 0  # lines already returned (incremental cursor; full mode does not advance it)
        self.running = True
        self.error = None
        self.ser = None
        self.thread = None
        self.last_activity = time.monotonic()  # bumped by monitor_read/monitor_send; the watchdog releases the port when it goes stale
        # Session full log: the ring buffer gets evicted and is cleared on reconnect/reset;
        # only the on-disk file never loses history. Write failures degrade silently (must not take down the reader thread).
        self.log_path = log_path
        self._log_fp = None
        if log_path:
            try:
                self._log_fp = open(log_path, 'a', encoding='utf-8', errors='replace')
                self._log_fp.write(f'==== monitor session {port}@{baud} opened '
                                   f'{time.strftime("%Y-%m-%d %H:%M:%S")} ====\n')
                self._log_fp.flush()
            except OSError as e:
                print(f'[WARN] monitor log file {log_path} unavailable: {e}', file=sys.stderr)
                self._log_fp = None

    def _log_write(self, text):
        if not self._log_fp:
            return
        try:
            self._log_fp.write(text + '\n')
            self._log_fp.flush()
        except Exception:
            try:
                self._log_fp.close()
            except Exception:
                pass
            self._log_fp = None

    def _open_serial(self):
        if self.ser and self.ser.is_open:
            try:
                self.ser.close()
            except Exception:
                pass
        self.ser = serial.Serial()
        self.ser.port = self.port
        self.ser.baudrate = self.baud
        self.ser.timeout = 0.25
        self.ser.write_timeout = 0.5
        self.ser.rtscts = False
        self.ser.dsrdtr = False
        self.ser.xonxoff = False
        self.ser.open()
        # Do not touch RTS/DTR right after opening; handle them once the reader thread is up
        # (toggling them during open would reset the board and lose the first log lines)

    def _set_rts(self, state):
        """Set RTS, working around a Windows usbser.sys quirk.
        
        The Windows usbser.sys driver does not send SET_CONTROL_LINE_STATE when only RTS changes;
        explicitly assigning DTR (even unchanged) forces the request out.
        """
        self.ser.rts = state
        self.ser.dtr = self.ser.dtr  # force the SET_CONTROL_LINE_STATE request out

    def _hard_reset(self):
        """A falling RTS edge triggers a reset (ESP32-S2 ROM CDC mechanism).
        
        ESP32-S2 ROM CDC driver: an RTS falling edge (True→False) resets the chip;
        DTR=False → REBOOT_NORMAL (normal reboot, app runs)
        DTR=True → REBOOT_BOOTLOADER (download mode)
        
        On physical UART bridges (CH340 etc.) RTS directly drives EN, so this resets them too.
        """
        try:
            self.ser.dtr = False  # make sure we get a normal reboot
            self._set_rts(True)   # RTS rising edge
            time.sleep(0.05)
            self._set_rts(False)  # RTS falling edge -> triggers the reset
        except Exception as e:
            print(f'[WARN] Hard reset failed: {e}', file=sys.stderr)

    def start(self, reset=True):
        self._open_serial()
        self.thread = threading.Thread(target=self._read_loop, daemon=True)
        self.thread.start()
        if reset:
            time.sleep(0.05)
            try:
                # Discard stale data buffered in the CDC/driver before the reset, so the previous boot's logs do not mix into this capture
                try:
                    self.ser.reset_input_buffer()
                except Exception:
                    pass
                self.buffer.clear()
                self.read_cursor = 0
                self.total = 0  # keep in sync with buffer/cursor, otherwise the first read after reset falsely reports evicted
                self._hard_reset()
                self._log_write('==== board reset ====')
            except serial.SerialException:
                pass  # the CDC port disappears during reset; swallow it (the reconnect logic clears the buffers)

    def _append_line(self, raw: bytes):
        """Decode raw bytes, strip ANSI escapes, store into the buffer - fault tolerant"""
        try:
            text = _ANSI_RE.sub('', raw.decode('utf-8', errors='replace')).rstrip('\r')
        except Exception:
            text = str(raw)
        self.buffer.append(text)
        self.total += 1
        self._log_write(text)

    def _read_loop(self):
        line_buffer = b''
        while self.running:
            try:
                if self.ser.is_open:
                    data = self.ser.read(self.ser.in_waiting or 1)
                else:
                    raise serial.PortNotOpenError
                if data:
                    line_buffer += data
                    while b'\n' in line_buffer:
                        line, line_buffer = line_buffer.split(b'\n', 1)
                        self._append_line(line)
                    if len(line_buffer) > 2048:
                        self._append_line(line_buffer)
                        line_buffer = b''
            except (serial.SerialException, OSError):
                if not self.running:
                    return
                try:
                    if self.ser and self.ser.is_open:
                        self.ser.close()
                except Exception:
                    pass
                # After a reset the port disappears and re-enumerates (USB-OTG); allow 0.5-2 s
                # Reconnect logic modeled on idf_monitor: 0.5 s interval, up to 10 s
                reconnect_waited = 0
                while self.running:
                    try:
                        time.sleep(0.5)  # officially recommended interval
                        reconnect_waited += 0.5
                        self._open_serial()
                        self.buffer.clear()  # On successful reconnect clear the old logs so only post-reset lines are kept
                        self.read_cursor = 0
                        self.total = 0     # keep in sync with buffer/cursor
                        line_buffer = b''  # drop the partial line from before the disconnect so it cannot glue onto reconnected data
                        self._log_write(f'==== port reconnected after {reconnect_waited}s ====')
                        print(f'[INFO] Port {self.port} reconnected after {reconnect_waited}s', file=sys.stderr)
                        break
                    except (serial.SerialException, OSError):
                        if reconnect_waited >= 10:  # give up after 10 s
                            self.error = f'Port re-enumeration failed after {reconnect_waited}s'
                            self.running = False
                            return
                        continue

    def stop(self):
        self.running = False
        if self.ser and self.ser.is_open:
            try:
                self.ser.close()
            except Exception:
                pass
        if self.thread:
            self.thread.join(timeout=2)
        if self._log_fp:
            try:
                self._log_fp.close()
            except Exception:
                pass
            self._log_fp = None


def _resolve_console_baud(project_dir: Optional[str] = None) -> int:
    """Resolve the serial baud rate: read CONFIG_ESP_CONSOLE_UART_BAUDRATE from the project's sdkconfig when
    a project dir is given; fall back to 115200 when standalone (or on read failure)."""
    if not project_dir:
        return 115200
    try:
        with open(os.path.join(project_dir, 'sdkconfig'), encoding='utf-8', errors='replace') as f:
            for line in f:
                if line.startswith('CONFIG_ESP_CONSOLE_UART_BAUDRATE='):
                    return int(line.split('=', 1)[1].strip().strip('"'))
    except (OSError, ValueError):
        print(f'[WARN] resolve baud from {project_dir} failed, fallback 115200', file=sys.stderr)
    return 115200


# Session-based serial monitor registry: session_id -> SerialSession (maintained by monitor_open/close)
_MONITORS: dict = {}
# Default session lifetime: after this long with no monitor_read/monitor_send the session frees its
# own port. Per-call override via monitor_open(idle_release=...). The MCP client abandons server
# processes without closing the stdio pipes (observed), and the reader thread re-grabs the port after
# unplug/replug — without self-release a monitor would hold the COM port until the process is killed
# by hand.
_SESSION_IDLE_RELEASE = 30


def _serial_ports():
    """Connected ports, COM1 (motherboard) excluded, in enumeration order (not sorted)."""
    ports = [p.device.strip() for p in list_ports.comports()] if list_ports else []
    return [p for p in ports if p.upper() != 'COM1']


def _autodetect_port() -> Optional[str]:
    """The only connected port, else None. Deliberately refuses to pick one out of several:
    enumeration order is not stable, so silently choosing could flash the wrong board."""
    ports = _serial_ports()
    return ports[0] if len(ports) == 1 else None


def _no_port_message() -> str:
    """Error text for callers that need exactly one port but found none or several."""
    ports = _serial_ports()
    if not ports:
        return 'No serial port found (COM1 excluded) — pass port explicitly.'
    return f'Multiple serial ports found: {", ".join(ports)} — pass port explicitly.'


def _close_monitors_on_port(port):
    """Close open monitor sessions on the given port (case-insensitive, no blanket close)."""
    closed = []
    for sid, sess in list(_MONITORS.items()):
        if sess.port.upper() == port.strip().upper():
            sess.stop()
            _MONITORS.pop(sid, None)
            closed.append(sid)
    return closed


def _close_all_monitors():
    """atexit fallback: on process exit release every session's port and log file, whatever the client left open."""
    for sid, sess in list(_MONITORS.items()):
        try:
            sess.stop()
        except Exception:
            pass
    _MONITORS.clear()


def _idle_watchdog(sess, sid, idle_release):
    """Session-owned lifetime (spawned by monitor_open): release the port once no
    monitor_read/monitor_send happened for idle_release seconds."""
    while sess.running and time.monotonic() - sess.last_activity < idle_release:
        time.sleep(1)
    if not sess.running:
        return  # closed externally (monitor_close / flash / atexit) while we slept
    print(f'[INFO] {sid} idle >{idle_release}s, releasing {sess.port}', file=sys.stderr)
    sess.stop()
    _MONITORS.pop(sid, None)


@mcp.tool(structured_output=False)
def monitor_open(port: str, reset: bool = True, project_dir: Optional[str] = None,
                 idle_release: float = _SESSION_IDLE_RELEASE) -> str:
    """Open a persistent serial monitor session. Returns immediately (non-blocking).
    Args:
        port: Serial port (e.g. COM14)
        reset: Hard-reset the board after opening, so the session captures a fresh boot (default True)
        project_dir: Project dir for baud auto-detect (sdkconfig); the session full log goes to its .esp_monitor_full.log
        idle_release: Seconds without monitor_read/monitor_send before the session auto-releases the port (default 30). Reopen if a call reports "No session".
    """
    baud = _resolve_console_baud(project_dir)
    sid = f'{port}@{baud}'
    if sid in _MONITORS:
        return f'Session {sid} is already open — use monitor_send / monitor_close.'
    open_on_port = [s for s, sess in _MONITORS.items() if sess.port.upper() == port.strip().upper()]
    if open_on_port:
        return (f'{port} is already monitored as {", ".join(open_on_port)} — monitor_close it '
                f'first if you want to reopen with a different baud.')
    # Session full log: into the project root when a project dir is given (.esp_monitor_full.log, easy to find), else into the temp dir
    if project_dir:
        log_path = os.path.join(project_dir, '.esp_monitor_full.log')
    else:
        os.makedirs(_LOG_DIR, exist_ok=True)
        log_path = os.path.join(_LOG_DIR, f'monitor_{port}_{time.strftime("%Y%m%d_%H%M%S")}.log')
    try:
        sess = SerialSession(port, baud, log_path=log_path)
        sess.start(reset=reset)
    except serial.SerialException as e:
        return f'Failed to open {port}: {e}'
    _MONITORS[sid] = sess
    threading.Thread(target=_idle_watchdog, args=(sess, sid, idle_release), daemon=True).start()
    return (f'Monitor opened: {sid} (reset={"on" if reset else "off"}); full log: {log_path}. '
            f'Idle >{idle_release:g}s auto-releases the port - reopen if the session is gone.')


@mcp.tool(structured_output=False)
def monitor_read(session_id: str, full: bool = False, timestamp: bool = False, max_lines: int = 0,
                 match: str = '') -> str:
    """Read new serial output of an open session since the last read.
    Default: incremental view - boot output before `app_main` is folded into a count, duplicate
    lines collapse to `line  *N`; the header reports counts and evictions.
    full=True: verbatim dump of everything retained in the buffer, without advancing the cursor
    (evicted lines and full history live in the log file from monitor_open).
    match: case-insensitive regex on the line text, '|' for several alternatives ('wifi|error');
    crash/error lines (E-level logs, panics, backtraces, reset reasons) always pass through.
    Args:
        session_id: Session id from monitor_open (e.g. 'COM14@74880')
        full: Dump everything retained verbatim, cursor-independent (default False = incremental)
        timestamp: Keep the leading `I (12345) ` ms prefix (default False: stripped)
        max_lines: Cap on returned lines (0 = no cap, the default)
        match: Regex filter ('|' separates alternatives, e.g. 'wifi|error')
    """
    sess = _MONITORS.get(session_id)
    if not sess:
        return f'No session {session_id}. Open one with monitor_open (open sessions: {list(_MONITORS) or "none"}).'
    sess.last_activity = time.monotonic()

    while True:
        total = sess.total  # read the counter before copying the buffer: lines appended during the copy are left for the next read - no loss, no duplication
        try:
            lines_all = list(sess.buffer)
            break
        except RuntimeError:
            continue  # the reader thread appended mid-iteration (deque raises); at serial rates the retry succeeds immediately
    base = total - len(lines_all)              # absolute line number of the buffer head (= lines evicted by the ring so far)

    # Filter = the caller's pattern (on the prefix-stripped line) OR the critical-log phrases (on the
    # raw line - their `E (...)` / `^rst:` anchors need the prefix). So a filtered view still carries
    # every crash/error line even when it matches none of the requested things.
    pat = None
    if match.strip():
        try:
            pat = re.compile(match, re.IGNORECASE)
        except re.error as e:
            return f'Invalid regex {match!r}: {e}'

    def _kept(line):
        return (pat is not None and pat.search(_strip_ts(line))) or _CRITICAL_LOG_RE.search(line) is not None

    if full:
        # Cursor-independent true full dump: emit every line kept in the buffer without advancing the incremental cursor (the two read modes never interfere)
        shown = [l for l in lines_all if _kept(l)] if pat else lines_all
        if max_lines > 0 and len(shown) > max_lines:
            shown = shown[-max_lines:]
        scope = f', {len(shown)} matched' if pat else ''
        header = (f'--- {session_id}: full dump, showing {len(shown)} of {len(lines_all)} retained '
                  f'lines{scope} ({total} total received, {base} evicted by buffer cap); '
                  f'log file: {sess.log_path or "<not enabled>"} ---')
        return os.linesep.join([header] + shown)

    start = max(sess.read_cursor - base, 0)
    new_lines = lines_all[start:]
    evicted = max(base - sess.read_cursor, 0)  # unread lines evicted by the ring buffer
    sess.read_cursor = total

    if pat:
        shown = [l for l in new_lines if _kept(l)]
        matched_n = len(shown)
        notes = f', {evicted} evicted by buffer cap' if evicted else ''
        if max_lines > 0 and len(shown) > max_lines:
            notes += f', {len(shown) - max_lines} older skipped (showing last {max_lines})'
            shown = shown[-max_lines:]
        return os.linesep.join([f'--- {session_id}: {len(new_lines)} new lines, {matched_n} matched{notes} ---'] + shown)

    # Find the app-layer start: fold everything before it, show it and what follows. When the batch has no marker, check whether it is still in the boot phase:
    # boot markers present (not yet past "Returned") -> fold the whole batch; otherwise it is a plain continuation -> show it all
    cut = next((i for i, line in enumerate(new_lines) if _APP_START_RE.search(line)), None)
    if cut is not None:
        folded = cut
    elif any(_BOOT_RE.search(line) for line in new_lines):
        folded = len(new_lines)
    else:
        folded = 0
    shown = _fold_repeats(new_lines[folded:], timestamp)

    if max_lines > 0 and len(shown) > max_lines:
        tail = shown[-max_lines:]
        skipped = len(shown) - len(tail)
    else:
        tail, skipped = shown, 0
    notes = f', {evicted} evicted by buffer cap' if evicted else ''
    notes += f', {skipped} older skipped (showing last {len(tail)})' if skipped else ''
    notes += f', {folded} lines folded before app start' if folded else ''
    if folded and folded == len(new_lines):
        notes += f' (no app-start marker yet; full serial log: {sess.log_path or "<not enabled>"})'
    notes += f', WARNING {sess.error}' if sess.error else ''
    return os.linesep.join([f'--- {session_id}: {len(new_lines)} new lines{notes} ---'] + tail)


@mcp.tool(structured_output=False)
def monitor_send(session_id: str, data: str, press_enter: bool = True) -> str:
    """Send text to the device's serial input (e.g. shell commands, menu selections).
    Args:
        session_id: Session id from monitor_open
        data: Text to write to the port
        press_enter: Append CRLF after the data (default True)
    """
    sess = _MONITORS.get(session_id)
    if not sess:
        return f'No session {session_id}. Open one with monitor_open (open sessions: {list(_MONITORS) or "none"}).'
    sess.last_activity = time.monotonic()  # writing counts as activity too, else send-then-read gaps would reap the session mid-work
    try:
        sess.ser.write(data.encode('utf-8') + (b'\r\n' if press_enter else b''))
        return f'Sent to {session_id}: {data!r}'
    except (serial.SerialException, OSError) as e:
        return f'Write failed ({e}). The port may be re-enumerating — retry once, or monitor_close + monitor_open to recover.'


@mcp.tool(structured_output=False)
def monitor_close(session_id: str) -> str:
    """Close a monitor session and release its serial port. Only the given session / port is released — there is no release-all mode.
    Args:
        session_id: Session id from monitor_open (e.g. 'COM14@74880'), or a bare port ('COM14') to close all sessions on that port.
    """
    target = session_id.strip()
    sid = next((s for s in _MONITORS if s.upper() == target.upper()), None)
    if sid:
        sess = _MONITORS.pop(sid)
        sess.stop()
        return f'Monitor closed: {sid}. Port {sess.port} released.'
    closed = _close_monitors_on_port(target)  # match by port name ('COM14' -> 'COM14@74880')
    if closed:
        return f'Monitor sessions closed on {target}: {", ".join(closed)}.'
    return f'No session {target} (open sessions: {list(_MONITORS) or "none"}).'


@mcp.tool(structured_output=False)
def run_pytest(project_dir: str, test_path: str = 'pytest', target: str = '', port: str = '', timeout: int = 300, extra_args: str = '') -> str:
    """Run pytest-embedded hardware tests (flash + interact with the real board, assert on serial output).
    Requires test scripts in the project (ESP-IDF convention: a 'pytest' folder with pytest_*.py).
    Args:
        project_dir: Absolute path to the ESP-IDF project directory
        test_path: Path (relative to project_dir) to the tests dir or a single test file (default 'pytest')
        target: Chip target passed as --target (e.g. esp32c2). Empty = use pytest-embedded defaults.
        port: Serial port for the device (e.g. COM14). Empty = auto-detect (works when exactly one device is connected).
        timeout: Max seconds for the whole test run (default 300)
        extra_args: Extra pytest CLI args, space-separated (e.g. '-k test_wifi --count=1 -vv')
    """
    cmd = [IDF_PYTHON, '-m', 'pytest', test_path, '--embedded-services=esp,idf']
    if target:
        cmd.append(f'--target={target}')
    if port:
        cmd.extend(['--port', port])
    if extra_args:
        cmd.extend(extra_args.split())
    rc, out, log_file = _run_sync(cmd, project_dir, timeout=timeout)
    tail = out if len(out) < 4000 else out[-4000:]
    if rc == 0:
        return f'All tests passed ({len(out)} bytes output):{os.linesep}{tail}'
    return f'Tests failed / errored (exit {rc}):{os.linesep}{tail}{os.linesep}full log: {log_file}'


# === RESOURCES ===

@mcp.resource('project://devices')
def get_connected_devices() -> str:
    """Get list of connected devices"""
    try:
        devices = [p.device.strip() for p in list_ports.comports()]
        print(f'Devices: {devices}', file=sys.stderr)
        return json.dumps({'available_ports': devices if devices else []}, indent=2)
    except Exception as e:
        return f'Error getting devices: {e}'

def main():
    """Run the MCP server on stdio. Its lifetime follows the client: the process exits when
    the client closes the pipe, and a crash exits too — whether to restart it is the MCP
    client's decision, not ours."""
    atexit.register(_close_all_monitors)
    try:
        mcp.run()
    except KeyboardInterrupt:
        print('\nMCP Server stopped by user.')
    except Exception as e:
        print(f'MCP Server stopped: {e}', file=sys.stderr)
        raise SystemExit(1)


if __name__ == '__main__':
    main()