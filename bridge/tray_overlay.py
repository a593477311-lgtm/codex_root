"""Animated usage popover for the Codex Bridge tray icon.

This is the "Plan A" implementation: a small topmost window drawn directly
with GDI+ through ctypes.  It deliberately avoids pywebview/WebView2 and any
network/window framework dependency.  The tray module only asks this module to
show a window and to refresh usage rows; every failure is contained here and
never takes the bridge or the basic tray icon down.

The donut deliberately copies the dashboard look: rounded charcoal card,
the same vendor palette, total tokens in the middle, and a short staggered
sweep animation when the card appears.
"""

import ctypes
import logging
import os
import threading
import time


log = logging.getLogger("kimi_bridge.tray-overlay")

WM_NULL = 0x0000
WM_CLOSE = 0x0010
WM_PAINT = 0x000F
WM_TIMER = 0x0113
WM_LBUTTONUP = 0x0202
WM_MOUSEMOVE = 0x0200
WM_MOUSELEAVE = 0x02A2
WM_NCHITTEST = 0x0084
WM_SETCURSOR = 0x0020
WM_APP_SHOW = 0x8001

HTCLIENT = 1
HTTRANSPARENT = -1

CS_HREDRAW = 0x0002
CS_VREDRAW = 0x0001
WS_POPUP = 0x80000000
WS_VISIBLE = 0x10000000

# TOPMOST | NOACTIVATE | TOOLWINDOW | LAYERED
EX_STYLE = 0x00000008 | 0x08000000 | 0x00000080 | 0x00080000

SW_HIDE = 0
SW_SHOWNOACTIVATE = 4
HWND_TOPMOST = -1
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040

IDC_ARROW = 32512
IDC_HAND = 32649
IMAGE_CURSOR = 2

SM_CXSCREEN = 0
SM_CYSCREEN = 1

ULW_ALPHA = 2
AC_SRC_OVER = 0
AC_SRC_ALPHA = 1

UNIT_PIXEL = 2
FONT_REGULAR = 0
FONT_BOLD = 1
SMOOTHING_ANTIALIAS = 4
TEXT_ANTIALIAS_GRIDFIT = 4
PIXEL_FORMAT_32BPP_ARGB = 0x0026200A
STRING_ALIGNMENT_CENTER = 1

TME_LEAVE = 0x00000002

# Layout / animation constants.
W = 288
H = 222
ANIM_MS = 620
STAGGER_MS = 75
FRAME_MS = 16
CHECK_MS = 110
ANCHOR_RADIUS = 46

# dashboard palette: --v1 / green / cyan / amber / red / --v2 / ...
PALETTE = (
    0xFF8B6CF7,
    0xFF39D353,
    0xFF2BC8E6,
    0xFFF5A623,
    0xFFF26D6A,
    0xFFA98BFA,
    0xFF8BC34A,
    0xFFF472B6,
)


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _SIZE(ctypes.Structure):
    _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]


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


class _BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_byte), ("BlendFlags", ctypes.c_byte),
                ("SourceConstantAlpha", ctypes.c_byte), ("AlphaFormat", ctypes.c_byte)]


class _GdiplusStartupInput(ctypes.Structure):
    _fields_ = [("GdiplusVersion", ctypes.c_uint),
                ("DebugEventCallback", ctypes.c_void_p),
                ("SuppressBackgroundThread", ctypes.c_int),
                ("SuppressExternalCodecs", ctypes.c_int)]


class _RECTF(ctypes.Structure):
    _fields_ = [("x", ctypes.c_float), ("y", ctypes.c_float),
                ("width", ctypes.c_float), ("height", ctypes.c_float)]


_DLLS = None
_TOKEN = None
_STATE = {
    "rows": [],
    "total": 0,
    "requests": 0,
    "title": "今日模型用量",
    "updated": 0.0,
}
_LOCK = threading.Lock()
_THREAD_LOCK = threading.Lock()
_THREAD = None
_READY = threading.Event()

# Populated on the overlay thread.
_hwnd = None
_gdiplus = None
_url = ""
_shown_at = 0.0
_anim_start = 0
_anchor = None
_mouse_tracked = False
_proc = None
_class_atom = 0


