"""Transparent animated usage card for the Codex Bridge tray icon.

The card uses WS_EX_LAYERED with real per-pixel alpha.  GDI+ renders an ARGB
card into a top-down DIB, the bytes are premultiplied, and UpdateLayeredWindow
composites it.  Corners are genuinely alpha=0, so no window rectangle remains.
"""

import ctypes
import logging
import math
import os
import threading
import time


log = logging.getLogger("kimi_bridge.tray-overlay")

WM_NULL = 0x0000
WM_CLOSE = 0x0010
WM_PAINT = 0x000F
WM_ERASEBKGND = 0x0014
WM_TIMER = 0x0113
WM_LBUTTONUP = 0x0202
WM_MOUSEMOVE = 0x0200
WM_MOUSELEAVE = 0x02A2
WM_NCHITTEST = 0x0084
WM_SETCURSOR = 0x0020
WM_APP_SHOW = 0x8001

CS_HREDRAW = 0x0002
CS_VREDRAW = 0x0001
WS_POPUP = 0x80000000

# TOPMOST | NOACTIVATE | TOOLWINDOW | LAYERED
EX_STYLE = 0x00000008 | 0x08000000 | 0x00000080 | 0x00080000

SW_HIDE = 0
SW_SHOWNOACTIVATE = 4
HWND_TOPMOST = -1
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040

IDC_ARROW = 32512
IDC_HAND = 32649
SM_CXSCREEN = 0
SM_CYSCREEN = 1
SRCCOPY = 0x00CC0020
ULW_ALPHA = 2
AC_SRC_OVER = 0
AC_SRC_ALPHA = 1
PIXEL_FORMAT_32BPP_ARGB = 0x0026200A

UNIT_PIXEL = 2
FONT_REGULAR = 0
FONT_BOLD = 1
FONT_BOLD_ITALIC = 3
SMOOTHING_ANTIALIAS = 4
TEXT_ANTIALIAS_CLEAR_TYPE = 5
STRING_ALIGNMENT_NEAR = 0
STRING_ALIGNMENT_FAR = 2

TME_LEAVE = 0x00000002
DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWCP_ROUND = 2

# Matches the card color from the user's screenshot.
CARD_RGB = 0x13121A
BORDER_RGB = 0x2E2C3D
DIVIDER_RGB = 0x232130
INK_RGB = 0xF2F1F8
SUB_RGB = 0x8E8A9E
FAINT_RGB = 0x6E6B80
ACCENT_RGB = 0x7C65C1

MODEL_COLORS = {
    "GLM-5.3-Flash": 0x06B6D4,
    "GLM-5.3": 0x3B82F6,
    "gemini-3.7-flash-high": 0x22C55E,
    "gemini-3.8-flash-high": 0x10B981,
    "MiniMax-M3": 0xF59E0B,
    "k3": 0x8B5CF6,
    "GLM-5.2": 0x64748B,
}


def _get_model_color(model_name):
    if not model_name:
        return 0x8E8A9E
    return MODEL_COLORS.get(model_name, 0x8E8A9E)

W = 362
H = 266
ANIM_MS = 620
STAGGER_MS = 75
FRAME_MS = 16
CHECK_MS = 110
ANCHOR_RADIUS = 48
SEAM_OVERLAP_DEGREES = 1.0
CARD_CORNER_DIAMETER = 48


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class _MSG(ctypes.Structure):
    _fields_ = [("hwnd", ctypes.c_void_p), ("message", ctypes.c_uint),
                ("wParam", ctypes.c_void_p), ("lParam", ctypes.c_void_p),
                ("time", ctypes.c_uint), ("pt", _POINT)]


class _WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", ctypes.c_uint), ("lpfnWndProc", ctypes.c_void_p),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", ctypes.c_void_p), ("hIcon", ctypes.c_void_p),
                ("hCursor", ctypes.c_void_p), ("hbrBackground", ctypes.c_void_p),
                ("lpszMenuName", ctypes.c_wchar_p), ("lpszClassName", ctypes.c_wchar_p)]


class _TRACKMOUSEEVENT(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwFlags", ctypes.c_uint),
                ("hwndTrack", ctypes.c_void_p), ("dwHoverTime", ctypes.c_uint)]


class _GdiplusStartupInput(ctypes.Structure):
    _fields_ = [("GdiplusVersion", ctypes.c_uint),
                ("DebugEventCallback", ctypes.c_void_p),
                ("SuppressBackgroundThread", ctypes.c_int),
                ("SuppressExternalCodecs", ctypes.c_int)]


class _RECTF(ctypes.Structure):
    _fields_ = [("x", ctypes.c_float), ("y", ctypes.c_float),
                ("width", ctypes.c_float), ("height", ctypes.c_float)]


class _SIZE(ctypes.Structure):
    _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]


class _BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_byte), ("BlendFlags", ctypes.c_byte),
                ("SourceConstantAlpha", ctypes.c_byte), ("AlphaFormat", ctypes.c_byte)]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", ctypes.c_uint), ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long), ("biPlanes", ctypes.c_ushort),
                ("biBitCount", ctypes.c_ushort), ("biCompression", ctypes.c_uint),
                ("biSizeImage", ctypes.c_uint), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", ctypes.c_uint),
                ("biClrImportant", ctypes.c_uint)]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", ctypes.c_uint * 3)]


_DLLS = None
_TOKEN = None
_READY = threading.Event()
_THREAD_LOCK = threading.Lock()
_THREAD = None
_hwnd = None
_proc = None
_mouse_tracked = False
_anim_start = 0.0
_anchor = None
_url = ""

_STATE = {
    "rows": [],
    "total": 0,
    "requests": 0,
    "title": "今日模型用量",
    "updated": 0.0,
}
_STATE_LOCK = threading.Lock()

