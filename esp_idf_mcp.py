import json
import os
import re
import tempfile
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Optional

import serial

try:
    from serial.tools import list_ports
except ImportError:
    list_ports = None

# Set up environment. Everything can be overridden from outside, e.g.:
#   set IDF_PATH=D:\esp\esp-idf
#   set IDF_PYTHON_ENV_PATH=C:\Espressif\tools\python_env\idf5.5_py3.11_env
# Defaults below match a standard Windows ESP-IDF installation — adjust via
# env vars if your layout differs (Linux/macOS users will want to set these).
os.environ.setdefault('PYTHON_DEPS_CHECKED', '1')
os.environ.setdefault('IDF_PATH', r'C:\esp\v6.0.2\esp-idf')
os.environ.setdefault('IDF_PYTHON_ENV_PATH', r'C:\Espressif\tools\python\v6.0.2\venv')
os.environ.setdefault('ESP_IDF_VERSION', '6.0.2')
# 启用 Component Manager：允许 idf.py add-dependency / build 解析并拉取组件
# (如 esp-nn, esp-tflite-micro)。默认关闭会导致组件依赖无法解析。
os.environ.setdefault('IDF_COMPONENT_MANAGER', '1')

# Add toolchain paths to PATH (missing entries are skipped so the script
# still works when only IDF_PATH / the Python env are present)
_tools = r'C:\Espressif\tools'
_extra_paths = [
    os.path.join(_tools, 'xtensa-esp-elf', 'esp-15.2.0_20251204', 'xtensa-esp-elf', 'bin'),
    os.path.join(_tools, 'riscv32-esp-elf', 'esp-15.2.0_20251204', 'riscv32-esp-elf', 'bin'),
    os.path.join(_tools, 'esp32ulp-elf', '2.38_20240113', 'esp32ulp-elf', 'bin'),
    os.path.join(_tools, 'cmake', '4.0.3', 'bin'),
    os.path.join(_tools, 'ninja', '1.12.1'),
    os.path.join(_tools, 'idf-exe', '1.0.3'),
    os.path.join(os.environ['IDF_PATH'], 'tools'),
    os.path.join(os.environ['IDF_PYTHON_ENV_PATH'], 'Scripts'),
]
os.environ['PATH'] = os.pathsep.join(p for p in _extra_paths if os.path.isdir(p)) + os.pathsep + os.environ.get('PATH', '')

from mcp.server.fastmcp import FastMCP

mcp = FastMCP('ESP-IDF')


def _get_idf_py():
    """Get the path to idf.py"""
    return os.path.join(os.environ['IDF_PATH'], 'tools', 'idf.py')


def _run_sync(cmd, cwd, timeout=600):
    """Run command synchronously with output to file. Returns (returncode, output)."""
    log_file = os.path.join(tempfile.gettempdir(), 'idf_mcp_output.log')
    try:
        with open(log_file, 'w', encoding='utf-8') as f:
            print(f'Running: {" ".join(cmd)} in {cwd}', file=sys.stderr)
            result = subprocess.run(
                cmd, stdout=f, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                env=os.environ.copy(), cwd=cwd, timeout=timeout
            )
        with open(log_file, 'r', encoding='utf-8', errors='replace') as f:
            output = f.read()
        return result.returncode, output
    except subprocess.TimeoutExpired:
        return -1, f'Timed out after {timeout}s'
    except Exception as e:
        return -1, str(e)


@mcp.tool(structured_output=False)
def build_project(project_dir: str, full_log: bool = False) -> str:
    """Build ESP-IDF project using ninja directly.
    Args:
        project_dir: Absolute path to the ESP-IDF project directory
        full_log: If True, return the complete build output (no truncation). If False (default), return only the tail of the output.
    """
    build_dir = os.path.join(project_dir, 'build')
    os.makedirs(build_dir, exist_ok=True)
    # Configure with cmake if needed
    cmake_cache = os.path.join(build_dir, 'CMakeCache.txt')
    if not os.path.exists(cmake_cache):
        rc, out = _run_sync(['cmake', '-G', 'Ninja', '-DPYTHON_DEPS_CHECKED=1', '-DESP_PLATFORM=1', '-B', build_dir, '-S', project_dir], project_dir)
        if rc != 0:
            return f'CMake configure failed: {out if full_log else out[-500:]}'
    # Build with ninja
    rc, out = _run_sync(['ninja', '-C', build_dir], project_dir)
    if rc == 0:
        return f'Successfully built project.\n{out if full_log else out[-300:]}'
    else:
        return f'Build failed (exit {rc}): {out if full_log else out[-500:]}'