def _dlls():
    """Bind all Win64-sensitive APIs once; mixed handles are the usual bug."""
    global _DLLS
    if _DLLS is not None:
        return _DLLS

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    gdi32 = ctypes.windll.gdi32
    gdiplus = ctypes.windll.gdiplus

    # Window/message APIs.
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
    user32.DestroyWindow.argtypes = [ctypes.c_void_p]
    user32.DestroyWindow.restype = ctypes.c_int
    user32.GetMessageW.argtypes = [ctypes.POINTER(_MSG), ctypes.c_void_p,
                                   ctypes.c_uint, ctypes.c_uint]
    user32.GetMessageW.restype = ctypes.c_int
    user32.TranslateMessage.argtypes = [ctypes.POINTER(_MSG)]
    user32.TranslateMessage.restype = ctypes.c_int
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(_MSG)]
    user32.DispatchMessageW.restype = ctypes.c_longlong
    user32.PostQuitMessage.argtypes = [ctypes.c_int]
    user32.PostQuitMessage.restype = None
    user32.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
    user32.SetWindowPos.restype = ctypes.c_int
    user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
    user32.ShowWindow.restype = ctypes.c_int
    user32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(_POINT * 2)]
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
    user32.UpdateLayeredWindow.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                           ctypes.POINTER(_POINT), ctypes.POINTER(_SIZE),
                                           ctypes.c_void_p, ctypes.POINTER(_POINT),
                                           ctypes.c_uint, ctypes.POINTER(_BLENDFUNCTION),
                                           ctypes.c_uint]
    user32.UpdateLayeredWindow.restype = ctypes.c_int
    user32.GetDC.argtypes = [ctypes.c_void_p]
    user32.GetDC.restype = ctypes.c_void_p
    user32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    user32.ReleaseDC.restype = ctypes.c_int
    user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    user32.GetSystemMetrics.restype = ctypes.c_int
    user32.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                    ctypes.c_void_p, ctypes.c_void_p]
    user32.PostMessageW.restype = ctypes.c_int
    kernel32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
    kernel32.GetModuleHandleW.restype = ctypes.c_void_p

    # Bitblt / bitmap ownership during UpdateLayeredWindow.
    gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
    gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
    gdi32.DeleteDC.argtypes = [ctypes.c_void_p]
    gdi32.DeleteDC.restype = ctypes.c_int
    gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    gdi32.SelectObject.restype = ctypes.c_void_p
    gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
    gdi32.DeleteObject.restype = ctypes.c_int

    # GDI+ object APIs.  All opaque handles stay c_void_p.
    gdiplus.GdiplusStartup.argtypes = [ctypes.POINTER(ctypes.c_void_p),
                                       ctypes.POINTER(_GdiplusStartupInput), ctypes.c_void_p]
    gdiplus.GdiplusStartup.restype = ctypes.c_int
    gdiplus.GdiplusShutdown.argtypes = [ctypes.c_void_p]
    gdiplus.GdiplusShutdown.restype = None
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
    gdiplus.GdipSetSmoothingMode.argtypes = [ctypes.c_void_p, ctypes.c_int]
    gdiplus.GdipSetSmoothingMode.restype = ctypes.c_int
    gdiplus.GdipSetTextRenderingHint.argtypes = [ctypes.c_void_p, ctypes.c_int]
    gdiplus.GdipSetTextRenderingHint.restype = ctypes.c_int
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
    gdiplus.GdipFillPath.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    gdiplus.GdipFillPath.restype = ctypes.c_int
    gdiplus.GdipDrawPath.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    gdiplus.GdipDrawPath.restype = ctypes.c_int
    gdiplus.GdipDrawArc.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                    ctypes.c_float, ctypes.c_float,
                                    ctypes.c_float, ctypes.c_float,
                                    ctypes.c_float, ctypes.c_float]
    gdiplus.GdipDrawArc.restype = ctypes.c_int
    gdiplus.GdipDrawLine.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                     ctypes.c_float, ctypes.c_float,
                                     ctypes.c_float, ctypes.c_float]
    gdiplus.GdipDrawLine.restype = ctypes.c_int
    gdiplus.GdipCreateFontFamilyFromName.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p,
                                                     ctypes.POINTER(ctypes.c_void_p)]
    gdiplus.GdipCreateFontFamilyFromName.restype = ctypes.c_int
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
    gdiplus.GdipDrawString.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int,
                                       ctypes.c_void_p, ctypes.POINTER(_RECTF),
                                       ctypes.c_void_p, ctypes.c_void_p]
    gdiplus.GdipDrawString.restype = ctypes.c_int
    gdiplus.GdipCreateHBITMAPFromBitmap.argtypes = [ctypes.c_void_p,
                                                    ctypes.POINTER(ctypes.c_void_p),
                                                    ctypes.c_uint]
    gdiplus.GdipCreateHBITMAPFromBitmap.restype = ctypes.c_int

    _DLLS = (user32, kernel32, gdi32, gdiplus)
    return _DLLS


