"""Windows notification-area icon for kimi_bridge (zero dependencies).

Left click  -> open the dashboard in the default browser.
Right click -> six-item service menu:
    打开控制台 / 当前供应商(灰显) / 一键刷新配额 / 重启桥接服务 /
    查看日志文件 / 退出

The 2026-09 rewrite binds argtypes/restype for every user32/shell32 call.
Without them ctypes truncates Win64 handles (HMENU / HWND / LRESULT) to
32 bits, which is exactly why the old menu rendered as a blank
separator-height strip. TrackPopupMenu is also followed by the MSDN-required
PostMessageW(WM_NULL) so the menu dismisses and can be re-opened.

Any failure here is non-fatal: the bridge keeps running headless, exactly
as before this module existed.
"""

import ctypes
import json
import logging
import os
import subprocess
import sys
import threading

log = logging.getLogger("kimi_bridge.tray")

WM_NULL = 0x0000
WM_DESTROY = 0x0002
WM_COMMAND = 0x0111
WM_APP = 0x8000
WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205
WM_CLOSE = 0x0010

NIM_ADD = 0
NIM_MODIFY = 1
NIM_DELETE = 2
NIF_MESSAGE = 0x01
NIF_ICON = 0x02
NIF_TIP = 0x04

TPM_RETURNCMD = 0x0100
TPM_RIGHTBUTTON = 0x0002

MF_STRING = 0x00000000
MF_GRAYED = 0x00000001
MF_SEPARATOR = 0x00000800
MF_DEFAULT = 0x00001000

IMAGE_ICON = 1
LR_LOADFROMFILE = 0x0010
LR_DEFAULTSIZE = 0x0040

DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200

MENU_OPEN = 1001
MENU_QUIT = 1002
MENU_REFRESH = 1003
MENU_RESTART = 1004
MENU_LOG = 1005

_HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(_HERE, "bridge_config.json")
LOG_FILE = os.path.join(_HERE, "kimi_bridge.log")
RESTART_SCRIPT = os.path.join(_HERE, "bridge_restart.py")


class _WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", ctypes.c_uint), ("lpfnWndProc", ctypes.c_void_p),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", ctypes.c_void_p), ("hIcon", ctypes.c_void_p),
                ("hCursor", ctypes.c_void_p), ("hbrBackground", ctypes.c_void_p),
                ("lpszMenuName", ctypes.c_wchar_p), ("lpszClassName", ctypes.c_wchar_p)]


class _NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("hWnd", ctypes.c_void_p),
                ("uID", ctypes.c_uint), ("uFlags", ctypes.c_uint),
                ("uCallbackMessage", ctypes.c_uint), ("hIcon", ctypes.c_void_p),
                ("szTip", ctypes.c_wchar * 128), ("dwState", ctypes.c_uint),
                ("dwStateMask", ctypes.c_uint),
                ("szInfo", ctypes.c_wchar * 256), ("uVersion", ctypes.c_uint),
                ("szInfoTitle", ctypes.c_wchar * 64),
                ("dwInfoFlags", ctypes.c_uint), ("guidItem", ctypes.c_void_p),
                ("hBalloonIcon", ctypes.c_void_p)]


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _MSG(ctypes.Structure):
    _fields_ = [("hwnd", ctypes.c_void_p), ("message", ctypes.c_uint),
                ("wParam", ctypes.c_void_p), ("lParam", ctypes.c_void_p),
                ("time", ctypes.c_uint), ("pt", _POINT)]


_DLLS = None
_NID = None