@mcp.tool(structured_output=False)
def flash_project(project_dir: str, port: Optional[str] = None, monitor_baud: int = 0, monitor_timeout: int = 5, wait_after_flash: float = 2.0, collect_window: int = 10, wait_for: str = '', max_wait: int = 30) -> str:
    """Flash the built project using esptool directly. Optionally auto-starts serial monitor after flash.
    Args:
        project_dir: Absolute path to the ESP-IDF project directory
        port: Serial port (e.g. COM13). Auto-detected if not specified.
        monitor_baud: If > 0, auto-start serial monitor after flash at this baud rate (default 0 = no monitor).
        monitor_timeout: Seconds to wait for first serial data before returning (default 5).
        wait_after_flash: Seconds to wait after flash before opening serial (default 2.0). Lets esptool's hard-reset boot finish, avoiding stale logs in USB buffer.
        collect_window: Seconds to keep collecting after first data arrives when wait_for is empty (default 10, covers most app startup sequences).
        wait_for: If set (regex), keep collecting until a line matches this pattern, then return early (~1s settle). E.g. 'error|panic|Guru Meditation', 'ip_ready'.
        max_wait: Max total seconds to keep collecting while waiting for wait_for to match (default 30).
    """
    # esptool 独占串口：先关掉占用目标口（或全部，自动找口时）的监视会话，否则烧录必失败
    closed = _close_monitors_on_port(port)
    closed_note = f' (auto-closed monitor: {", ".join(closed)})' if closed else ''
    build_dir = os.path.join(project_dir, 'build')
    flash_args_file = os.path.join(build_dir, 'flash_args')
    cmd = [sys.executable, '-m', 'esptool']
    if port:
        cmd.extend(['--port', port])
    cmd.extend(['--before', 'default-reset', '--after', 'hard-reset'])
    cmd.append('write-flash')
    if os.path.exists(flash_args_file):
        with open(flash_args_file, 'r') as f:
            args = f.read().strip().split()
        cmd.extend(args)
    rc, out = _run_sync(cmd, build_dir)
    if rc != 0:
        return f'Flash failed (exit {rc}): {out[-500:]}'
    result = f'Successfully flashed{" to " + port if port else ""}.{closed_note} {out[-200:]}'
    # 烧录后自动开串口：等待 esptool 硬复位后的启动跑完，再打开串口+主动复位，避免 USB 缓冲区残留旧日志
    if monitor_baud > 0 and port:
        if wait_after_flash > 0:
            time.sleep(wait_after_flash)
        result += '\n' + serial_start(port, baud=monitor_baud, reset=True, duration=monitor_timeout, collect_window=collect_window, wait_for=wait_for, max_wait=max_wait)
    return result

@mcp.tool(structured_output=False)
def set_target(project_dir: str, target: str) -> str:
    """Set the ESP-IDF target using idf.py set-target.
    Args:
        project_dir: Absolute path to the ESP-IDF project directory
        target: Target chip (e.g. esp32, esp32s3, esp32c2)
    """
    cmd = [sys.executable, _get_idf_py(), 'set-target', target]
    rc, out = _run_sync(cmd, project_dir, timeout=120)
    if rc == 0:
        return f'Target set to: {target}'
    else:
        return f'Failed to set target (exit {rc}): {out[-500:]}'


@mcp.tool(structured_output=False)
def add_dependency(project_dir: str, dependency: str, component: str = 'main', path: str = '') -> str:
    """Add component dependency to the project's idf_component.yml. The component is downloaded on the next build.
    Args:
        project_dir: Absolute path to the ESP-IDF project directory
        dependency: Component dependency string (e.g. 'espressif/button^2.5.0', 'elrebo-de/deep_sleep^1.2.1')
        component: Name of the component in the project whose manifest gets the dependency (default 'main').
        path: Path to the component directory whose manifest gets the dependency. Takes precedence over `component` if set (official --path option).
    """
    cmd = [sys.executable, _get_idf_py(), 'add-dependency', dependency]
    if path:
        cmd.extend(['--path', path])
    elif component != 'main':
        cmd.extend(['--component', component])
    rc, out = _run_sync(cmd, project_dir, timeout=120)
    if rc == 0:
        return f'Dependency added to manifest: {dependency} (component downloaded on next build)'
    else:
        return f'Failed to add dependency (exit {rc}): {out[-500:]}'