_FAMILY = None
_FONT_CACHE = {}
_FORMAT_CACHE = {}
_BRUSH_CACHE = {}
_PEN_CACHE = {}
_POS = [0, 0]


def _dlls():
    """Create private WinDLL instances and bind every Win64-sensitive call."""
    global _DLLS
    if _DLLS is not None:
        return _DLLS

    user32 = ctypes.WinDLL("user32")
    kernel32 = ctypes.WinDLL("kernel32")
    gdi32 = ctypes.WinDLL("gdi32")
    gdiplus = ctypes.WinDLL("gdiplus")
    dwmapi = ctypes.WinDLL("dwmapi")
    # PostMessageW gets its own instance.  This avoids the bizarre shared
    # argtypes poisoning seen on this host and keeps overlay/tray isolated.
    post_user32 = ctypes.WinDLL("user32", use_last_error=True)

    user32.DefWindowProcW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                      ctypes.c_void_p, ctypes.c_void_p]
    user32.DefWindowProcW.restype = ctypes.c_longlong
    user32.RegisterClassW.argtypes = [ctypes.POINTER(_WNDCLASSW)]
    user32.RegisterClassW.restype = ctypes.c_ushort
    user32.CreateWindowExW.argtypes = [ctypes.c_uint, ctypes.c_wchar_p, ctypes.c_wchar_p,
                                       ctypes.c_uint, ctypes.c_int, ctypes.c_int,
                                       ctypes.c_int, ctypes.c_int, ctypes.c_void_p,
                                       ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    user32.CreateWindowExW.restype = ctypes.c_void_p
    user32.GetMessageW.argtypes = [ctypes.POINTER(_MSG), ctypes.c_void_p,
                                   ctypes.c_uint, ctypes.c_uint]
    user32.GetMessageW.restype = ctypes.c_int
    user32.TranslateMessage.argtypes = [ctypes.POINTER(_MSG)]
    user32.TranslateMessage.restype = ctypes.c_int
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(_MSG)]
    user32.DispatchMessageW.restype = ctypes.c_longlong
    user32.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
    user32.SetWindowPos.restype = ctypes.c_int
    user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
    user32.ShowWindow.restype = ctypes.c_int
    user32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(_RECT)]
    user32.GetWindowRect.restype = ctypes.c_int
    user32.GetCursorPos.argtypes = [ctypes.POINTER(_POINT)]
    user32.GetCursorPos.restype = ctypes.c_int
    user32.SetTimer.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p]
    user32.SetTimer.restype = ctypes.c_void_p
    user32.KillTimer.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    user32.KillTimer.restype = ctypes.c_int
    user32.TrackMouseEvent.argtypes = [ctypes.POINTER(_TRACKMOUSEEVENT)]
    user32.TrackMouseEvent.restype = ctypes.c_int
    user32.LoadCursorW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    user32.LoadCursorW.restype = ctypes.c_void_p
    user32.SetCursor.argtypes = [ctypes.c_void_p]
    user32.SetCursor.restype = ctypes.c_void_p
    user32.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                    ctypes.c_void_p, ctypes.c_void_p]
    user32.PostMessageW.restype = ctypes.c_int
    user32.ValidateRect.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    user32.ValidateRect.restype = ctypes.c_int
    user32.GetDC.argtypes = [ctypes.c_void_p]
    user32.GetDC.restype = ctypes.c_void_p
    user32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    user32.ReleaseDC.restype = ctypes.c_int
    user32.SetWindowRgn.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int]
    user32.SetWindowRgn.restype = ctypes.c_int
    user32.UpdateLayeredWindow.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                           ctypes.POINTER(_POINT), ctypes.POINTER(_SIZE),
                                           ctypes.c_void_p, ctypes.POINTER(_POINT),
                                           ctypes.c_uint, ctypes.POINTER(_BLENDFUNCTION),
                                           ctypes.c_uint]
    user32.UpdateLayeredWindow.restype = ctypes.c_int
    user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    user32.GetSystemMetrics.restype = ctypes.c_int
    kernel32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
    kernel32.GetModuleHandleW.restype = ctypes.c_void_p

    gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
    gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
    gdi32.CreateCompatibleBitmap.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
    gdi32.CreateCompatibleBitmap.restype = ctypes.c_void_p
    gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    gdi32.SelectObject.restype = ctypes.c_void_p
    gdi32.DeleteDC.argtypes = [ctypes.c_void_p]
    gdi32.DeleteDC.restype = ctypes.c_int
    gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
    gdi32.DeleteObject.restype = ctypes.c_int
    gdi32.CreateRoundRectRgn.argtypes = [ctypes.c_int, ctypes.c_int,
                                         ctypes.c_int, ctypes.c_int,
                                         ctypes.c_int, ctypes.c_int]
    gdi32.CreateRoundRectRgn.restype = ctypes.c_void_p
    gdi32.CreateDIBSection.argtypes = [ctypes.c_void_p, ctypes.POINTER(_BITMAPINFO),
                                       ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p),
                                       ctypes.c_void_p, ctypes.c_uint]
    gdi32.CreateDIBSection.restype = ctypes.c_void_p
    gdi32.BitBlt.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                             ctypes.c_int, ctypes.c_int, ctypes.c_void_p,
                             ctypes.c_int, ctypes.c_int, ctypes.c_uint]
    gdi32.BitBlt.restype = ctypes.c_int

    gdiplus.GdiplusStartup.argtypes = [ctypes.POINTER(ctypes.c_void_p),
                                       ctypes.POINTER(_GdiplusStartupInput), ctypes.c_void_p]
    gdiplus.GdiplusStartup.restype = ctypes.c_int
    gdiplus.GdiplusShutdown.argtypes = [ctypes.c_void_p]
    gdiplus.GdiplusShutdown.restype = None
    gdiplus.GdipCreateFromHDC.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    gdiplus.GdipCreateFromHDC.restype = ctypes.c_int
    gdiplus.GdipCreateBitmapFromScan0.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                                  ctypes.c_int, ctypes.c_void_p,
                                                  ctypes.POINTER(ctypes.c_void_p)]
    gdiplus.GdipCreateBitmapFromScan0.restype = ctypes.c_int
    gdiplus.GdipDisposeImage.argtypes = [ctypes.c_void_p]
    gdiplus.GdipDisposeImage.restype = ctypes.c_int
    gdiplus.GdipGetImageGraphicsContext.argtypes = [ctypes.c_void_p,
                                                    ctypes.POINTER(ctypes.c_void_p)]
    gdiplus.GdipGetImageGraphicsContext.restype = ctypes.c_int
    gdiplus.GdipDeleteGraphics.argtypes = [ctypes.c_void_p]
    gdiplus.GdipDeleteGraphics.restype = ctypes.c_int
    gdiplus.GdipFlush.argtypes = [ctypes.c_void_p, ctypes.c_int]
    gdiplus.GdipFlush.restype = ctypes.c_int
    gdiplus.GdipSetSmoothingMode.argtypes = [ctypes.c_void_p, ctypes.c_int]
    gdiplus.GdipSetSmoothingMode.restype = ctypes.c_int
    gdiplus.GdipSetTextRenderingHint.argtypes = [ctypes.c_void_p, ctypes.c_int]
    gdiplus.GdipSetTextRenderingHint.restype = ctypes.c_int
    gdiplus.GdipCreateFontFamilyFromName.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p,
                                                     ctypes.POINTER(ctypes.c_void_p)]
    gdiplus.GdipCreateFontFamilyFromName.restype = ctypes.c_int
    gdiplus.GdipLoadImageFromFile.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_void_p)]
    gdiplus.GdipLoadImageFromFile.restype = ctypes.c_int
    gdiplus.GdipDrawImageRectRect.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_float, ctypes.c_float, ctypes.c_float, ctypes.c_float,
        ctypes.c_float, ctypes.c_float, ctypes.c_float, ctypes.c_float,
        ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p
    ]
    gdiplus.GdipDrawImageRectRect.restype = ctypes.c_int
    gdiplus.GdipDisposeImage.argtypes = [ctypes.c_void_p]
    gdiplus.GdipDisposeImage.restype = ctypes.c_int
    gdiplus.GdipGetGenericFontFamilySansSerif.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
    gdiplus.GdipGetGenericFontFamilySansSerif.restype = ctypes.c_int
    gdiplus.GdipDeleteFontFamily.argtypes = [ctypes.c_void_p]
    gdiplus.GdipDeleteFontFamily.restype = ctypes.c_int
    gdiplus.GdipCreateFont.argtypes = [ctypes.c_void_p, ctypes.c_float,
                                       ctypes.c_int, ctypes.c_int,
                                       ctypes.POINTER(ctypes.c_void_p)]
    gdiplus.GdipCreateFont.restype = ctypes.c_int
    gdiplus.GdipDeleteFont.argtypes = [ctypes.c_void_p]
    gdiplus.GdipDeleteFont.restype = ctypes.c_int
    gdiplus.GdipCreateStringFormat.argtypes = [ctypes.c_int, ctypes.c_int,
                                               ctypes.POINTER(ctypes.c_void_p)]
    gdiplus.GdipCreateStringFormat.restype = ctypes.c_int
    gdiplus.GdipDeleteStringFormat.argtypes = [ctypes.c_void_p]
    gdiplus.GdipDeleteStringFormat.restype = ctypes.c_int
    gdiplus.GdipSetStringFormatAlign.argtypes = [ctypes.c_void_p, ctypes.c_int]
    gdiplus.GdipSetStringFormatAlign.restype = ctypes.c_int
    gdiplus.GdipSetStringFormatLineAlign.argtypes = [ctypes.c_void_p, ctypes.c_int]
    gdiplus.GdipSetStringFormatLineAlign.restype = ctypes.c_int
    gdiplus.GdipCreateSolidFill.argtypes = [ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p)]
    gdiplus.GdipCreateSolidFill.restype = ctypes.c_int
    gdiplus.GdipDeleteBrush.argtypes = [ctypes.c_void_p]
    gdiplus.GdipDeleteBrush.restype = ctypes.c_int
    gdiplus.GdipCreatePen1.argtypes = [ctypes.c_uint, ctypes.c_float,
                                       ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)]
    gdiplus.GdipCreatePen1.restype = ctypes.c_int
    gdiplus.GdipDeletePen.argtypes = [ctypes.c_void_p]
    gdiplus.GdipDeletePen.restype = ctypes.c_int
    gdiplus.GdipCreatePath.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)]
    gdiplus.GdipCreatePath.restype = ctypes.c_int
    gdiplus.GdipDeletePath.argtypes = [ctypes.c_void_p]
    gdiplus.GdipDeletePath.restype = ctypes.c_int
    gdiplus.GdipAddPathArc.argtypes = [ctypes.c_void_p, ctypes.c_float, ctypes.c_float,
                                       ctypes.c_float, ctypes.c_float,
                                       ctypes.c_float, ctypes.c_float]
    gdiplus.GdipAddPathArc.restype = ctypes.c_int
    gdiplus.GdipClosePathFigure.argtypes = [ctypes.c_void_p]
    gdiplus.GdipClosePathFigure.restype = ctypes.c_int
    gdiplus.GdipFillPath.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    gdiplus.GdipFillPath.restype = ctypes.c_int
    gdiplus.GdipDrawPath.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    gdiplus.GdipDrawPath.restype = ctypes.c_int
    gdiplus.GdipDrawArc.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                    ctypes.c_float, ctypes.c_float,
                                    ctypes.c_float, ctypes.c_float,
                                    ctypes.c_float, ctypes.c_float]
    gdiplus.GdipDrawArc.restype = ctypes.c_int
    gdiplus.GdipFillEllipse.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                        ctypes.c_float, ctypes.c_float,
                                        ctypes.c_float, ctypes.c_float]
    gdiplus.GdipFillEllipse.restype = ctypes.c_int
    gdiplus.GdipDrawLine.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                     ctypes.c_float, ctypes.c_float,
                                     ctypes.c_float, ctypes.c_float]
    gdiplus.GdipDrawLine.restype = ctypes.c_int
    gdiplus.GdipDrawString.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int,
                                       ctypes.c_void_p, ctypes.POINTER(_RECTF),
                                       ctypes.c_void_p, ctypes.c_void_p]
    gdiplus.GdipDrawString.restype = ctypes.c_int
    gdiplus.GdipMeasureString.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int,
                                          ctypes.c_void_p, ctypes.POINTER(_RECTF),
                                          ctypes.c_void_p, ctypes.POINTER(_RECTF),
                                          ctypes.c_void_p, ctypes.c_void_p]
    gdiplus.GdipMeasureString.restype = ctypes.c_int

    dwmapi.DwmSetWindowAttribute.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                             ctypes.c_void_p, ctypes.c_uint]
    dwmapi.DwmSetWindowAttribute.restype = ctypes.c_int

    post_user32.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                         ctypes.c_void_p, ctypes.c_void_p]
    post_user32.PostMessageW.restype = ctypes.c_int

    _DLLS = (user32, kernel32, gdi32, gdiplus, dwmapi, post_user32)
    return _DLLS