def _set_usage_locked(rows, total, requests, title):
    clean = []
    for r in rows or []:
        model = str(r.get("model") or "?")
        if model == "?":
            continue
        clean.append((model, int(r.get("tokens_total") or 0),
                      int(r.get("requests") or 0)))
    clean.sort(key=lambda x: x[1], reverse=True)
    _STATE["rows"] = clean[:6]
    _STATE["total"] = int(total or 0)
    _STATE["requests"] = int(requests or 0)
    _STATE["title"] = str(title or "今日模型用量")
    _STATE["updated"] = time.time()


def set_usage(rows, total, requests, title="今日模型用量"):
    """Thread-safe usage cache; safe before/after the overlay has started."""
    try:
        with _LOCK:
            _set_usage_locked(rows, total, requests, title)
        hwnd = _hwnd
        if hwnd and _READY.is_set():
            user32 = _dlls()[0]
            user32.PostMessageW(hwnd, WM_NULL, None, None)
    except Exception as e:
        log.debug("overlay set_usage failed: %s", e)


def _argb(hex_color, alpha=255):
    r = (hex_color >> 16) & 255
    g = (hex_color >> 8) & 255
    b = hex_color & 255
    return (int(alpha) << 24) | (r << 16) | (g << 8) | b


def _fmt_tokens(n):
    n = int(n or 0)
    if n >= 100000000:
        s = "%.2f" % (n / 100000000.0)
    elif n >= 10000:
        s = "%.1f" % (n / 10000.0)
    else:
        return str(n)
    if s.endswith("0"):
        s = s[:-2]
    return s + (" 亿" if n >= 100000000 else " 万")


def _rounded_path(gdiplus, x, y, w, h, radius):
    path = ctypes.c_void_p()
    if gdiplus.GdipCreatePath(0, ctypes.byref(path)) != 0:
        raise OSError("GdipCreatePath failed")
    d = 2.0 * radius
    gdiplus.GdipAddPathArc(path, x, y, d, d, 180.0, 90.0)
    gdiplus.GdipAddPathArc(path, x + w - d, y, d, d, 270.0, 90.0)
    gdiplus.GdipAddPathArc(path, x + w - d, y + h - d, d, d, 0.0, 90.0)
    gdiplus.GdipAddPathArc(path, x, y + h - d, d, d, 90.0, 90.0)
    gdiplus.GdipClosePathFigure(path)
    return path


class _Brush:
    def __init__(self, gdiplus, color):
        self.p = ctypes.c_void_p()
        if gdiplus.GdipCreateSolidFill(color, ctypes.byref(self.p)) != 0:
            raise OSError("GdipCreateSolidFill failed")

    def close(self, gdiplus):
        if self.p:
            gdiplus.GdipDeleteBrush(self.p)
            self.p = ctypes.c_void_p()


class _Pen:
    def __init__(self, gdiplus, color, width):
        self.p = ctypes.c_void_p()
        if gdiplus.GdipCreatePen1(color, float(width), UNIT_PIXEL,
                                  ctypes.byref(self.p)) != 0:
            raise OSError("GdipCreatePen1 failed")

    def close(self, gdiplus):
        if self.p:
            gdiplus.GdipDeletePen(self.p)
            self.p = ctypes.c_void_p()


