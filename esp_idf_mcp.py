"""ESP-IDF MCP server: build, flash and serial monitor tools for ESP-IDF projects."""
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

# === ESP-IDF 自动定位：环境变量优先，否则扫描常见安装位置取最新版 ===
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
            return sorted(found)[-1]  # 取版本号最大的
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
# 目录名形如 'v6.1'，但 idf_component_manager 用 Version.coerce 解析该变量，
# 前导 'v' 会抛 "Version string lacks a numerical component"，导致
# clean_project / set_target / add_dependency（走 idf.py 的工具）全部失败。
# 因此这里去掉前导 v：'v6.1' -> '6.1'
os.environ['ESP_IDF_VERSION'] = os.path.basename(os.path.dirname(IDF_PATH)).lstrip('vV')
# 启用 Component Manager：允许 idf.py add-dependency / build 解析并拉取组件
# (如 esp-nn, esp-tflite-micro)。默认关闭会导致组件依赖无法解析。
os.environ['IDF_COMPONENT_MANAGER'] = '1'

_tools = _find_tools_dir()
os.environ['IDF_TOOLS_PATH'] = _tools
# IDF python venv：env 优先，否则 tools 下取最新（安装器随 IDF 版本生成）
_python_env = os.environ.get('IDF_PYTHON_ENV_PATH') or _newest(os.path.join(_tools, 'python', '*', 'venv'))
if not _python_env or not os.path.isdir(_python_env):
    raise SystemExit('IDF python venv not found: set IDF_PYTHON_ENV_PATH')
os.environ['IDF_PYTHON_ENV_PATH'] = _python_env
IDF_PYTHON = os.path.join(_python_env, 'Scripts', 'python.exe')  # 子进程统一用它（esptool/idf.py/pytest）

# 工具链版本号通配匹配（升级 IDF/工具链无需改代码），注入 PATH
_extra_paths = [
    _newest(os.path.join(_tools, 'xtensa-esp-elf', '*', 'xtensa-esp-elf', 'bin')),
    _newest(os.path.join(_tools, 'riscv32-esp-elf', '*', 'riscv32-esp-elf', 'bin')),
    _newest(os.path.join(_tools, 'esp32ulp-elf', '*', 'esp32ulp-elf', 'bin')),
    _newest(os.path.join(_tools, 'cmake', '*', 'bin')),
    _newest(os.path.join(_tools, 'ninja', '*')),
    _newest(os.path.join(_tools, 'idf-exe', '*')),
    # ninja 用 ccache 当编译器启动器时需要能找到它，否则报
    # "CreateProcess failed: The system cannot find the file specified"
    # EIM 布局多嵌一层：ccache\<ver>\ccache-<ver>-windows-x86_64\ccache.exe
    _newest(os.path.join(_tools, 'ccache', '*', 'ccache-*')) or _newest(os.path.join(_tools, 'ccache', '*')),
    os.path.join(IDF_PATH, 'tools'),
    os.path.join(_python_env, 'Scripts'),
]

# 生成 esp_rom gdbinit 需要该变量，缺失时 project.cmake 会打 CMake Warning
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


### 持久日志：每次调用独立文件，互不串扰、可事后回查；只保留最近 _LOG_KEEP 份 ###
_LOG_DIR = os.path.join(tempfile.gettempdir(), 'esp-idf-mcp-logs')
_LOG_KEEP = 20