def set_usage(rows, total, requests, title="Codex Bridge", hit_rate=None):
    """Cache usage rows.  Safe to call from the bridge or tooltip worker."""
    try:
        clean = []
        for row in rows or []:
            model = str(row.get("model") or "?")
            if model == "?":
                continue
            clean.append((model, int(row.get("tokens_total") or 0),
                          int(row.get("requests") or 0)))
        clean.sort(key=lambda item: item[1], reverse=True)
        with _STATE_LOCK:
            _STATE["rows"] = clean[:6]
            _STATE["total"] = int(total or 0)
            _STATE["requests"] = int(requests or 0)
            _STATE["title"] = str(title or "Codex Bridge")
            if hit_rate is not None:
                _STATE["hit_rate"] = float(hit_rate)
            _STATE["updated"] = time.time()
    except Exception as e:
        log.debug("overlay set_usage failed: %s", e)


def _argb(rgb, alpha=255):
    r = (rgb >> 16) & 255
    g = (rgb >> 8) & 255
    b = rgb & 255
    return (int(alpha) << 24) | (r << 16) | (g << 8) | b


def _fmt_tokens(value):
    value = int(value or 0)
    if value >= 100000000:
        val = value / 100000000.0
        text = ("%.2f" % val).rstrip("0").rstrip(".")
        return text + " 亿"
    elif value >= 10000:
        val = value / 10000.0
        text = ("%.1f" % val).rstrip("0").rstrip(".")
        return text + " 万"
    return str(value)