class _Font:
    def __init__(self, gdiplus, size, bold=False):
        family = ctypes.c_void_p()
        status = gdiplus.GdipCreateFontFamilyFromName("Microsoft YaHei UI", None,
                                                      ctypes.byref(family))
        if status != 0:
            status = gdiplus.GdipCreateFontFamilyFromName("Microsoft YaHei", None,
                                                          ctypes.byref(family))
        if status != 0:
            gdiplus.GdipGetGenericFontFamilySansSerif(ctypes.byref(family))
        self.family = family
        self.p = ctypes.c_void_p()
        if gdiplus.GdipCreateFont(family, float(size), FONT_BOLD if bold else FONT_REGULAR,
                                  UNIT_PIXEL, ctypes.byref(self.p)) != 0:
            raise OSError("GdipCreateFont failed")

    def close(self, gdiplus):
        if self.p:
            gdiplus.GdipDeleteFont(self.p)
            self.p = ctypes.c_void_p()
        if self.family:
            gdiplus.GdipDeleteFontFamily(self.family)
            self.family = ctypes.c_void_p()


class _Format:
    def __init__(self, gdiplus, align=0):
        self.p = ctypes.c_void_p()
        if gdiplus.GdipCreateStringFormat(0, 0, ctypes.byref(self.p)) != 0:
            raise OSError("GdipCreateStringFormat failed")
        if align == 1:
            gdiplus.GdipSetStringFormatAlign(self.p, STRING_ALIGNMENT_CENTER)
            gdiplus.GdipSetStringFormatLineAlign(self.p, STRING_ALIGNMENT_CENTER)
        elif align == 2:
            gdiplus.GdipSetStringFormatAlign(self.p, 2)  # StringAlignmentFar/right

    def close(self, gdiplus):
        if self.p:
            gdiplus.GdipDeleteStringFormat(self.p)
            self.p = ctypes.c_void_p()


def _draw_text(gdiplus, graphics, brush, font, fmt, text, x, y, w, h):
    rect = _RECTF(float(x), float(y), float(w), float(h))
    gdiplus.GdipDrawString(graphics, str(text), len(str(text)), font.p,
                           ctypes.byref(rect), fmt.p, brush.p)