@mcp.tool(structured_output=False)
def remove_dependency(project_dir: str, dependency: str) -> str:
    """Remove a dependency (e.g. 'espressif/button') from all manifest files in the project. Component files are pruned on the next build.
    Args:
        project_dir: Absolute path to the ESP-IDF project directory
        dependency: Component dependency name (e.g. 'espressif/button', 'button')
    """
    cmd = [sys.executable, _get_idf_py(), 'remove-dependency', dependency]
    rc, out = _run_sync(cmd, project_dir, timeout=120)
    if rc == 0:
        return f'Dependency removed from manifests: {dependency} (pruned on next build)'
    else:
        return f'Failed to remove dependency (exit {rc}): {out[-500:]}'


@mcp.tool(structured_output=False)
def clean_project(project_dir: str, full: bool = False) -> str:
    """Clean build artifacts using idf.py.
    Args:
        project_dir: Absolute path to the ESP-IDF project directory
        full: If True, remove entire build directory (fullclean). If False, incremental clean.
    """
    action = 'fullclean' if full else 'clean'
    cmd = [sys.executable, _get_idf_py(), action]
    rc, out = _run_sync(cmd, project_dir, timeout=120)
    if rc == 0:
        return f'Project {action} successfully'
    else:
        return f'Clean failed (exit {rc}): {out[-500:]}'




_ANSI_RE = re.compile(r'\x1b\[[0-9;]*[A-Za-z]')  # ESP_LOG 彩色输出的 ANSI 转义，对 agent 是纯噪声


def _collapse_dupes(lines):
    """合并相邻重复行（如 SD 卡重试、状态轮询刷屏），xN 标注次数。"""
    out = []
    for line in lines:
        if out and out[-1][0] == line:
            out[-1][1] += 1
        else:
            out.append([line, 1])
    return [l if c == 1 else f'{l}  (x{c})' for l, c in out]


class SerialSession:
    def __init__(self, port, baud, max_lines=5000):
        self.port = port
        self.baud = baud
        self.buffer = deque(maxlen=max_lines)
        self.read_cursor = 0  # 已返回给 agent 的行数；monitor_read 据此只返回新行
        self.running = True
        self.error = None
        self.ser = None
        self.thread = None

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
        # 打开后先不改变 RTS/DTR，等读线程启动后再统一处理
        # （避免打开时就触发复位，导致丢失首行日志）

    def _set_rts(self, state):
        """设置 RTS，包含 Windows usbser.sys workaround。
        
        Windows usbser.sys 驱动在仅改变 RTS 时不会发送 SET_CONTROL_LINE_STATE 请求，
        必须显式设置 DTR（即使值不变）才能强制发送控制请求。
        """
        self.ser.rts = state
        self.ser.dtr = self.ser.dtr  # 强制发送 SET_CONTROL_LINE_STATE

    def _hard_reset(self):
        """RTS 下降沿触发复位（ESP32-S2 ROM CDC 机制）。
        
        ESP32-S2 ROM CDC 驱动：RTS 下降沿（True→False）触发复位，
        DTR=False → REBOOT_NORMAL（普通重启，运行应用）
        DTR=True → REBOOT_BOOTLOADER（进入下载模式）
        
        对 CH340 等物理串口：RTS 直接控制 EN，也能正常复位
        """
        try:
            self.ser.dtr = False  # 确保普通重启
            self._set_rts(True)   # RTS 上升沿
            time.sleep(0.05)
            self._set_rts(False)  # RTS 下降沿 → 触发复位
        except Exception as e:
            print(f'[WARN] Hard reset failed: {e}', file=sys.stderr)

    def start(self, reset=True):
        self._open_serial()
        self.thread = threading.Thread(target=self._read_loop, daemon=True)
        self.thread.start()
        if reset:
            time.sleep(0.05)
            try:
                # 复位前丢弃 CDC/驱动缓冲区里的陈旧数据，避免上一轮启动日志混进本次抓取
                try:
                    self.ser.reset_input_buffer()
                except Exception:
                    pass
                self.buffer.clear()
                self.read_cursor = 0
                self._hard_reset()
            except serial.SerialException:
                pass  # CDC 端口复位时断开，静默捕获（重连逻辑会清缓冲）

    def _append_line(self, raw: bytes):
        """将原始字节解码、去 ANSI 转义后存入缓冲区，容错处理"""
        try:
            text = _ANSI_RE.sub('', raw.decode('utf-8', errors='replace')).rstrip('\r')
        except Exception:
            text = str(raw)
        self.buffer.append(text)

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
                # 复位后端口会消失并重新枚举（USB-OTG），需等待 0.5-2 秒
                # 参考 idf_monitor 的重连逻辑：0.5s 间隔，最多 10 秒
                reconnect_waited = 0
                while self.running:
                    try:
                        time.sleep(0.5)  # 官方推荐间隔
                        reconnect_waited += 0.5
                        self._open_serial()
                        self.buffer.clear()  # 重连成功后清空旧日志，确保只保留复位后的新日志
                        self.read_cursor = 0  # 缓冲已清空，游标同步归零
                        print(f'[INFO] Port {self.port} reconnected after {reconnect_waited}s', file=sys.stderr)
                        break
                    except (serial.SerialException, OSError):
                        if reconnect_waited >= 10:  # 最多等 10 秒
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