def _family(gdiplus):
    """Use the screenshot's UI family; YaHei explicitly covers CJK text."""
    global _FAMILY
    if _FAMILY is not None:
        return _FAMILY
    family = ctypes.c_void_p()
    for name in ("Segoe UI Variable Display", "Segoe UI", "Microsoft YaHei UI"):
        if gdiplus.GdipCreateFontFamilyFromName(name, None, ctypes.byref(family)) == 0:
            _FAMILY = family
            return family
    gdiplus.GdipGetGenericFontFamilySansSerif(ctypes.byref(family))
    _FAMILY = family
    return family


def _font(gdiplus, size, style=FONT_REGULAR, cjk=False):
    key = ("cjk" if cjk else "latin", float(size), int(style))
    font = _FONT_CACHE.get(key)
    if font:
        return font
    if cjk:
        family = ctypes.c_void_p()
        status = gdiplus.GdipCreateFontFamilyFromName("Microsoft YaHei UI", None,
                                                      ctypes.byref(family))
        if status != 0:
            family = _family(gdiplus)
    else:
        family = _family(gdiplus)
    result = ctypes.c_void_p()
    if gdiplus.GdipCreateFont(family, float(size), int(style), UNIT_PIXEL,
                              ctypes.byref(result)) != 0:
        raise OSError("GdipCreateFont failed")
    _FONT_CACHE[key] = result
    return result


def _format(gdiplus, align=STRING_ALIGNMENT_NEAR):
    key = int(align)
    fmt = _FORMAT_CACHE.get(key)
    if fmt:
        return fmt
    result = ctypes.c_void_p()
    if gdiplus.GdipCreateStringFormat(0, 0, ctypes.byref(result)) != 0:
        raise OSError("GdipCreateStringFormat failed")
    if key != STRING_ALIGNMENT_NEAR:
        gdiplus.GdipSetStringFormatAlign(result, key)
        gdiplus.GdipSetStringFormatLineAlign(result, 1)
    _FORMAT_CACHE[key] = result
    return result


def _brush(gdiplus, rgb, alpha=255):
    color = _argb(rgb, alpha)
    brush = _BRUSH_CACHE.get(color)
    if brush:
        return brush
    result = ctypes.c_void_p()
    if gdiplus.GdipCreateSolidFill(color, ctypes.byref(result)) != 0:
        raise OSError("GdipCreateSolidFill failed")
    _BRUSH_CACHE[color] = result
    return result