def _draw_card(gdiplus, graphics):
    with _LOCK:
        rows = list(_STATE["rows"])
        total = int(_STATE["total"])
        requests = int(_STATE["requests"])
        title = _STATE["title"]

    brushes = []
    pens = []
    fonts = []
    formats = []
    path = None
    try:
        bg = _Brush(gdiplus, _argb(0x14131A, 244))
        border = _Brush(gdiplus, _argb(0x2A2933, 255))
        ink = _Brush(gdiplus, _argb(0xE8E7ED))
        sub = _Brush(gdiplus, _argb(0x9A99A5))
        faint = _Brush(gdiplus, _argb(0x63626E))
        bg_pen = _Pen(gdiplus, _argb(0x33323E), 1.0)
        ring_pen = _Pen(gdiplus, _argb(0x26252E), 17.0)
        brushes = [bg, border, ink, sub, faint]
        pens = [bg_pen, ring_pen]
        title_font = _Font(gdiplus, 17, True)
        value_font = _Font(gdiplus, 15, True)
        label_font = _Font(gdiplus, 10)
        row_font = _Font(gdiplus, 11)
        fonts = [title_font, value_font, label_font, row_font]
        center_fmt = _Format(gdiplus, 1)
        left_fmt = _Format(gdiplus)
        right_fmt = _Format(gdiplus, 2)
        formats = [center_fmt, left_fmt, right_fmt]

        path = _rounded_path(gdiplus, 0.5, 0.5, W - 1.0, H - 1.0, 22.0)
        gdiplus.GdipFillPath(bg.p, path)
        gdiplus.GdipDrawPath(bg_pen.p, path)

        # Top title + compact live dot.  This reads as the dashboard card,
        # not as a replacement for the ordinary tray tooltip.
        _draw_text(gdiplus, graphics, ink, title_font, left_fmt, title, 20, 18, 174, 26)
        dot = _Brush(gdiplus, _argb(0x39D353))
        brushes.append(dot)
        dot_path = _rounded_path(gdiplus, 181.0, 26.0, 8.0, 8.0, 4.0)
        gdiplus.GdipFillPath(dot.p, dot_path)
        gdiplus.GdipDeletePath(dot_path)

        donut_x, donut_y, donut_w = 23.0, 65.0, 104.0
        gdiplus.GdipDrawArc(graphics, ring_pen.p, donut_x, donut_y, donut_w, donut_w,
                            -90.0, 360.0)

        subtotal = sum(r[1] for r in rows)
        use_total = total or subtotal
        off = 0.0
        elapsed_ms = int(max(0.0, (time.perf_counter_ns() / 1000000.0) - _anim_start))
        for i, (_model, tokens, _requests) in enumerate(rows[:6]):
            frac = float(tokens) / use_total if use_total else 0.0
            delay = i * STAGGER_MS
            p = max(0.0, min(1.0, float(elapsed_ms - delay) / ANIM_MS))
            # cubic-bezier(.2,.8,.2,1) approximation; just enough to match the
            # dashboard feel while keeping the GDI+ layer dependency-free.
            eased = p * p * (3.0 - 2.0 * p)
            if frac <= 0.0:
                continue
            seg_pen = _Pen(gdiplus, PALETTE[i % len(PALETTE)], 17.0)
            pens.append(seg_pen)
            start = -90.0 + 360.0 * off
            sweep = max(1.0, 360.0 * frac * eased)
            gdiplus.GdipDrawArc(graphics, seg_pen.p, donut_x, donut_y,
                                donut_w, donut_w, start, sweep)
            off += frac

        _draw_text(gdiplus, graphics, ink, value_font, center_fmt,
                   _fmt_tokens(total), donut_x, donut_y + 38.0, donut_w, 20)
        _draw_text(gdiplus, graphics, faint, label_font, center_fmt,
                   "tokens", donut_x, donut_y + 58.0, donut_w, 16)

        if rows:
            y = 68.0
            for i, (model, tokens, reqs) in enumerate(rows[:4]):
                brush = _Brush(gdiplus, PALETTE[i % len(PALETTE)])
                brushes.append(brush)
                dot_path = _rounded_path(gdiplus, 142.0, y + 5.0, 8.0, 8.0, 4.0)
                gdiplus.GdipFillPath(brush.p, dot_path)
                gdiplus.GdipDeletePath(dot_path)
                pct = (100.0 * tokens / use_total) if use_total else 0.0
                _draw_text(gdiplus, graphics, sub, row_font, left_fmt,
                           model, 158, y, 90, 17)
                _draw_text(gdiplus, graphics, sub, row_font, right_fmt,
                           "%.1f%%" % pct, 238, y, 32, 17)
                y += 22.0
            if len(rows) > 4:
                _draw_text(gdiplus, graphics, faint, label_font, left_fmt,
                           "+%d 个模型" % (len(rows) - 4), 158, y + 1, 104, 15)
            _draw_text(gdiplus, graphics, ink, label_font, left_fmt,
                       "%s 次 · %s" % (requests, _fmt_tokens(tokens if len(rows) == 1 else total)),
                       142, 168, 128, 18)
        else:
            _draw_text(gdiplus, graphics, faint, row_font, center_fmt,
                       "暂无模型数据", 138, 104, 132, 18)

        _draw_text(gdiplus, graphics, faint, label_font, left_fmt,
                   "点击打开仪表盘", 20, 178, 118, 16)
    finally:
        if path:
            gdiplus.GdipDeletePath(path)
        for x in fonts:
            x.close(gdiplus)
        for x in formats:
            x.close(gdiplus)
        for x in pens:
            x.close(gdiplus)
        for x in brushes:
            x.close(gdiplus)