# 会话式串口监视器注册表：session_id -> SerialSession（monitor_open/close 维护）
_MONITORS: dict = {}


def _close_monitors_on_port(port):
    """Close open monitor sessions. Closes all when port is None (esptool auto-detect needs free ports)."""
    closed = []
    for sid, sess in list(_MONITORS.items()):
        if port is None or sess.port == port:
            sess.stop()
            _MONITORS.pop(sid, None)
            closed.append(sid)
    return closed


@mcp.tool(structured_output=False)
def monitor_open(port: str, baud: int = 74880, reset: bool = True) -> str:
    """Open a persistent serial monitor session. Returns immediately (non-blocking); poll output with monitor_read.
    Args:
        port: Serial port (e.g. COM14)
        baud: Baud rate (default 74880, works for both C2 ROM and S2 CDC)
        reset: Reset the board after opening (default True) so you capture a fresh boot
    """
    sid = f'{port}@{baud}'
    if sid in _MONITORS:
        return f'Session {sid} is already open — use monitor_read / monitor_send / monitor_close.'
    try:
        sess = SerialSession(port, baud)
        sess.start(reset=reset)
    except serial.SerialException as e:
        return f'Failed to open {port}: {e}'
    _MONITORS[sid] = sess
    return (f'Monitor opened: {sid} (reset={"on" if reset else "off"}). '
            f'Poll with monitor_read, send input with monitor_send, release with monitor_close. '
            f'Note: close this session before flashing {port}.')


@mcp.tool(structured_output=False)
def monitor_read(session_id: str, wait_for: str = '', timeout: float = 3.0, max_lines: int = 200, compact: bool = True) -> str:
    """Read new serial output of an open session since the last read. Instant return when no wait_for; use this instead of blocking serial_start when you want to stay responsive.
    Args:
        session_id: Session id from monitor_open (e.g. 'COM14@74880')
        wait_for: Optional regex; keep polling up to `timeout` seconds until a new line matches, then return early (~instant on hit)
        timeout: Max seconds to poll while waiting for wait_for (default 3). Ignored when wait_for is empty.
        max_lines: Cap on returned lines, keeping the most recent tail (default 200)
        compact: Collapse consecutive duplicate lines (xN) to save tokens (default True)
    """
    sess = _MONITORS.get(session_id)
    if not sess:
        return f'No session {session_id}. Open one with monitor_open (open sessions: {list(_MONITORS) or "none"}).'
    try:
        pattern = re.compile(wait_for) if wait_for else None
    except re.error as e:
        return f'Invalid wait_for regex: {e}'
    lines_all = []
    matched = pattern is None
    deadline = time.time() + (timeout if pattern is not None else 0.0)
    while True:
        lines_all = list(sess.buffer)
        start = min(sess.read_cursor, len(lines_all))
        new_lines = lines_all[start:]
        if pattern is not None and any(pattern.search(l) for l in new_lines):
            matched = True
        if matched or time.time() >= deadline:
            break
        time.sleep(0.2)
    sess.read_cursor = len(lines_all)
    status = f' [wait_for: {"MATCHED" if matched else "not matched within timeout"}]' if pattern is not None else ''
    if not new_lines:
        return f'--- {session_id}: 0 new lines{status} ---'
    tail = new_lines[-max_lines:]
    skipped = len(new_lines) - len(tail)
    if compact:
        tail = _collapse_dupes(tail)
    header = f'--- {session_id}: {len(new_lines)} new lines' + (f' ({skipped} older skipped, showing last {len(tail)})' if skipped else '')
    return header + status + f' ---{os.linesep}' + os.linesep.join(tail)


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
    try:
        sess.ser.write(data.encode('utf-8') + (b'\r\n' if press_enter else b''))
        return f'Sent to {session_id}: {data!r}'
    except (serial.SerialException, OSError) as e:
        return f'Write failed ({e}). The port may be re-enumerating — retry once, or monitor_close + monitor_open to recover.'