def _pen(gdiplus, rgb, width, alpha=255):
    color = _argb(rgb, alpha)
    key = (color, float(width))
    pen = _PEN_CACHE.get(key)
    if pen:
        return pen
    result = ctypes.c_void_p()
    if gdiplus.GdipCreatePen1(color, float(width), UNIT_PIXEL,
                              ctypes.byref(result)) != 0:
        raise OSError("GdipCreatePen1 failed")
    _PEN_CACHE[key] = result
    return result


def _draw_text(gdiplus, graphics, text, font, fmt, rgb, x, y, w, h):
    gdiplus.GdipDrawString(graphics, str(text), len(str(text)), font,
                           ctypes.byref(_RECTF(float(x), float(y), float(w), float(h))),
                           fmt, _brush(gdiplus, rgb))


def _measure(gdiplus, graphics, text, font, fmt, width=10000.0):
    layout = _RECTF(0.0, 0.0, float(width), 1000.0)
    bounds = _RECTF()
    gdiplus.GdipMeasureString(graphics, str(text), len(str(text)), font,
                              ctypes.byref(layout), fmt, ctypes.byref(bounds),
                              None, None)
    return float(bounds.width)


def _fit_text(gdiplus, graphics, text, font, fmt, max_width):
    """Hard-guarantee that the name column never enters the percent column."""
    text = str(text or "")
    if _measure(gdiplus, graphics, text, font, fmt) <= max_width:
        return text
    for cut in range(len(text), 0, -1):
        candidate = text[:cut].rstrip() + "…"
        if _measure(gdiplus, graphics, candidate, font, fmt) <= max_width:
            return candidate
    return "…"


def _rounded_path(gdiplus, x, y, w, h, radius):
    path = ctypes.c_void_p()
    if gdiplus.GdipCreatePath(0, ctypes.byref(path)) != 0:
        raise OSError("GdipCreatePath failed")
    diameter = 2.0 * radius
    gdiplus.GdipAddPathArc(path, x, y, diameter, diameter, 180.0, 90.0)
    gdiplus.GdipAddPathArc(path, x + w - diameter, y, diameter, diameter, 270.0, 90.0)
    gdiplus.GdipAddPathArc(path, x + w - diameter, y + h - diameter, diameter, diameter, 0.0, 90.0)
    gdiplus.GdipAddPathArc(path, x, y + h - diameter, diameter, diameter, 90.0, 90.0)
    gdiplus.GdipClosePathFigure(path)
    return path


def _tokens_or_zero(rows, index):
    try:
        return rows[index][1]
    except (IndexError, TypeError):
        return 0


