"""Restart the Codex Bridge in place: kill old -> wait port -> relaunch -> health.

Persisted under tools/ (previously a fragile %TEMP% one-off). Launched
detached by the tray "重启桥接服务" item; also safe to run by hand:
    pythonw bridge_restart.py
Exit code 0 only when the bridge answers /dashboard/api/health afterwards.
Rollback to kimi_bridge.py.bak happens only when that file actually exists.
"""

import os
import ctypes
import shutil
import socket
import subprocess
import sys
import time
import urllib.request

DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200

HERE = os.path.dirname(os.path.abspath(__file__))
BRIDGE = os.path.join(HERE, "kimi_bridge.py")
BAK = BRIDGE + ".bak"  # optional manual safety copy
HOST, PORT = "127.0.0.1", 15731
HEALTH = "http://{}:{}/dashboard/api/health".format(HOST, PORT)


def bridge_pids():
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='python.exe' or Name='pythonw.exe'\" | "
          "Where-Object { $_.CommandLine -match 'kimi_bridge' } | "
          "Select-Object -ExpandProperty ProcessId")
    out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                         capture_output=True, text=True)
    return [int(x) for x in out.stdout.split() if x.strip().isdigit()]


def port_free(timeout=6.0):
    """Wait until nothing listens on the bridge port anymore."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = socket.socket()
        s.settimeout(0.5)
        busy = (s.connect_ex((HOST, PORT)) == 0)
        s.close()
        if not busy:
            return True
        time.sleep(0.3)
    return False


def kill_pids(pids, timeout=5.0):
    """Terminate only these PIDs; never walk the process tree.

    The restart script is launched as a child of kimi_bridge.  A tree kill
    would kill the restarter before it can relaunch the bridge.
    """
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.c_uint]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    kernel32.TerminateProcess.restype = ctypes.c_int
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    kernel32.WaitForSingleObject.restype = ctypes.c_uint
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    process_terminate = 0x0001
    synchronize = 0x00100000
    for pid in pids:
        handle = kernel32.OpenProcess(process_terminate | synchronize, 0, int(pid))
        if not handle:
            subprocess.run(["powershell", "-NoProfile", "-Command",
                            f"Stop-Process -Id {int(pid)} -Force"],
                           capture_output=True, text=True)
            continue
        try:
            kernel32.TerminateProcess(handle, 1)
            kernel32.WaitForSingleObject(handle, int(timeout * 1000))
        finally:
            kernel32.CloseHandle(handle)


def healthy(timeout=5):
    try:
        with urllib.request.urlopen(HEALTH, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def start_bridge():
    py = sys.executable
    if py.lower().endswith("python.exe"):
        cand = os.path.join(os.path.dirname(py), "pythonw.exe")
        if os.path.exists(cand):
            py = cand
    subprocess.Popen([py, BRIDGE],
                     creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
                     close_fds=True)


def main():
    pids = bridge_pids()
    print("bridge pids:", pids)
    kill_pids(pids)
    if pids and not port_free():
        print("ERROR: old bridge did not terminate; refusing to launch a second process")
        return 2
    if not port_free():
        print("ERROR: bridge port is still busy; refusing to launch a second process")
        return 2
    time.sleep(0.8)

    start_bridge()
    for _ in range(20):
        time.sleep(1)
        if healthy():
            print("health after restart: True")
            return 0
        print("waiting for bridge...")
    print("health after restart: False")

    if os.path.isfile(BAK):  # rollback only when a manual safety copy exists
        print("ROLLBACK ->", BAK)
        shutil.copy(BAK, BRIDGE)
        start_bridge()
        time.sleep(3)
        print("health after rollback:", healthy())
    return 1


if __name__ == "__main__":
    sys.exit(main())
