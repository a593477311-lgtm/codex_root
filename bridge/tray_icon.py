"""Windows notification-area icon for kimi_bridge (zero dependencies).

Left click  -> open the dashboard in the default browser.
Right click -> menu: open dashboard / quit.

Any failure here is non-fatal: the bridge keeps running headless, exactly
as before this module existed.
"""

import ctypes
import logging
import os
import threading

log = logging.getLogger("kimi_bridge.tray")

WM_DESTROY = 0x0002
WM_COMMAND = 0x0111
WM_APP = 0x8000
WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205
WM_CLOSE = 0x0010

NIM_ADD = 0
NIM_DELETE = 2
NIF_MESSAGE = 0x01
NIF_ICON = 0x02
NIF_TIP = 0x04

TPM_RETURNCMD = 0x0100
TPM_RIGHTBUTTON = 0x0002

IMAGE_ICON = 1
LR_LOADFROMFILE = 0x0010
LR_DEFAULTSIZE = 0x0040

MENU_OPEN = 1001
MENU_QUIT = 1002


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


def _run(icon_path, tooltip, url):
    try:
        user32 = ctypes.windll.user32
        shell32 = ctypes.windll.shell32
        kernel32 = ctypes.windll.kernel32

        shell32.SetCurrentProcessExplicitAppUserModelID("codex.kimibridge")

        def wndproc(hwnd, msg, wp, lp):
            if msg == WM_APP:
                if lp == WM_LBUTTONUP:
                    os.startfile(url)
                elif lp == WM_RBUTTONUP:
                    _popup_menu(user32, hwnd, url)
                return 0
            if msg == WM_COMMAND:
                cmd = wp & 0xFFFF
                if cmd == MENU_OPEN:
                    os.startfile(url)
                elif cmd == MENU_QUIT:
                    _quit(user32, shell32, hwnd)
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
        cls.lpszClassName = "KimiBridgeTray"
        if not user32.RegisterClassW(ctypes.byref(cls)):
            raise OSError("RegisterClassW failed")

        WS_OVERLAPPED = 0x00000000
        hwnd = user32.CreateWindowExW(0, "KimiBridgeTray", "Kimi Bridge",
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

        msg = _MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
    except Exception as e:  # pragma: no cover
        log.warning("tray icon unavailable: %s", e)


def _popup_menu(user32, hwnd, url):
    h_menu = user32.CreatePopupMenu()
    user32.AppendMenuW(h_menu, 0, MENU_OPEN, "打开控制台")
    user32.AppendMenuW(h_menu, 0x0800, 0, None)  # separator
    user32.AppendMenuW(h_menu, 0, MENU_QUIT, "退出")
    pt = _POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    user32.SetForegroundWindow(hwnd)
    cmd = user32.TrackPopupMenu(h_menu, TPM_RETURNCMD | TPM_RIGHTBUTTON,
                                pt.x, pt.y, 0, hwnd, None)
    user32.DestroyMenu(h_menu)
    if cmd == MENU_OPEN:
        os.startfile(url)
    elif cmd == MENU_QUIT:
        _quit(user32, ctypes.windll.shell32, hwnd)


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