def _kill_tree(proc):
    """杀掉子进程及其子孙进程：idf.py 会拉起 cmake/ninja，只杀直接子进程会留下残留
    （继续占 CPU、锁住 build 目录，导致下一次构建失败）。"""
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
    """Flash the built project using esptool directly. Optionally opens a persistent monitor session after flash.
    Monitor sessions on the target port are closed automatically before flashing (no manual monitor_close needed).
    Args:
        project_dir: Absolute path to the ESP-IDF project directory
        port: Serial port (e.g. COM13). When omitted, the first non-COM1 port is auto-detected and used. Only monitor sessions on this port are closed before flashing (skip if none open).
        monitor: Open a persistent monitor session after flashing (default True) so the boot log is captured. Baud auto-detected from sdkconfig; set False to skip.
        wait_after_flash: Seconds to wait after flash before opening the monitor (default 2.0). Lets esptool's hard-reset boot finish, avoiding stale logs in the USB buffer.
    """
    # 烧录前准备：无 port 时先自动检测；确定具体串口后只关闭这个口的监视会话（没开着就跳过）
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
    # 烧录后开一个常驻监视会话（reset=True：板子被复位，会话里只有本次启动的新日志），
    # 立即返回；不用了用 monitor_close 释放。
    if monitor:
        if wait_after_flash > 0:
            time.sleep(wait_after_flash)
        result += '\n' + monitor_open(port, reset=True, project_dir=project_dir)
    return result

# 在 venv python 子进程里执行的读芯片信息脚本（esptool 日志走 stdout，
# 只能放子进程，否则会污染 MCP 的 stdio JSON-RPC 通道）
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
        # esptool 5.x 的 detect_flash_size 返回 '4MB' 这类字符串（或 None）
        info['flash_size'] = detect_flash_size(esp)
    except Exception as e:
        info['flash_error'] = str(e)
    try:
        esp.hard_reset()  # 读完硬复位，让应用恢复运行
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
    # 输出里混有 esptool 的连接日志，取最后一行 JSON
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


_ANSI_RE = re.compile(r'\x1b\[[0-9;]*[A-Za-z]')  # ESP_LOG 彩色输出的 ANSI 转义，对 agent 是纯噪声
# 行首级别+毫秒前缀（'I (12345) '）：ESP_LOG 级别字符为 V/D/I/W/E，折叠比较前剥离
_LOG_PREFIX_RE = re.compile(r'^([IWEVD]) \((\d+)\) ')
# 应用层起点：main_task 的 'Calling app_main()'；裁剪过的工程没有该行时回退到 TAG 为 main 的首条日志
_APP_START_RE = re.compile(r'Calling app_main\b|^[IWEVD] \(\d+\) main:')


def _strip_ts(line: str) -> str:
    """剥掉行首的 'I (12345) ' 前缀，只用于重复比较，显示仍是原文。"""
    m = _LOG_PREFIX_RE.match(line)
    return line[m.end():] if m else line


def _fold_repeats(lines, show_ts: bool):
    """相邻重复行折叠为 `行  *N`（N>1）。比较用剥掉时间戳后的正文，所以毫秒不同、
    消息相同的日志也算重复；show_ts=False 时显示不带 `I (12345) ` 前缀的正文。"""
    runs = []  # [正文, 计数, 首行原文]
    for line in lines:
        key = _strip_ts(line)
        if runs and runs[-1][0] == key:
            runs[-1][1] += 1
        else:
            runs.append([key, 1, line])
    return [f'{(first if show_ts else key)}  *{n}' if n > 1 else (first if show_ts else key)
            for key, n, first in runs]


class SerialSession:
    def __init__(self, port, baud, max_lines=5000):
        self.port = port
        self.baud = baud
        self.buffer = deque(maxlen=max_lines)
        self.total = 0        # 累计接收行数
        self.read_cursor = 0  # 已返回的行数（增量读取游标）
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
                self.total = 0  # 与 buffer/游标同步，否则复位后首次 read 会误报 evicted
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
        self.total += 1

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
                        self.read_cursor = 0
                        self.total = 0     # 与 buffer/游标同步
                        line_buffer = b''  # 丢弃断线残行，避免和重连后的数据粘成一行
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


def _resolve_console_baud(project_dir: Optional[str] = None) -> int:
    """解析串口波特率：有工程目录读 sdkconfig 的 CONFIG_ESP_CONSOLE_UART_BAUDRATE；
    无工程目录（单独开监视器）或读取失败时回退 115200。"""
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


# 会话式串口监视器注册表：session_id -> SerialSession（monitor_open/close 维护）
_MONITORS: dict = {}


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


