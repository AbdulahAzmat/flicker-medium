"""Put what just landed on the Windows clipboard, so Ctrl+V works straight away.

Several forms go on the clipboard at once and each app takes the one it understands:

  the files themselves   paste into Explorer, a chat, an email, a browser upload
  the picture            paste into Paint, Photoshop, a document (one image only)
  the text               paste into any text box (one small text file only)
"""

import ctypes
import io
import struct
import time
from ctypes import wintypes as wt

from PIL import Image, ImageOps

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


def _declare(function, restype, *argtypes):
    function.restype, function.argtypes = restype, argtypes


_declare(user32.OpenClipboard, wt.BOOL, wt.HWND)
_declare(user32.CloseClipboard, wt.BOOL)
_declare(user32.EmptyClipboard, wt.BOOL)
_declare(user32.SetClipboardData, wt.HANDLE, wt.UINT, wt.HANDLE)
_declare(user32.RegisterClipboardFormatW, wt.UINT, wt.LPCWSTR)
_declare(user32.GetClipboardSequenceNumber, wt.DWORD)
_declare(kernel32.GlobalAlloc, ctypes.c_void_p, wt.UINT, ctypes.c_size_t)
_declare(kernel32.GlobalLock, ctypes.c_void_p, ctypes.c_void_p)
_declare(kernel32.GlobalUnlock, wt.BOOL, ctypes.c_void_p)
_declare(kernel32.GlobalFree, ctypes.c_void_p, ctypes.c_void_p)

CF_DIB, CF_UNICODETEXT, CF_HDROP = 8, 13, 15
DROP_EFFECT = user32.RegisterClipboardFormatW("Preferred DropEffect")
MAX_TEXT_BYTES = 64 * 1024
MAX_PICTURE_PIXELS = 30_000_000


def _put(formats):
    """Replace the clipboard with {format: bytes}. Returns its new sequence number, or None."""
    for _ in range(10):  # another app may be holding the clipboard for a moment
        if user32.OpenClipboard(None):
            break
        time.sleep(0.03)
    else:
        return None
    try:
        user32.EmptyClipboard()
        for fmt, data in formats.items():
            handle = kernel32.GlobalAlloc(2, len(data))
            ctypes.memmove(kernel32.GlobalLock(handle), data, len(data))
            kernel32.GlobalUnlock(handle)
            if not user32.SetClipboardData(fmt, handle):
                kernel32.GlobalFree(handle)  # on success Windows owns the memory
    finally:
        user32.CloseClipboard()
    return user32.GetClipboardSequenceNumber()


def copy_files(paths):
    """Copy the files (and the text of a lone small text file). Returns (formats, sequence) for add_picture."""
    file_list = ("\0".join(str(p) for p in paths) + "\0\0").encode("utf-16-le")
    formats = {
        CF_HDROP: struct.pack("<IiiII", 20, 0, 0, 0, 1) + file_list,  # DROPFILES header, wide names
        DROP_EFFECT: struct.pack("<I", 1),  # copy, never move: pasting must not empty the inbox
    }
    if len(paths) == 1 and paths[0].suffix.lower() in (".txt", ".md") and paths[0].stat().st_size <= MAX_TEXT_BYTES:
        try:
            formats[CF_UNICODETEXT] = (paths[0].read_text(encoding="utf-8") + "\0").encode("utf-16-le")
        except (OSError, UnicodeDecodeError):
            pass
    return formats, _put(formats)


def add_picture(path, formats, sequence):
    """Add the image's pixels alongside the file. Slower, so it runs after the card is already up."""
    try:
        with Image.open(path) as image:
            if image.width * image.height > MAX_PICTURE_PIXELS:
                return
            buffer = io.BytesIO()
            ImageOps.exif_transpose(image).convert("RGB").save(buffer, "BMP")
    except Exception:
        return
    # Only if nothing else has been copied meanwhile: never overwrite the user's own copy.
    if user32.GetClipboardSequenceNumber() == sequence:
        _put({**formats, CF_DIB: buffer.getvalue()[14:]})  # a DIB is a BMP without its file header
