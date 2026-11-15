"""The landing card: a small pill that drops in from the top of the screen when a file arrives.

It floats above whatever you're working in, never takes keyboard focus, and is gone in a few
seconds. Clicking it opens what arrived. Windows only.

    python overlay.py      show a demo card
"""

import ctypes
import math
import queue
import threading
import time
from ctypes import wintypes as wt

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

MINT = (124, 245, 196)

# The light, card outline and tick take the colour of what arrived.
COLOURS = {
    "image": (196, 170, 255),  # light purple
    "pdf": (255, 142, 135),    # light red
    "doc": (242, 245, 250),    # white
    "video": (130, 200, 255),  # light blue
    "audio": (255, 208, 128),  # light amber
    "file": MINT,              # anything else, or a mixed batch
}
PANEL = (21, 25, 30)
TEXT = (238, 241, 244)
MUTED = (139, 149, 161)

LIGHT = 3.25  # seconds until the border light has fully faded (see tick_tock)
RISE, FADE = 0.75, 0.28
HOLD = LIGHT - RISE  # the card waits for the light to finish, then leaves briskly

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wt.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int), ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
        ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH), ("lpszMenuName", wt.LPCWSTR),
        ("lpszClassName", wt.LPCWSTR),
    ]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_byte), ("BlendFlags", ctypes.c_byte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_byte)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG), ("biPlanes", wt.WORD),
        ("biBitCount", wt.WORD), ("biCompression", wt.DWORD), ("biSizeImage", wt.DWORD),
        ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD),
        ("biClrImportant", wt.DWORD),
    ]


def _declare(function, restype, *argtypes):
    function.restype, function.argtypes = restype, argtypes