def _draw_card(gdiplus, graphics):
    """Draw the complete opaque card in client coordinates."""
    with _STATE_LOCK:
        rows = list(_STATE["rows"])
        total = int(_STATE["total"])
        requests = int(_STATE["requests"])
        hit_rate = _STATE.get("hit_rate")

    gdiplus.GdipSetSmoothingMode(graphics, SMOOTHING_ANTIALIAS)
    gdiplus.GdipSetTextRenderingHint(graphics, TEXT_ANTIALIAS_CLEAR_TYPE)

    # Background & border
    background = _rounded_path(gdiplus, 0.5, 0.5, W - 1.0, H - 1.0, 16.0)
    gdiplus.GdipFillPath(graphics, _brush(gdiplus, CARD_RGB), background)
    gdiplus.GdipDrawPath(graphics, _pen(gdiplus, BORDER_RGB, 1.0), background)
    gdiplus.GdipDeletePath(background)

    # 1. Header: Bridge icon
    icon_path = os.path.join(os.path.dirname(__file__), "assets", "bridge-icon.png")
    icon_img = ctypes.c_void_p()
    if os.path.exists(icon_path) and gdiplus.GdipLoadImageFromFile(icon_path, ctypes.byref(icon_img)) == 0:
        gdiplus.GdipDrawImageRectRect(
            graphics, icon_img,
            ctypes.c_float(20.0), ctypes.c_float(17.0), ctypes.c_float(20.0), ctypes.c_float(20.0),
            ctypes.c_float(0.0), ctypes.c_float(0.0), ctypes.c_float(256.0), ctypes.c_float(256.0),
            UNIT_PIXEL, None, None, None
        )
        gdiplus.GdipDisposeImage(icon_img)

    # Title (clean, upright semibold typography)
    title_font = _font(gdiplus, 14.5, FONT_BOLD)
    left_fmt = _format(gdiplus, 0)
    right_fmt = _format(gdiplus, STRING_ALIGNMENT_FAR)
    center_fmt = _format(gdiplus, 1)

    _draw_text(gdiplus, graphics, "Codex Bridge", title_font, left_fmt, INK_RGB,
               46.0, 16.0, 160.0, 22.0)

    # Badge "今日"
    badge_w, badge_h = 48.0, 20.0
    badge_x, badge_y = W - 68.0, 18.0
    badge_path = _rounded_path(gdiplus, badge_x, badge_y, badge_w, badge_h, 5.0)
    gdiplus.GdipFillPath(graphics, _brush(gdiplus, 0x201E2E), badge_path)
    gdiplus.GdipDrawPath(graphics, _pen(gdiplus, 0x3B3750, 1.0), badge_path)
    gdiplus.GdipDeletePath(badge_path)
    _draw_text(gdiplus, graphics, "今日", _font(gdiplus, 10.0, FONT_BOLD, cjk=True),
               center_fmt, 0x9D99AF, badge_x, badge_y, badge_w, badge_h)

    # Divider 1
    gdiplus.GdipDrawLine(graphics, _pen(gdiplus, DIVIDER_RGB, 1.0),
                         20.0, 48.0, W - 20.0, 48.0)

    # 2. Donut Chart
    cx, cy = 76.0, 128.0
    outer_r = 45.0
    inner_r = 30.0
    donut_box = (cx - outer_r, cy - outer_r, outer_r * 2.0, outer_r * 2.0)
    in_box = (cx - inner_r, cy - inner_r, inner_r * 2.0, inner_r * 2.0)

    # Track background
    track_pen = _pen(gdiplus, 0x1F1D2B, 15.0)
    gdiplus.GdipDrawArc(graphics, track_pen,
                        ctypes.c_float(cx - 37.5), ctypes.c_float(cy - 37.5),
                        ctypes.c_float(75.0), ctypes.c_float(75.0),
                        ctypes.c_float(-90.0), ctypes.c_float(360.0))

    subtotal = sum(item[1] for item in rows)
    denominator = total or subtotal
    offset = 0.0
    elapsed_ms = max(0.0, time.perf_counter_ns() / 1000000.0 - _anim_start)

    for index, (_model, tokens, _requests) in enumerate(rows[:6]):
        fraction = float(tokens) / denominator if denominator else 0.0
        if fraction <= 0.0:
            continue
        rgb = _get_model_color(_model)
        progress = max(0.0, min(1.0, (elapsed_ms - index * STAGGER_MS) / ANIM_MS))
        eased = progress * progress * (3.0 - 2.0 * progress)
        start = -90.0 + 360.0 * offset
        sweep = 360.0 * fraction * eased
        if sweep > 0.0:
            # Draw precise SVG-style annular wedge using closed compound path
            slice_path = ctypes.c_void_p()
            if gdiplus.GdipCreatePath(0, ctypes.byref(slice_path)) == 0:
                gdiplus.GdipAddPathArc(
                    slice_path,
                    ctypes.c_float(donut_box[0]), ctypes.c_float(donut_box[1]),
                    ctypes.c_float(donut_box[2]), ctypes.c_float(donut_box[3]),
                    ctypes.c_float(start), ctypes.c_float(sweep)
                )
                gdiplus.GdipAddPathArc(
                    slice_path,
                    ctypes.c_float(in_box[0]), ctypes.c_float(in_box[1]),
                    ctypes.c_float(in_box[2]), ctypes.c_float(in_box[3]),
                    ctypes.c_float(start + sweep), ctypes.c_float(-sweep)
                )
                gdiplus.GdipClosePathFigure(slice_path)
                slice_brush = _brush(gdiplus, rgb)
                gdiplus.GdipFillPath(graphics, slice_brush, slice_path)
                gdiplus.GdipDeletePath(slice_path)
        offset += fraction

    # Donut center text
    _draw_text(gdiplus, graphics, _fmt_tokens(total), _font(gdiplus, 14.5, FONT_BOLD, cjk=True),
               center_fmt, INK_RGB, cx - outer_r, cy - 17.0, outer_r * 2.0, 20.0)
    _draw_text(gdiplus, graphics, "tokens", _font(gdiplus, 9.5, FONT_REGULAR),
               center_fmt, FAINT_RGB, cx - outer_r, cy + 3.0, outer_r * 2.0, 16.0)

    # 3. Model Rows
    f_model = _font(gdiplus, 11.5, FONT_REGULAR)
    f_sub = _font(gdiplus, 9.0, FONT_REGULAR, cjk=True)
    f_pct = _font(gdiplus, 11.0, FONT_BOLD)

    row_y = 60.0
    list_left = 152.0
    name_w = 132.0
    pct_w = 45.0
    pct_x = W - 20.0 - pct_w

    for index, (model, _tokens, _requests) in enumerate(rows[:4]):
        rgb = _get_model_color(model)
        # Dot
        gdiplus.GdipFillEllipse(graphics, _brush(gdiplus, rgb),
                                ctypes.c_float(list_left), ctypes.c_float(row_y + 4.5),
                                ctypes.c_float(7.0), ctypes.c_float(7.0))
        # Name
        safe_name = _fit_text(gdiplus, graphics, model, f_model, left_fmt, name_w)
        _draw_text(gdiplus, graphics, safe_name, f_model, left_fmt,
                   0xE1DFEA, list_left + 15.0, row_y - 1.0, name_w, 18.0)
        # Sub: tokens · requests
        tok_fmt = _fmt_tokens(_tokens)
        sub_text = "%s · %d次" % (tok_fmt, _requests)
        _draw_text(gdiplus, graphics, sub_text, f_sub, left_fmt,
                   0x757288, list_left + 15.0, row_y + 15.0, name_w, 15.0)
        # Percentage
        fraction = float(_tokens) / denominator if denominator else 0.0
        _draw_text(gdiplus, graphics, "%.1f%%" % (fraction * 100.0), f_pct,
                   right_fmt, 0xA5A1B8, pct_x, row_y + 5.0, pct_w, 18.0)
        row_y += 34.0

    if not rows:
        _draw_text(gdiplus, graphics, "暂无模型数据", _font(gdiplus, 11, FONT_REGULAR, cjk=True),
                   left_fmt, FAINT_RGB, list_left + 15.0, 100.0, 116.0, 18.0)

    # Divider 2
    gdiplus.GdipDrawLine(graphics, _pen(gdiplus, DIVIDER_RGB, 1.0),
                         20.0, 210.0, W - 20.0, 210.0)

    # 4. Footer
    f_foot_l = _font(gdiplus, 10.0, FONT_REGULAR, cjk=True)
    f_foot_r = _font(gdiplus, 10.0, FONT_BOLD, cjk=True)

    if hit_rate is not None:
        foot_left_txt = "%d 次 · 命中率 %.1f%%" % (requests, hit_rate * 100.0)
    else:
        foot_left_txt = "%d 次请求" % requests

    _draw_text(gdiplus, graphics, foot_left_txt, f_foot_l, left_fmt,
               SUB_RGB, 20.0, 224.0, 190.0, 22.0)
    _draw_text(gdiplus, graphics, "打开仪表盘 →", f_foot_r, right_fmt,
               ACCENT_RGB, W - 20.0 - 120.0, 224.0, 120.0, 22.0)

    gdiplus.GdipFlush(graphics, 1)