def _paint_layered(hwnd):
    """Render an ARGB bitmap and hand it to UpdateLayeredWindow."""
    user32, _kernel32, gdi32, gdiplus = _dlls()
    bitmap = ctypes.c_void_p()
    graphics = ctypes.c_void_p()
    hbitmap = ctypes.c_void_p()
    hdc_mem = None
    old = None
    try:
        if gdiplus.GdipCreateBitmapFromScan0(W, H, 0, PIXEL_FORMAT_32BPP_ARGB,
                                             None, ctypes.byref(bitmap)) != 0:
            raise OSError("GdipCreateBitmapFromScan0 failed")
        if gdiplus.GdipGetImageGraphicsContext(bitmap, ctypes.byref(graphics)) != 0:
            raise OSError("GdipGetImageGraphicsContext failed")
        gdiplus.GdipSetSmoothingMode(graphics, SMOOTHING_ANTIALIAS)
        gdiplus.GdipSetTextRenderingHint(graphics, TEXT_ANTIALIAS_GRIDFIT)
        _draw_card(gdiplus, graphics)
        if gdiplus.GdipCreateHBITMAPFromBitmap(bitmap, ctypes.byref(hbitmap), 0) != 0:
            raise OSError("GdipCreateHBITMAPFromBitmap failed")

        hdc_screen = user32.GetDC(hwnd)
        hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
        user32.ReleaseDC(hwnd, hdc_screen)
        old = gdi32.SelectObject(hdc_mem, hbitmap)

        dst = _POINT(_STATE.get("_x", 0), _STATE.get("_y", 0))
        src = _POINT(0, 0)
        size = _SIZE(W, H)
        blend = _BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
        if not user32.UpdateLayeredWindow(hwnd, None, ctypes.byref(dst),
                                          ctypes.byref(size), hdc_mem,
                                          ctypes.byref(src), 0,
                                          ctypes.byref(blend), ULW_ALPHA):
            raise OSError("UpdateLayeredWindow failed")
    finally:
        if old and hdc_mem:
            gdi32.SelectObject(hdc_mem, old)
        if hdc_mem:
            gdi32.DeleteDC(hdc_mem)
        if hbitmap:
            gdi32.DeleteObject(hbitmap)
        if graphics:
            gdiplus.GdipDeleteGraphics(graphics)
        if bitmap:
            gdiplus.GdipDisposeImage(bitmap)


def _cursor_in_rect(x, y, left, top, right, bottom, margin=0):
    return left - margin <= x <= right + margin and top - margin <= y <= bottom + margin


def _still_wanted():
    """Keep the card while the cursor is over it or back on the tray icon."""
    if not _hwnd or not _anchor:
        return False
    user32 = _dlls()[0]
    pt = _POINT()
    if not user32.GetCursorPos(ctypes.byref(pt)):
        return False
    rect = (_POINT * 2)()
    if user32.GetWindowRect(_hwnd, ctypes.byref(rect)):
        left, top = rect[0].x, rect[0].y
        right, bottom = rect[1].x, rect[1].y
        if _cursor_in_rect(pt.x, pt.y, left, top, right, bottom, 10):
            return True
    ax, ay = _anchor
    return abs(pt.x - ax) <= ANCHOR_RADIUS and abs(pt.y - ay) <= ANCHOR_RADIUS


def _hide(user32):
    global _mouse_tracked, _anchor
    user32.KillTimer(_hwnd, ctypes.c_void_p(1))
    user32.KillTimer(_hwnd, ctypes.c_void_p(2))
    user32.ShowWindow(_hwnd, SW_HIDE)
    _mouse_tracked = False
    _anchor = None


def _show_at(user32, x, y):
    global _shown_at, _anim_start, _anchor
    screen_w = user32.GetSystemMetrics(SM_CXSCREEN)
    screen_h = user32.GetSystemMetrics(SM_CYSCREEN)
    left = min(max(8, x - W + 36), screen_w - W - 8)
    top = max(8, y - H - 18)
    _STATE["_x"] = left
    _STATE["_y"] = top
    _anchor = (x, y)
    _anim_start = time.perf_counter_ns() / 1000000.0
    _shown_at = time.time()
    user32.SetWindowPos(_hwnd, ctypes.c_void_p(HWND_TOPMOST), left, top, W, H,
                        SWP_NOACTIVATE | SWP_SHOWWINDOW)
    _paint_layered(_hwnd)
    user32.SetTimer(_hwnd, ctypes.c_void_p(1), FRAME_MS, None)
    user32.SetTimer(_hwnd, ctypes.c_void_p(2), CHECK_MS, None)


def show(x, y):
    """Public entry called by the tray after its 500 ms hover debounce."""
    hwnd = _hwnd
    if not hwnd or not _READY.wait(0.15):
        return False
    try:
        user32 = _dlls()[0]
        # Marshal the coordinates into the overlay's message loop.  All
        # painting/timer ownership therefore stays on one thread.
        lp = (max(0, min(32767, int(y))) << 16) | max(0, min(65535, int(x) & 0xFFFF))
        return bool(user32.PostMessageW(hwnd, WM_APP_SHOW, None, ctypes.c_void_p(lp)))
    except Exception as e:
        log.debug("overlay show failed: %s", e)
        return False