_declare(user32.DefWindowProcW, LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
_declare(user32.RegisterClassW, wt.ATOM, ctypes.POINTER(WNDCLASSW))
_declare(user32.CreateWindowExW, wt.HWND, wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int,
         ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID)
_declare(user32.DestroyWindow, wt.BOOL, wt.HWND)
_declare(user32.ShowWindow, wt.BOOL, wt.HWND, ctypes.c_int)
_declare(user32.UpdateLayeredWindow, wt.BOOL, wt.HWND, wt.HDC, ctypes.POINTER(wt.POINT), ctypes.POINTER(wt.SIZE),
         wt.HDC, ctypes.POINTER(wt.POINT), wt.COLORREF, ctypes.POINTER(BLENDFUNCTION), wt.DWORD)
_declare(user32.PeekMessageW, wt.BOOL, ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT, wt.UINT)
_declare(user32.TranslateMessage, wt.BOOL, ctypes.POINTER(wt.MSG))
_declare(user32.DispatchMessageW, LRESULT, ctypes.POINTER(wt.MSG))
_declare(user32.LoadCursorW, wt.HANDLE, wt.HINSTANCE, wt.LPVOID)
_declare(user32.GetDC, wt.HDC, wt.HWND)
_declare(user32.ReleaseDC, ctypes.c_int, wt.HWND, wt.HDC)
_declare(user32.SystemParametersInfoW, wt.BOOL, wt.UINT, wt.UINT, wt.LPVOID, wt.UINT)
_declare(user32.GetSystemMetrics, ctypes.c_int, ctypes.c_int)
_declare(user32.SetThreadDpiAwarenessContext, wt.HANDLE, wt.HANDLE)
_declare(user32.GetDpiForSystem, wt.UINT)
_declare(kernel32.GetModuleHandleW, wt.HMODULE, wt.LPCWSTR)
_declare(gdi32.CreateCompatibleDC, wt.HDC, wt.HDC)
_declare(gdi32.CreateDIBSection, wt.HBITMAP, wt.HDC, ctypes.POINTER(BITMAPINFOHEADER), wt.UINT,
         ctypes.POINTER(ctypes.c_void_p), wt.HANDLE, wt.DWORD)
_declare(gdi32.SelectObject, wt.HGDIOBJ, wt.HDC, wt.HGDIOBJ)
_declare(gdi32.DeleteObject, wt.BOOL, wt.HGDIOBJ)
_declare(gdi32.DeleteDC, wt.BOOL, wt.HDC)

WS_POPUP = 0x80000000
WS_EX_TOPMOST, WS_EX_TRANSPARENT, WS_EX_TOOLWINDOW = 0x8, 0x20, 0x80
WS_EX_LAYERED, WS_EX_NOACTIVATE = 0x80000, 0x08000000
WM_MOUSEACTIVATE, WM_LBUTTONUP, MA_NOACTIVATE = 0x21, 0x202, 3
CLASS_NAME = "FlickLandingCard"

_clicked = set()  # window handles that were clicked this frame


@WNDPROC
def _window_proc(hwnd, message, wparam, lparam):
    if message == WM_MOUSEACTIVATE:
        return MA_NOACTIVATE  # a click must never pull focus away from the user's work
    if message == WM_LBUTTONUP:
        _clicked.add(hwnd)
        return 0
    return user32.DefWindowProcW(hwnd, message, wparam, lparam)


class Layer:
    """One always-on-top window whose pixels (including transparency) come from a PIL image."""

    def __init__(self, image, click_through):
        self.size = image.size
        width, height = image.size
        header = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), width, -height, 1, 32, 0, 0, 0, 0, 0, 0)
        bits = ctypes.c_void_p()
        screen = user32.GetDC(None)
        self.dc = gdi32.CreateCompatibleDC(screen)
        self.bitmap = gdi32.CreateDIBSection(screen, ctypes.byref(header), 0, ctypes.byref(bits), None, 0)
        user32.ReleaseDC(None, screen)
        pixels = image.convert("RGBa").tobytes("raw", "BGRa")  # premultiplied, as Windows expects
        ctypes.memmove(bits, pixels, len(pixels))
        self.previous = gdi32.SelectObject(self.dc, self.bitmap)

        style = WS_EX_LAYERED | WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
        if click_through:
            style |= WS_EX_TRANSPARENT
        self.hwnd = user32.CreateWindowExW(
            style, CLASS_NAME, "Flick", WS_POPUP, 0, 0, width, height,
            None, None, kernel32.GetModuleHandleW(None), None,
        )
        self.shown = False

    def place(self, x, y, opacity):
        blend = BLENDFUNCTION(0, 0, max(0, min(255, int(opacity * 255))), 1)
        user32.UpdateLayeredWindow(
            self.hwnd, None, ctypes.byref(wt.POINT(int(x), int(y))), ctypes.byref(wt.SIZE(*self.size)),
            self.dc, ctypes.byref(wt.POINT(0, 0)), 0, ctypes.byref(blend), 2,
        )
        if not self.shown:
            user32.ShowWindow(self.hwnd, 4)  # show without activating
            self.shown = True

    def close(self):
        user32.DestroyWindow(self.hwnd)
        gdi32.SelectObject(self.dc, self.previous)
        gdi32.DeleteObject(self.bitmap)
        gdi32.DeleteDC(self.dc)


def _font(size, bold=False):
    for name in (("seguisb.ttf", "segoeuib.ttf") if bold else ("segoeui.ttf",)) + ("arial.ttf",):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _fit(text, font, width):
    if font.getlength(text) <= width:
        return text
    while len(text) > 1 and font.getlength(text + "…") > width:
        text = text[:-1]
    return text + "…"


def _thumbnail(path, size):
    try:
        with Image.open(path) as image:
            image.draft("RGB", (size * 2, size * 2))
            return ImageOps.fit(ImageOps.exif_transpose(image).convert("RGB"), (size, size))
    except Exception:
        return None