def _paint_window(hwnd):
    """Render ARGB GDI+ into a premultiplied DIB for UpdateLayeredWindow."""
    user32, _kernel32, gdi32, gdiplus, _dwmapi, _post_user32 = _dlls()
    screen_dc = None
    mem_dc = None
    hbitmap = None
    bits = ctypes.c_void_p()
    old_bitmap = None
    graphics = ctypes.c_void_p()
    gp_bitmap = ctypes.c_void_p()
    try:
        screen_dc = user32.GetDC(None)
        mem_dc = gdi32.CreateCompatibleDC(screen_dc)
        user32.ReleaseDC(None, screen_dc)
        screen_dc = None

        bmi = _BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = W
        bmi.bmiHeader.biHeight = -H  # top-down
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = 0  # BI_RGB
        hbitmap = gdi32.CreateDIBSection(mem_dc, ctypes.byref(bmi), 0,
                                         ctypes.byref(bits), None, 0)
        if not hbitmap or not bits:
            raise OSError("CreateDIBSection failed")
        old_bitmap = gdi32.SelectObject(mem_dc, hbitmap)

        # Let GDI+ render straight-alpha ARGB directly into the DIB.
        if gdiplus.GdipCreateBitmapFromScan0(W, H, W * 4, PIXEL_FORMAT_32BPP_ARGB,
                                             bits, ctypes.byref(gp_bitmap)) != 0:
            raise OSError("GdipCreateBitmapFromScan0 failed")
        if gdiplus.GdipGetImageGraphicsContext(gp_bitmap, ctypes.byref(graphics)) != 0:
            raise OSError("GdipGetImageGraphicsContext failed")
        _draw_card(gdiplus, graphics)
        gdiplus.GdipFlush(graphics, 1)

        # Release GDI+ before mutating the external DIB memory.
        gdiplus.GdipDeleteGraphics(graphics)
        graphics = ctypes.c_void_p()
        gdiplus.GdipDisposeImage(gp_bitmap)
        gp_bitmap = ctypes.c_void_p()

        # Convert straight-alpha BGRA to the premultiplied BGRA required by
        # UpdateLayeredWindow.  Opaque card pixels are unchanged.
        pixels = ctypes.cast(bits, ctypes.POINTER(ctypes.c_ubyte))
        for offset in range(0, W * H * 4, 4):
            alpha = pixels[offset + 3]
            if alpha == 255 or alpha == 0:
                continue
            pixels[offset] = pixels[offset] * alpha // 255
            pixels[offset + 1] = pixels[offset + 1] * alpha // 255
            pixels[offset + 2] = pixels[offset + 2] * alpha // 255

        dst = _POINT(_POS[0], _POS[1])
        src = _POINT(0, 0)
        size = _SIZE(W, H)
        blend = _BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
        if not user32.UpdateLayeredWindow(hwnd, None, ctypes.byref(dst),
                                          ctypes.byref(size), mem_dc,
                                          ctypes.byref(src), 0,
                                          ctypes.byref(blend), ULW_ALPHA):
            error = ctypes.get_last_error()
            raise OSError(f"UpdateLayeredWindow failed ({error})")
    finally:
        if graphics:
            gdiplus.GdipDeleteGraphics(graphics)
        if gp_bitmap:
            gdiplus.GdipDisposeImage(gp_bitmap)
        if old_bitmap and mem_dc:
            gdi32.SelectObject(mem_dc, old_bitmap)
        if hbitmap:
            gdi32.DeleteObject(hbitmap)
        if mem_dc:
            gdi32.DeleteDC(mem_dc)


def _cursor_in_rect(x, y, left, top, right, bottom, margin=0):
    return left - margin <= x <= right + margin and top - margin <= y <= bottom + margin


def _still_wanted():
    if not _hwnd or not _anchor:
        return False
    user32 = _dlls()[0]
    point = _POINT()
    if not user32.GetCursorPos(ctypes.byref(point)):
        return False
    rect = _RECT()
    if user32.GetWindowRect(_hwnd, ctypes.byref(rect)):
        if _cursor_in_rect(point.x, point.y, rect.left, rect.top, rect.right, rect.bottom, 10):
            return True
    anchor_x, anchor_y = _anchor
    return abs(point.x - anchor_x) <= ANCHOR_RADIUS and abs(point.y - anchor_y) <= ANCHOR_RADIUS


def _hide(user32):
    global _mouse_tracked, _anchor
    if not _hwnd:
        return
    user32.KillTimer(_hwnd, ctypes.c_void_p(1))
    user32.KillTimer(_hwnd, ctypes.c_void_p(2))
    user32.ShowWindow(_hwnd, SW_HIDE)
    _mouse_tracked = False
    _anchor = None


def _show_at(user32, x, y):
    global _anim_start, _anchor
    screen_w = user32.GetSystemMetrics(SM_CXSCREEN)
    screen_h = user32.GetSystemMetrics(SM_CYSCREEN)
    left = min(max(8, x - W + 42), screen_w - W - 8)
    top = max(8, y - H - 18)
    _POS[0] = left
    _POS[1] = top
    _anchor = (x, y)
    _anim_start = time.perf_counter_ns() / 1000000.0
    user32.SetWindowPos(_hwnd, ctypes.c_void_p(HWND_TOPMOST), left, top, W, H,
                        SWP_NOACTIVATE | SWP_SHOWWINDOW)
    _paint_window(_hwnd)
    user32.SetTimer(_hwnd, ctypes.c_void_p(1), FRAME_MS, None)
    user32.SetTimer(_hwnd, ctypes.c_void_p(2), CHECK_MS, None)