@mcp.tool(structured_output=False)
def monitor_open(port: str, reset: bool = True, project_dir: Optional[str] = None) -> str:
    """Open a persistent serial monitor session. Returns immediately (non-blocking).
    Args:
        port: Serial port (e.g. COM14)
        reset: Reset the board after opening (default True) so you capture a fresh boot
        project_dir: Project dir for console baud auto-detect (sdkconfig). flash_project passes it automatically; standalone opens fall back to 115200.
    """
    baud = _resolve_console_baud(project_dir)
    sid = f'{port}@{baud}'
    if sid in _MONITORS:
        return f'Session {sid} is already open — use monitor_send / monitor_close.'
    open_on_port = [s for s, sess in _MONITORS.items() if sess.port.upper() == port.strip().upper()]
    if open_on_port:
        return (f'{port} is already monitored as {", ".join(open_on_port)} — monitor_close it '
                f'first if you want to reopen with a different baud.')
    try:
        sess = SerialSession(port, baud)
        sess.start(reset=reset)
    except serial.SerialException as e:
        return f'Failed to open {port}: {e}'
    _MONITORS[sid] = sess
    return (f'Monitor opened: {sid} (reset={"on" if reset else "off"}). '
            f'Send input with monitor_send, release with monitor_close. '
            f'Note: flashing closes existing sessions on the target port before writing, then opens a fresh one; '
            f'read_chip_info auto-closes sessions on the target port; '
            f'monitor_close(session_id) releases a specific session / port.')


@mcp.tool(structured_output=False)
def monitor_read(session_id: str, timestamp: bool = False, max_lines: int = 200) -> str:
    """Read new serial output of an open session since the last read.

    应用层起点用正则匹配：`main_task: Calling app_main()`（或 TAG 为 main 的首条日志）。
    起点之前的系统日志折叠成计数，起点及其后的行原样显示，相邻重复行折叠为 `<行>  *N`
    （比较时忽略行首时间戳，毫秒不同、消息相同也算重复）。本批日志里找不到起点时，
    整批按应用日志显示。
    Args:
        session_id: Session id from monitor_open (e.g. 'COM14@74880')
        timestamp: Keep the leading `I (12345) ` ms prefix (default False: stripped, `TAG: msg` only)
        max_lines: Cap on returned lines after folding (default 200)
    """
    sess = _MONITORS.get(session_id)
    if not sess:
        return f'No session {session_id}. Open one with monitor_open (open sessions: {list(_MONITORS) or "none"}).'

    while True:
        total = sess.total  # 先读计数再拷缓冲：拷贝里多出来的行留给下次返回，不丢不重
        try:
            lines_all = list(sess.buffer)
            break
        except RuntimeError:
            continue  # 读线程恰在 append（deque 迭代中会抛）；串口速率下重试必然立即成功
    base = total - len(lines_all)              # 缓冲首行的绝对行号
    start = max(sess.read_cursor - base, 0)
    new_lines = lines_all[start:]
    evicted = max(base - sess.read_cursor, 0)  # 被 5000 行环形缓冲挤掉的未读行数
    sess.read_cursor = total

    # 正则找应用层起点：之前的系统日志折叠，起点及其后原样显示
    cut = next((i for i, line in enumerate(new_lines) if _APP_START_RE.search(line)), None)
    folded = cut if cut is not None else 0
    app_lines = new_lines[cut:] if cut is not None else new_lines

    shown = _fold_repeats(app_lines, timestamp)

    tail = shown[-max_lines:] if max_lines > 0 and len(shown) > max_lines else shown
    skipped = len(shown) - len(tail)
    notes = f', {evicted} evicted by buffer cap' if evicted else ''
    notes += f', {skipped} older skipped (showing last {len(tail)})' if skipped else ''
    notes += f', {folded} system lines folded' if folded else ''
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
    closed = _close_monitors_on_port(target)  # 按端口名匹配（'COM14' → 'COM14@74880'）
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
    """Start the MCP server with auto-restart on crash (also the pip console entry point)."""
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