def render_card(title, subtitle, preview, badge, scale, accent=MINT):
    """Draw the pill at 2x and shrink it, so the curves come out smooth."""
    s = scale * 2
    px = lambda v: int(round(v * s))
    margin, width, height = px(30), px(340), px(72)
    canvas = Image.new("RGBA", (width + margin * 2, height + margin * 2), (0, 0, 0, 0))
    box = (margin, margin, margin + width, margin + height)

    shadow = Image.new("L", canvas.size, 0)
    ImageDraw.Draw(shadow).rounded_rectangle((box[0], box[1] + px(6), box[2], box[3] + px(6)), px(22), fill=150)
    canvas.putalpha(shadow.filter(ImageFilter.GaussianBlur(px(10))))

    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle(box, px(22), fill=PANEL + (250,), outline=accent + (90,), width=max(1, px(1)))

    side = px(52)
    left, top = box[0] + px(10), box[1] + px(10)
    mask = Image.new("L", (side, side), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, side, side), px(14), fill=255)
    picture = _thumbnail(preview, side) if preview else None
    if picture is None:
        picture = Image.new("RGB", (side, side), (15, 18, 22))
        font = _font(px(13), bold=True)
        label = _fit(badge.upper(), font, side - px(8))
        ImageDraw.Draw(picture).text((side / 2, side / 2), label, font=font, fill=accent, anchor="mm")
    canvas.paste(picture, (left, top), mask)

    tick = px(22)
    cx, cy = box[2] - px(16) - tick // 2, box[1] + height // 2
    draw.ellipse((cx - tick // 2, cy - tick // 2, cx + tick // 2, cy + tick // 2), fill=accent + (255,))
    draw.line(
        [(cx - px(5), cy), (cx - px(1.5), cy + px(3.5)), (cx + px(5), cy - px(3.5))],
        fill=(11, 13, 16, 255), width=px(2.2), joint="curve",
    )

    text_left = left + side + px(12)
    room = cx - tick // 2 - px(12) - text_left
    title_font, sub_font = _font(px(14.5), bold=True), _font(px(12.5))
    draw.text((text_left, box[1] + px(26)), _fit(title, title_font, room), font=title_font, fill=TEXT, anchor="lm")
    draw.text((text_left, box[1] + px(47)), _fit(subtitle, sub_font, room), font=sub_font, fill=MUTED, anchor="lm")

    return canvas.resize((canvas.width // 2, canvas.height // 2), Image.LANCZOS), margin // 2


GLOW_DEPTH = 80   # how far the light reaches in from the middle of an edge, in pixels at 100% scaling
GLOW_PEAK = 150   # brightness right at the edge, 0-255


def render_edge(length, scale, accent=MINT):
    """Light along one screen edge, corner to corner: a bell curve, deepest and brightest mid-edge.

    Drawn as the top edge. It is smooth, so a small grid stretched to size is exact enough.
    """
    columns, rows = 200, 40
    cells = []
    bells = [math.sin(math.pi * (x + 0.5) / columns) ** 1.5 for x in range(columns)]
    for y in range(rows):
        inward = (y + 0.5) / rows
        for bell in bells:
            reach = 0.25 + 0.75 * bell          # corners are shallow, the middle is deep
            strength = 0.4 + 0.6 * bell         # and dimmer, so overlapping corners don't flare
            cells.append(int(max(0.0, 1 - inward / reach) ** 2 * strength * GLOW_PEAK))
    alpha = Image.new("L", (columns, rows))
    alpha.putdata(cells)
    alpha = alpha.resize((length, int(GLOW_DEPTH * scale)), Image.BICUBIC)
    edge = Image.new("RGBA", alpha.size, accent + (0,))
    edge.putalpha(alpha)
    return edge


def render_border(width, height, scale, accent=MINT):
    """Four strips that together ring the screen. Returns [(image, x, y), ...]."""
    top, side = render_edge(width, scale, accent), render_edge(height, scale, accent)
    left = side.transpose(Image.ROTATE_90)
    return [
        (top, 0, 0),
        (top.transpose(Image.FLIP_TOP_BOTTOM), 0, height - top.height),
        (left, 0, 0),
        (left.transpose(Image.FLIP_LEFT_RIGHT), width - left.width, 0),
    ]


def _ease(p):
    """Smooth start and stop, so brightness glides instead of snapping."""
    return 0.5 - 0.5 * math.cos(math.pi * max(0.0, min(1.0, p)))


def tick_tock(t):
    """Brightness of the border light over time: an unhurried "tick", then a softer, longer "tock"."""
    if t < 0.30:
        return _ease(t / 0.30)                           # tick: glows up
    if t < 0.85:
        return 1 - 0.85 * _ease((t - 0.30) / 0.55)       # and settles almost to dark
    if t < 1.25:
        return 0.15 + 0.55 * _ease((t - 0.85) / 0.40)    # tock: a gentler second beat
    return 0.70 * (1 - _ease((t - 1.25) / 2.0))          # that lingers and fades


class Overlay:
    def __init__(self):
        self.jobs = queue.Queue()
        threading.Thread(target=self._run, daemon=True).start()

    def show(self, title, subtitle, preview=None, badge="file", on_click=None, kind="file"):
        """Queue a card. Safe to call from any thread."""
        self.jobs.put((title, subtitle, preview, badge, on_click, kind))

    def _run(self):
        try:
            user32.SetThreadDpiAwarenessContext(wt.HANDLE(-4))  # per-monitor: crisp on scaled displays
        except Exception:
            pass
        window_class = WNDCLASSW()
        window_class.lpfnWndProc = _window_proc
        window_class.hInstance = kernel32.GetModuleHandleW(None)
        window_class.hCursor = user32.LoadCursorW(None, ctypes.c_void_p(32649))  # pointing hand
        window_class.lpszClassName = CLASS_NAME
        user32.RegisterClassW(ctypes.byref(window_class))
        while True:
            job = self.jobs.get()
            try:
                self._play(*job)
            except Exception as error:  # an animation must never take the server down
                print(f"landing card failed: {error}", flush=True)

    def _play(self, title, subtitle, preview, badge, on_click, kind):
        accent = COLOURS.get(kind, MINT)
        scale = user32.GetDpiForSystem() / 96
        work = wt.RECT()
        user32.SystemParametersInfoW(0x30, 0, ctypes.byref(work), 0)  # screen minus the taskbar
        centre = (work.left + work.right) // 2

        card_image, margin = render_card(title, subtitle, preview, badge, scale, accent)
        border = render_border(user32.GetSystemMetrics(0), user32.GetSystemMetrics(1), scale, accent)
        # No windows exist between cards, so nothing of ours sits in the system while idle.
        # Four thin strips rather than one screen-sized window, so Windows never treats us as full-screen.
        glows = [(Layer(image, click_through=True), x, y) for image, x, y in border]
        card = Layer(card_image, click_through=False)
        card_x = centre - card_image.width // 2
        rest_y = work.top + int(40 * scale) - margin

        message = wt.MSG()
        started = time.perf_counter()
        total = RISE + HOLD + FADE
        try:
            while True:
                t = time.perf_counter() - started
                if t >= total:
                    break
                while user32.PeekMessageW(ctypes.byref(message), None, 0, 0, 1):
                    user32.TranslateMessage(ctypes.byref(message))
                    user32.DispatchMessageW(ctypes.byref(message))
                if card.hwnd in _clicked:
                    _clicked.discard(card.hwnd)
                    if on_click:
                        on_click()
                    break

                if t < RISE:
                    p = 1 - (1 - t / RISE) ** 3  # ease out
                    card.place(card_x, rest_y - (1 - p) * 44 * scale, p)  # drops in from above
                elif t < RISE + HOLD:
                    card.place(card_x, rest_y, 1)
                else:
                    p = (t - RISE - HOLD) / FADE
                    card.place(card_x, rest_y - p * p * 44 * scale, 1 - p)  # flicks back up the way it came

                bloom = tick_tock(t)
                for glow, x, y in glows:
                    glow.place(x, y, bloom)
                time.sleep(1 / 90)
        finally:
            card.close()
            for glow, _, _ in glows:
                glow.close()


if __name__ == "__main__":
    import sys

    # python overlay.py [kind]   e.g. image, pdf, doc, video, audio, file
    kind = sys.argv[1] if len(sys.argv) > 1 else "image"
    demo = Overlay()
    demo.show(f"Demo.{kind}", "From Galaxy S23 · 2.6 MB", badge=kind, kind=kind)
    time.sleep(RISE + HOLD + FADE + 0.6)