@mcp.tool(structured_output=False)
def monitor_close(session_id: str) -> str:
    """Close a monitor session and release the serial port (needed before flashing that port).
    Args:
        session_id: Session id from monitor_open
    """
    sess = _MONITORS.pop(session_id, None)
    if not sess:
        return f'No session {session_id} (open sessions: {list(_MONITORS) or "none"}).'
    sess.stop()
    return f'Monitor closed: {session_id}. Port {sess.port} released.'


@mcp.tool(structured_output=False)
def serial_start(port: str, baud: int = 74880, reset: bool = True, duration: int = 5, collect_window: int = 10, wait_for: str = '', max_wait: int = 30, compact: bool = True) -> str:
    """One-shot serial log capture: open port, reset board, wait for first data, collect logs, auto-close, return them.
    Args:
        port: Serial port (e.g. COM14)
        baud: Baud rate (default 74880, works for both C2 ROM and S2 CDC)
        reset: Reset board after opening port (default True)
        duration: Max seconds to wait for first data before giving up (default 5)
        collect_window: Seconds to keep collecting after first data arrives when wait_for is empty (default 10, covers most app startup sequences).
        wait_for: If set (regex), keep collecting until a line matches this pattern, then return early (~1s settle). E.g. 'error|panic|Guru Meditation', 'ip_ready', 'example: Example ended'.
        max_wait: Max total seconds to keep collecting while waiting for wait_for to match (default 30).
        compact: Collapse consecutive duplicate lines (xN) to save tokens (default True)
    """
    try:
        pattern = re.compile(wait_for) if wait_for else None
    except re.error as e:
        return f'Invalid wait_for regex: {e}'
    session = SerialSession(port, baud)
    try:
        session.start(reset=reset)
        deadline = time.time() + duration
        while time.time() < deadline and len(session.buffer) == 0:
            time.sleep(0.1)
        if len(session.buffer) == 0:
            lines = []
        elif pattern is None:
            time.sleep(collect_window)  # 首数据后再收集 collect_window 秒
            lines = list(session.buffer)
        else:
            # 等 wait_for 命中：命中后固定收 1 秒尾巴（错误堆栈常跟在触发行后面），超时则截断
            hard_deadline = time.time() + max_wait
            while time.time() < hard_deadline:
                if any(pattern.search(line) for line in session.buffer):
                    time.sleep(1.0)
                    break
                time.sleep(0.2)
            lines = list(session.buffer)
        header = f'--- {port} @ {baud} baud ({len(lines)} lines captured) ---'
        if pattern is not None:
            matched = any(pattern.search(line) for line in lines)
            header += f' [wait_for: {"MATCHED" if matched else "not matched within max_wait"}]'
        if lines:
            if compact:
                lines = _collapse_dupes(lines)
            return header + os.linesep + os.linesep.join(lines)
        reason = f'(no data within {duration}s)' if pattern is None else f'(no data; wait_for never matched)'
        return header + os.linesep + reason
    except serial.SerialException as e:
        return f'Failed to open {port}: {e}'
    except Exception as e:
        return f'Error: {e}'
    finally:
        session.stop()


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
    cmd = [sys.executable, '-m', 'pytest', test_path, '--embedded-services=esp,idf']
    if target:
        cmd.append(f'--target={target}')
    if port:
        cmd.extend(['--port', port])
    if extra_args:
        cmd.extend(extra_args.split())
    rc, out = _run_sync(cmd, project_dir, timeout=timeout)
    tail = out if len(out) < 4000 else out[-4000:]
    if rc == 0:
        return f'All tests passed ({len(out)} bytes output):{os.linesep}{tail}'
    return f'Tests failed / errored (exit {rc}):{os.linesep}{tail}'


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
    """Start the MCP server with auto-restart on crash."""
    while True:
        try:
            mcp.run()
            break
        except KeyboardInterrupt:
            print('\nMCP Server stopped by user.')
            break
        except Exception as e:
            print(f'MCP Server crashed: {e}, restarting in 3 seconds...', file=sys.stderr)
            time.sleep(3)


if __name__ == '__main__':
    main()