def _dlls():
    """Load user32/shell32/kernel32 once and bind Win64-safe prototypes."""
    global _DLLS
    if _DLLS is not None:
        return _DLLS
    user32 = ctypes.windll.user32
    shell32 = ctypes.windll.shell32
    kernel32 = ctypes.windll.kernel32

    user32.DefWindowProcW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                      ctypes.c_void_p, ctypes.c_void_p]
    user32.DefWindowProcW.restype = ctypes.c_longlong          # LRESULT is 64-bit
    user32.RegisterClassW.argtypes = [ctypes.POINTER(_WNDCLASSW)]
    user32.RegisterClassW.restype = ctypes.c_ushort            # ATOM
    user32.CreateWindowExW.argtypes = [ctypes.c_uint, ctypes.c_wchar_p, ctypes.c_wchar_p,
                                       ctypes.c_uint, ctypes.c_int, ctypes.c_int,
                                       ctypes.c_int, ctypes.c_int, ctypes.c_void_p,
                                       ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    user32.CreateWindowExW.restype = ctypes.c_void_p
    user32.LoadImageW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint,
                                  ctypes.c_int, ctypes.c_int, ctypes.c_uint]
    user32.LoadImageW.restype = ctypes.c_void_p
    user32.GetMessageW.argtypes = [ctypes.POINTER(_MSG), ctypes.c_void_p,
                                   ctypes.c_uint, ctypes.c_uint]
    user32.GetMessageW.restype = ctypes.c_int
    user32.TranslateMessage.argtypes = [ctypes.POINTER(_MSG)]
    user32.TranslateMessage.restype = ctypes.c_int
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(_MSG)]
    user32.DispatchMessageW.restype = ctypes.c_longlong
    user32.GetCursorPos.argtypes = [ctypes.POINTER(_POINT)]
    user32.GetCursorPos.restype = ctypes.c_int
    user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
    user32.SetForegroundWindow.restype = ctypes.c_int
    user32.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                    ctypes.c_void_p, ctypes.c_void_p]
    user32.PostMessageW.restype = ctypes.c_int
    user32.CreatePopupMenu.restype = ctypes.c_void_p           # HMENU, full 64 bits
    user32.AppendMenuW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                   ctypes.c_size_t, ctypes.c_wchar_p]
    user32.AppendMenuW.restype = ctypes.c_int
    user32.GetMenuItemCount.argtypes = [ctypes.c_void_p]
    user32.GetMenuItemCount.restype = ctypes.c_int
    user32.TrackPopupMenu.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int,
                                      ctypes.c_int, ctypes.c_int, ctypes.c_void_p,
                                      ctypes.c_void_p]
    user32.TrackPopupMenu.restype = ctypes.c_int               # cmd id under TPM_RETURNCMD
    user32.DestroyMenu.argtypes = [ctypes.c_void_p]
    user32.DestroyMenu.restype = ctypes.c_int
    shell32.Shell_NotifyIconW.argtypes = [ctypes.c_uint, ctypes.POINTER(_NOTIFYICONDATAW)]
    shell32.Shell_NotifyIconW.restype = ctypes.c_int
    shell32.SetCurrentProcessExplicitAppUserModelID.argtypes = [ctypes.c_wchar_p]
    shell32.SetCurrentProcessExplicitAppUserModelID.restype = ctypes.c_int
    kernel32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
    kernel32.GetModuleHandleW.restype = ctypes.c_void_p

    _DLLS = (user32, shell32, kernel32)
    return _DLLS


def start(icon_path, tooltip, url):
    """Show the tray icon on a daemon thread. Never raises."""
    if os.name != "nt" or not os.path.exists(icon_path):
        return
    try:
        t = threading.Thread(target=_run, args=(icon_path, tooltip, url),
                             daemon=True, name="tray-icon")
        t.start()
    except Exception as e:  # pragma: no cover
        log.warning("tray icon thread failed to start: %s", e)


def update_tooltip(text):
    """Refresh the tray hover tooltip (NIM_MODIFY); safe from any thread."""
    nid = _NID
    if nid is None:
        return
    try:
        _, shell32, _ = _dlls()
        nid.szTip = str(text).replace("\r", "")[:127]
        shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))
    except Exception as e:
        log.debug("tooltip update failed: %s", e)


def _route_label():
    """Grayed status line, re-read from disk on every right-click."""
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            cfg = json.load(f)
        providers = cfg.get("providers", {})
        pid = str(cfg.get("active_provider", "") or "")
        name = (providers.get(pid) or {}).get("name") or pid or "未知"
        return "模型路由: 按模型自动分发(%d家) · 兜底 %s" % (len(providers), name)
    except Exception as e:
        log.debug("route label unavailable: %s", e)
        return "模型路由: 按模型自动分发 · 兜底未知"


def _build_menu(user32):
    """Fresh menu on every right-click so the status line is live."""
    m = user32.CreatePopupMenu()
    user32.AppendMenuW(m, MF_STRING | MF_DEFAULT, MENU_OPEN, "打开控制台")
    user32.AppendMenuW(m, MF_SEPARATOR, 0, None)
    user32.AppendMenuW(m, MF_STRING | MF_GRAYED, 0, _route_label())
    user32.AppendMenuW(m, MF_SEPARATOR, 0, None)
    user32.AppendMenuW(m, MF_STRING, MENU_REFRESH, "一键刷新配额")
    user32.AppendMenuW(m, MF_STRING, MENU_RESTART, "重启桥接服务")
    user32.AppendMenuW(m, MF_STRING, MENU_LOG, "查看日志文件")
    user32.AppendMenuW(m, MF_SEPARATOR, 0, None)
    user32.AppendMenuW(m, MF_STRING, MENU_QUIT, "退出")
    return m


