"""Self-check for the esp_idf_mcp fixes (version sort, esptool command version, monitor snapshot/cursor math).
Run with an interpreter that can import the server module (needs mcp + pyserial + an ESP-IDF install):
    python test_esp_idf_mcp.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import esp_idf_mcp as m

# _ver_key: numeric order beats lexicographic ('v5.9' > 'v10.0' lexicographically - wrong)
assert m._ver_key(r'C:\x\v10.0') > m._ver_key(r'C:\x\v5.9')
assert m._ver_key(r'esp-14.2.0_20241119') > m._ver_key(r'esp-13.2.0_20230718')

# flash command follows the esptool bundled with the detected IDF version
_old = os.environ['ESP_IDF_VERSION']
try:
    os.environ['ESP_IDF_VERSION'] = '6.1'
    cmd = m._esptool_flash_cmd('COM3')
    assert cmd[-1] == 'write-flash' and cmd[cmd.index('--before') + 1] == 'default-reset', cmd
    os.environ['ESP_IDF_VERSION'] = '5.4'
    cmd = m._esptool_flash_cmd('COM3')
    assert cmd[-1] == 'write_flash' and cmd[cmd.index('--before') + 1] == 'default_reset', cmd
    os.environ['ESP_IDF_VERSION'] = '5.5'  # boundary: v5.5 still pins esptool 4.x -> underscore forms
    cmd = m._esptool_flash_cmd('COM3')
    assert cmd[-1] == 'write_flash', cmd
    os.environ['ESP_IDF_VERSION'] = '6.0'  # boundary: esptool 5.x first ships with IDF v6.0
    cmd = m._esptool_flash_cmd('COM3')
    assert cmd[-1] == 'write-flash', cmd
finally:
    os.environ['ESP_IDF_VERSION'] = _old

# monitor snapshot: cursor math stays consistent across appends (lock-protected snapshot)
sess = m.SerialSession('COMTEST', 115200)
for i in range(3):
    sess._append_line(f'line{i}'.encode())
with sess._buf_lock:
    total, lines = sess.total, list(sess.buffer)
assert total == 3 and len(lines) == 3
base = total - len(lines)
assert lines[max(0 - base, 0):] == ['line0', 'line1', 'line2']  # first read shows everything
sess._append_line(b'line3')
with sess._buf_lock:
    total, lines = sess.total, list(sess.buffer)
base = total - len(lines)
assert lines[max(3 - base, 0):] == ['line3']  # incremental read after cursor 3 shows only the new line
# monitor_read end-to-end: the real function must advance the cursor correctly across calls
sid = 'COMTEST@115200'
m._MONITORS[sid] = sess
try:
    out1 = m.monitor_read(sid)  # the buffer already holds 4 lines from the snapshot checks above
    assert '4 new lines' in out1 and 'line0' in out1, out1
    sess._append_line(b'line4')
    out2 = m.monitor_read(sid)
    assert '1 new lines' in out2 and 'line4' in out2 and 'line0' not in out2, out2
    assert 'line4' in m.monitor_read(sid, full=True)
finally:
    m._MONITORS.pop(sid, None)

# session log: every session rewrites the file from scratch - no cross-session accumulation
import tempfile as _tempfile
_log = os.path.join(_tempfile.mkdtemp(), '.esp_monitor_full.log')
s1 = m.SerialSession('COMT', 115200, log_path=_log)
s1._append_line(b'old crash line')
s1.stop()
s2 = m.SerialSession('COMT', 115200, log_path=_log)
s2._append_line(b'fresh line')
s2.stop()
content = open(_log, encoding='utf-8').read()
assert 'old crash line' not in content and 'fresh line' in content, content

sess.stop()  # never-started session (no thread, no port) must stop cleanly

# build/flash expose a caller-tunable timeout that reaches _run_sync
import inspect
for name in ('build_project', 'flash_project'):
    sig = inspect.signature(getattr(m, name))
    assert sig.parameters['timeout'].default == 600, name

print('all checks passed')