def _wndproc(hwnd, msg, wp, lp):
    global _mouse_tracked
    user32 = _dlls()[0]
    if msg == WM_APP_SHOW:
        try:
            packed = int(lp or 0)
            x = packed & 0xFFFF
            y = (packed >> 16) & 0xFFFF
            _show_at(user32, x, y)
        except Exception as e:
            log.warning("overlay show failed: %s", e)
        return 0
    if msg == WM_TIMER:
        tid = (wp or 0)
        if tid == 1:
            if time.perf_counter_ns() / 1000000.0 - _anim_start <= ANIM_MS + STAGGER_MS * 6:
                try:
                    _paint_layered(hwnd)
                except Exception as e:
                    log.warning("overlay animation stopped: %s", e)
                    _hide(user32)
            else:
                user32.KillTimer(hwnd, ctypes.c_void_p(1))
                try:
                    _paint_layered(hwnd)
                except Exception:
                    pass
        elif tid == 2 and not _still_wanted():
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
        tme = _TRACKMOUSEEVENT(ctypes.sizeof(_TRACKMOUSEEVENT), TME_LEAVE, hwnd, 0)
        if user32.TrackMouseEvent(ctypes.byref(tme)):
            _mouse_tracked = True
        return 0
    if msg == WM_MOUSELEAVE:
        # The checker timer keeps the card if the cursor crosses back to the
        # tray icon; otherwise close immediately for a normal popover feel.
        if not _still_wanted():
            _hide(user32)
        _mouse_tracked = False
        return 0
    if msg in (WM_CLOSE,):
        user32.ShowWindow(hwnd, SW_HIDE)
        return 0
    return user32.DefWindowProcW(hwnd, msg, wp, lp)


def _run():
    global _hwnd, _proc, _class_atom
    try:
        user32, kernel32, _gdi32, gdiplus = _dlls()
        token = ctypes.c_void_p()
        startup = _GdiplusStartupInput(1, None, 0, 0)
        if gdiplus.GdiplusStartup(ctypes.byref(token), ctypes.byref(startup), None) != 0:
            raise OSError("GdiplusStartup failed")
        globals()["_TOKEN"] = token

        _proc = ctypes.WINFUNCTYPE(ctypes.c_longlong, ctypes.c_void_p, ctypes.c_uint,
                                   ctypes.c_void_p, ctypes.c_void_p)(_wndproc)
        h_inst = kernel32.GetModuleHandleW(None)
        cursor = user32.LoadCursorW(None, ctypes.c_void_p(IDC_ARROW))
        cls = _WNDCLASSW()
        cls.style = CS_HREDRAW | CS_VREDRAW
        cls.lpfnWndProc = ctypes.cast(_proc, ctypes.c_void_p)
        cls.hInstance = h_inst
        cls.hCursor = cursor
        cls.lpszClassName = "CodexBridgeUsageOverlay"
        atom = user32.RegisterClassW(ctypes.byref(cls))
        if not atom:
            raise OSError("RegisterClassW(overlay) failed")
        _class_atom = atom
        hwnd = user32.CreateWindowExW(EX_STYLE, "CodexBridgeUsageOverlay",
                                      "Codex Bridge 用量", WS_POPUP,
                                      0, 0, W, H, None, None, h_inst, None)
        if not hwnd:
            raise OSError("CreateWindowExW(overlay) failed")
        _hwnd = hwnd
        _READY.set()

        q = _MSG()
        while user32.GetMessageW(ctypes.byref(q), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(q))
            user32.DispatchMessageW(ctypes.byref(q))
    except Exception as e:
        log.warning("usage overlay unavailable: %s", e)
    finally:
        _READY.clear()
        globals()["_hwnd"] = None
        token = globals().get("_TOKEN")
        if token:
            try:
                gdiplus.GdiplusShutdown(token)
            except Exception:
                pass


def start(url):
    """Start the overlay message thread; failures are logged and ignored."""
    global _THREAD, _url
    if os.name != "nt":
        return
    _url = str(url)
    with _THREAD_LOCK:
        if _THREAD is not None and _THREAD.is_alive():
            return
        _THREAD = threading.Thread(target=_run, daemon=True, name="tray-usage-overlay")
        _THREAD.start()


def stop():
    hwnd = _hwnd
    if hwnd:
        try:
            user32 = _dlls()[0]
            user32.PostMessageW(hwnd, WM_CLOSE, None, None)
        except Exception:
            pass