def _launch_restart():
    """Detach-launch the persistent restart script; it outlives our death."""
    py = sys.executable
    if py.lower().endswith("python.exe"):
        cand = os.path.join(os.path.dirname(py), "pythonw.exe")
        if os.path.exists(cand):
            py = cand
    try:
        subprocess.Popen([py, RESTART_SCRIPT],
                         creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
                         close_fds=True)
        log.info("bridge restart requested via %s", RESTART_SCRIPT)
    except Exception as e:
        log.warning("restart launch failed: %s", e)


def _dispatch(cmd, user32, shell32, hwnd, url):
    if cmd == MENU_OPEN:
        os.startfile(url)
    elif cmd == MENU_REFRESH:
        sep = "&" if "?" in url else "?"
        os.startfile(url + sep + "bal_refresh=1")
    elif cmd == MENU_RESTART:
        _launch_restart()
    elif cmd == MENU_LOG:
        if os.path.isfile(LOG_FILE):
            os.startfile(LOG_FILE)
    elif cmd == MENU_QUIT:
        _quit(user32, shell32, hwnd)


def _popup_menu(user32, shell32, hwnd, url):
    h_menu = _build_menu(user32)
    pt = _POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    user32.SetForegroundWindow(hwnd)
    cmd = user32.TrackPopupMenu(h_menu, TPM_RETURNCMD | TPM_RIGHTBUTTON,
                                pt.x, pt.y, 0, hwnd, None)
    # MSDN: the owner window must receive WM_NULL after TrackPopupMenu,
    # otherwise the menu misbehaves on the next open / click-away.
    user32.PostMessageW(hwnd, WM_NULL, None, None)
    user32.DestroyMenu(h_menu)
    if cmd:
        _dispatch(cmd, user32, shell32, hwnd, url)


def _quit(user32, shell32, hwnd):
    nid = _NOTIFYICONDATAW()
    nid.cbSize = ctypes.sizeof(nid)
    nid.hWnd = hwnd
    nid.uID = 1
    shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
    user32.PostQuitMessage(0)
    # Exit from the tray menu is an explicit admin action; the bridge is a
    # stateless local proxy, so a hard exit is the intended behaviour.
    os._exit(0)


def _run(icon_path, tooltip, url):
    try:
        user32, shell32, kernel32 = _dlls()

        shell32.SetCurrentProcessExplicitAppUserModelID("codex.codexbridge")

        def wndproc(hwnd, msg, wp, lp):
            if msg == WM_APP:
                if lp == WM_LBUTTONUP:
                    os.startfile(url)
                elif lp == WM_RBUTTONUP:
                    _popup_menu(user32, shell32, hwnd, url)
                return 0
            if msg == WM_COMMAND:
                _dispatch(wp & 0xFFFF, user32, shell32, hwnd, url)
                return 0
            if msg in (WM_CLOSE, WM_DESTROY):
                _quit(user32, shell32, hwnd)
                return 0
            return user32.DefWindowProcW(hwnd, msg, wp, lp)

        # Keep a reference so the callback is never GC'd mid-dispatch.
        _run.proc = ctypes.WINFUNCTYPE(
            ctypes.c_longlong, ctypes.c_void_p, ctypes.c_uint,
            ctypes.c_void_p, ctypes.c_void_p)(wndproc)

        h_inst = kernel32.GetModuleHandleW(None)
        cls = _WNDCLASSW()
        cls.lpfnWndProc = ctypes.cast(_run.proc, ctypes.c_void_p)
        cls.hInstance = h_inst
        cls.lpszClassName = "CodexBridgeTray"
        if not user32.RegisterClassW(ctypes.byref(cls)):
            raise OSError("RegisterClassW failed")

        WS_OVERLAPPED = 0x00000000
        hwnd = user32.CreateWindowExW(0, "CodexBridgeTray", "Codex Bridge",
                                      WS_OVERLAPPED, 0, 0, 0, 0, None, None,
                                      h_inst, None)
        if not hwnd:
            raise OSError("CreateWindowExW failed")
        _run.hwnd = hwnd

        h_icon = user32.LoadImageW(None, icon_path, IMAGE_ICON, 0, 0,
                                   LR_LOADFROMFILE | LR_DEFAULTSIZE)
        if not h_icon:
            raise OSError("LoadImageW(.ico) failed")

        nid = _NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(nid)
        nid.hWnd = hwnd
        nid.uID = 1
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_APP
        nid.hIcon = h_icon
        nid.szTip = tooltip[:127]
        if not shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
            raise OSError("Shell_NotifyIconW failed")
        global _NID
        _NID = nid

        msg = _MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
    except Exception as e:  # pragma: no cover
        log.warning("tray icon unavailable: %s", e)