def show(x, y):
    """Called by tray_icon after the 500 ms hover debounce."""
    hwnd = _hwnd
    if not hwnd or not _READY.wait(0.15):
        return False
    try:
        post_user32 = _dlls()[5]
        packed = (max(0, min(32767, int(y))) << 16) | max(0, min(65535, int(x) & 0xFFFF))
        return bool(post_user32.PostMessageW(hwnd, WM_APP_SHOW, ctypes.c_void_p(0),
                                             ctypes.c_void_p(packed)))
    except Exception as e:
        log.debug("overlay show failed: %s", e)
        return False


def _wndproc(hwnd, msg, wp, lp):
    global _mouse_tracked
    user32 = _dlls()[0]
    if msg == WM_APP_SHOW:
        try:
            packed = int(lp or 0)
            _show_at(user32, packed & 0xFFFF, (packed >> 16) & 0xFFFF)
        except Exception as e:
            log.warning("overlay show failed: %s", e)
        return 0
    if msg == WM_PAINT:
        try:
            _paint_window(hwnd)
        except Exception as e:
            log.warning("overlay paint failed: %s", e)
        user32.ValidateRect(hwnd, None)
        return 0
    if msg == WM_ERASEBKGND:
        return 1
    if msg == WM_TIMER:
        timer_id = int(wp or 0)
        if timer_id == 1:
            if time.perf_counter_ns() / 1000000.0 - _anim_start <= ANIM_MS + STAGGER_MS * 6:
                try:
                    _paint_window(hwnd)
                except Exception as e:
                    log.warning("overlay animation stopped: %s", e)
                    _hide(user32)
            else:
                user32.KillTimer(hwnd, ctypes.c_void_p(1))
                try:
                    _paint_window(hwnd)
                except Exception:
                    pass
        elif timer_id == 2 and not _still_wanted():
            _hide(user32)
        return 0
    if msg == WM_NCHITTEST:
        return HTCLIENT
    if msg == WM_SETCURSOR and int(lp or 0) & 0xFFFF == HTCLIENT:
        cursor = user32.LoadCursorW(None, ctypes.c_void_p(IDC_HAND))
        user32.SetCursor(cursor)
        return 1
    if msg == WM_LBUTTONUP and _url:
        try:
            os.startfile(_url)
        except Exception as e:
            log.debug("overlay dashboard launch failed: %s", e)
        _hide(user32)
        return 0
    if msg == WM_MOUSEMOVE and not _mouse_tracked:
        track = _TRACKMOUSEEVENT(ctypes.sizeof(_TRACKMOUSEEVENT), TME_LEAVE, hwnd, 0)
        if user32.TrackMouseEvent(ctypes.byref(track)):
            _mouse_tracked = True
        return 0
    if msg == WM_MOUSELEAVE:
        if not _still_wanted():
            _hide(user32)
        _mouse_tracked = False
        return 0
    if msg == WM_CLOSE:
        user32.ShowWindow(hwnd, SW_HIDE)
        return 0
    return user32.DefWindowProcW(hwnd, msg, wp, lp)


def _run():
    global _hwnd, _proc
    gdiplus = None
    try:
        user32, kernel32, gdi32, gdiplus, dwmapi, _post_user32 = _dlls()
        token = ctypes.c_void_p()
        startup = _GdiplusStartupInput(1, None, 0, 0)
        if gdiplus.GdiplusStartup(ctypes.byref(token), ctypes.byref(startup), None) != 0:
            raise OSError("GdiplusStartup failed")
        globals()["_TOKEN"] = token

        _proc = ctypes.WINFUNCTYPE(ctypes.c_longlong, ctypes.c_void_p, ctypes.c_uint,
                                   ctypes.c_void_p, ctypes.c_void_p)(_wndproc)
        instance = kernel32.GetModuleHandleW(None)
        cursor = user32.LoadCursorW(None, ctypes.c_void_p(IDC_ARROW))
        cls = _WNDCLASSW()
        cls.style = CS_HREDRAW | CS_VREDRAW
        cls.lpfnWndProc = ctypes.cast(_proc, ctypes.c_void_p)
        cls.hInstance = instance
        cls.hCursor = cursor
        cls.lpszClassName = "CodexBridgeUsageCard"
        if not user32.RegisterClassW(ctypes.byref(cls)):
            raise OSError("RegisterClassW(overlay) failed")

        hwnd = user32.CreateWindowExW(EX_STYLE, "CodexBridgeUsageCard",
                                      "Codex Bridge Usage", WS_POPUP,
                                      0, 0, W, H, None, None, instance, None)
        if not hwnd:
            raise OSError("CreateWindowExW(overlay) failed")
        _hwnd = hwnd

        _READY.set()

        queue = _MSG()
        while user32.GetMessageW(ctypes.byref(queue), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(queue))
            user32.DispatchMessageW(ctypes.byref(queue))
    except Exception as e:
        log.warning("usage card unavailable: %s", e)
    finally:
        _READY.clear()
        globals()["_hwnd"] = None
        token = globals().get("_TOKEN")
        if token and gdiplus:
            try:
                gdiplus.GdiplusShutdown(token)
            except Exception:
                pass


def start(url):
    global _THREAD, _url
    if os.name != "nt":
        return
    _url = str(url)
    with _THREAD_LOCK:
        if _THREAD is not None and _THREAD.is_alive():
            return
        _THREAD = threading.Thread(target=_run, daemon=True, name="tray-usage-card")
        _THREAD.start()


def stop():
    hwnd = _hwnd
    if hwnd:
        try:
            user32 = _dlls()[0]
            user32.PostMessageW(hwnd, WM_CLOSE, ctypes.c_void_p(0), ctypes.c_void_p(0))
        except Exception:
            pass
