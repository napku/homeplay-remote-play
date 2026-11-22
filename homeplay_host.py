#!/usr/bin/env python3
"""
HomePlay host - play the games on this PC from a web browser on your phone,
tablet or another computer.

What it does
  * captures the screen with the graphics card and encodes it as H.264 (via FFmpeg)
  * captures the sound the PC is playing
  * replays the keyboard, mouse and gamepad input that the browser sends back
  * serves the web page (web/ folder) and talks to it over one WebSocket

Runs on Windows 10/11. Start it with start.bat, or:  python homeplay_host.py --help

Only the Python standard library is required. Optional extras:
  soundcard (+ numpy)  sends the PC's sound
  vgamepad             virtual Xbox controller for gamepad / on-screen controller input
  cryptography         self-signed HTTPS for --lan mode
FFmpeg 6.0 or newer does the screen capture and encoding.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import collections
import contextlib
import datetime
import hashlib
import hmac
import ipaddress
import json
import logging
import math
import os
import queue
import re
import secrets
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
from dataclasses import asdict, dataclass, replace
from pathlib import Path

VERSION = "1.2.0"
IS_WINDOWS = sys.platform == "win32"
BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "web"
CONFIG_FILE = BASE_DIR / "homeplay_config.json"
CERT_DIR = BASE_DIR / "certs"
DEMO_SIZE = (1280, 720)

# Binary messages from host to browser. The first byte is the message type.
MSG_VIDEO_CONFIG = 1  # [1][codec string length u8][codec string][decoder config record]
MSG_VIDEO_FRAME = 2   # [2][flags u8][seq u32le][timestamp ms u32le][access unit, length-prefixed NALs / OBUs]
MSG_AUDIO = 3         # [3][channels u8][block seq u16le][sample rate u32le][PCM s16le interleaved]
MSG_AUDIO_OPUS = 4    # [4][channels u8][packet seq u16le][sample rate u32le][one 10 ms Opus packet]

# WebSocket close codes the page understands.
CLOSE_BAD_PASSWORD = 4001
CLOSE_REPLACED = 4002
CLOSE_LOCKED_OUT = 4003
CLOSE_PROTOCOL = 4004

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    NO_WINDOW = subprocess.CREATE_NO_WINDOW
    ABOVE_NORMAL = subprocess.ABOVE_NORMAL_PRIORITY_CLASS
else:
    NO_WINDOW = 0
    ABOVE_NORMAL = 0

log = logging.getLogger("homeplay")


# ======================================================================================
# Settings and small helpers
# ======================================================================================

@dataclass(frozen=True)
class StreamSettings:
    fps: int = 60
    kbps: int = 15000     # when auto is on, this is the ceiling the host may use
    height: int = 0       # 0 = native resolution, otherwise scale down to this height
    monitor: int = 0
    cursor: bool = True
    codec: str = "auto"   # auto | h264 | hevc | av1
    auto: bool = False     # let the host pick the bitrate to match the connection

    @classmethod
    def from_client(cls, d: dict, base: "StreamSettings") -> "StreamSettings":
        def num(key, lo, hi, default):
            try:
                value = int(float(d.get(key, default)))
            except (TypeError, ValueError):
                return default
            return max(lo, min(hi, value))

        height = base.height
        res = d.get("res")
        if isinstance(res, str):
            if res == "native":
                height = 0
            elif res.endswith("p") and res[:-1].isdigit():
                height = int(res[:-1])
        if height:
            height = max(240, min(4320, height))
        codec = d.get("codec", base.codec)
        if codec not in ("auto", "h264", "hevc", "av1"):
            codec = base.codec
        return cls(
            fps=num("fps", 10, 240, base.fps),
            kbps=num("kbps", 500, 150000, base.kbps),
            height=height,
            monitor=num("mon", 0, 15, base.monitor),
            cursor=bool(d.get("cursor", base.cursor)),
            codec=codec,
            auto=bool(d.get("auto", base.auto)),
        )


def clamp_int(value, lo: int, hi: int) -> int:
    v = int(value)
    return lo if v < lo else hi if v > hi else v


MIN_PASSWORD_LENGTH = 10      # the shortest password --password accepts
PASSWORD_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"   # no look-alikes (i/l/1, o/0)


def generate_password() -> str:
    return "-".join("".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(4)) for _ in range(4))


def load_config() -> dict:
    try:
        data = json.loads(CONFIG_FILE.read_text("utf-8"))
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        log.warning("Could not read %s (%s); starting fresh.", CONFIG_FILE.name, e)
        return {}


def save_config(cfg: dict) -> None:
    try:
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2), "utf-8")
    except OSError as e:
        log.warning("Could not save %s: %s", CONFIG_FILE.name, e)


def lan_addresses() -> list[str]:
    ips = set()
    with contextlib.suppress(OSError):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("192.0.2.1", 9))      # no packet is sent; this just picks the main interface
            ips.add(s.getsockname()[0])
        finally:
            s.close()
    with contextlib.suppress(OSError):
        ips.update(socket.gethostbyname_ex(socket.gethostname())[2])
    return sorted(ip for ip in ips if not ip.startswith(("127.", "169.254.", "0.")))


def describe_device(user_agent: str) -> str:
    ua = user_agent or ""
    device = ("iPhone" if "iPhone" in ua else "iPad" if "iPad" in ua else "Android" if "Android" in ua
              else "Windows PC" if "Windows" in ua else "Mac" if "Macintosh" in ua
              else "Linux PC" if "Linux" in ua else "device")
    browser = ("Edge" if "Edg/" in ua else "Firefox" if "Firefox/" in ua else "Chrome" if "Chrome/" in ua
               else "Safari" if "Safari/" in ua else "browser")
    return f"{device} ({browser})"


def clean_for_log(text: str, limit: int = 80) -> str:
    """Printable ASCII only, and short: header values come from the other side and end up in the log."""
    return re.sub(r"[^\x20-\x7e]", "?", text)[:limit]


# ======================================================================================
# Windows: DPI, console, sleep, display info and input injection
# ======================================================================================

class Windows:
    """Thin wrappers around a few Win32 calls. All of them do nothing on other systems."""

    @staticmethod
    def prepare_process() -> None:
        if not IS_WINDOWS:
            return
        # Real pixel coordinates even with display scaling (125 %, 150 %, ...).
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            with contextlib.suppress(Exception):
                ctypes.windll.user32.SetProcessDPIAware()
        # A click inside a console window with "QuickEdit" on pauses all output of the
        # program, which would freeze the stream. Turn QuickEdit off for this window.
        try:
            k32 = ctypes.WinDLL("kernel32")
            k32.GetStdHandle.restype = wintypes.HANDLE
            k32.GetStdHandle.argtypes = (wintypes.DWORD,)
            k32.GetConsoleMode.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
            k32.SetConsoleMode.argtypes = (wintypes.HANDLE, wintypes.DWORD)
            handle = k32.GetStdHandle(0xFFFFFFF6)            # STD_INPUT_HANDLE
            mode = wintypes.DWORD()
            if k32.GetConsoleMode(handle, ctypes.byref(mode)):
                k32.SetConsoleMode(handle, (mode.value & ~0x0040) | 0x0080)
        except Exception:
            pass
        Windows.boost_process()

    # ---- staying fast while a game is using the PC ----
    @staticmethod
    def _kernel32():
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.SetPriorityClass.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        k32.SetPriorityClass.restype = wintypes.BOOL
        k32.SetProcessInformation.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
        k32.SetProcessInformation.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = (wintypes.HANDLE,)
        return k32

    @staticmethod
    def boost_process(pid: int | None = None) -> bool:
        """Keep a process responsive while a game has the focus: opt it out of Windows power
        throttling (EcoQoS), which slows down background work, and give this process
        above-normal priority (FFmpeg gets that at launch). pid=None means this process."""
        if not IS_WINDOWS:
            return False
        try:
            k32 = Windows._kernel32()

            class POWER_THROTTLING_STATE(ctypes.Structure):
                _fields_ = [("Version", wintypes.ULONG), ("ControlMask", wintypes.ULONG),
                            ("StateMask", wintypes.ULONG)]

            # Version 1, control EXECUTION_SPEED, state 0 = "never throttle me"
            state = POWER_THROTTLING_STATE(1, 0x1, 0x0)
            if pid is None:
                handle, close = k32.GetCurrentProcess(), False
                k32.SetPriorityClass(handle, 0x00008000)            # ABOVE_NORMAL_PRIORITY_CLASS
            else:
                handle, close = k32.OpenProcess(0x0200, False, pid), True   # PROCESS_SET_INFORMATION
                if not handle:
                    return False
            try:
                return bool(k32.SetProcessInformation(handle, 4,        # ProcessPowerThrottling
                                                      ctypes.byref(state), ctypes.sizeof(state)))
            finally:
                if close:
                    k32.CloseHandle(handle)
        except Exception as e:
            log.debug("Could not tune process %s: %s", pid or "self", e)
            return False

    @staticmethod
    def mmcss_begin(task: str = "Audio"):
        """Put the calling thread in the Multimedia Class Scheduler, so sound keeps flowing
        when a game loads the CPU. Returns a handle for mmcss_end(), or None."""
        if not IS_WINDOWS:
            return None
        try:
            avrt = ctypes.WinDLL("avrt")
            avrt.AvSetMmThreadCharacteristicsW.argtypes = (wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD))
            avrt.AvSetMmThreadCharacteristicsW.restype = wintypes.HANDLE
            index = wintypes.DWORD(0)
            return avrt.AvSetMmThreadCharacteristicsW(task, ctypes.byref(index)) or None
        except Exception as e:
            log.debug("MMCSS not available: %s", e)
            return None

    @staticmethod
    def mmcss_end(handle) -> None:
        if not handle or not IS_WINDOWS:
            return
        with contextlib.suppress(Exception):
            avrt = ctypes.WinDLL("avrt")
            avrt.AvRevertMmThreadCharacteristics.argtypes = (wintypes.HANDLE,)
            avrt.AvRevertMmThreadCharacteristics(handle)

    @staticmethod
    def _execution_state(flags: int) -> None:
        if not IS_WINDOWS:
            return
        try:
            fn = ctypes.windll.kernel32.SetThreadExecutionState
            fn.argtypes = (ctypes.c_uint32,)
            fn.restype = ctypes.c_uint32
            fn(flags)
        except Exception:
            pass

    @classmethod
    def keep_awake(cls, display: bool) -> None:
        """Keep the PC from sleeping; with display=True also keep the screen on."""
        cls._execution_state(0x80000000 | 0x00000001 | (0x00000002 if display else 0))

    @classmethod
    def allow_sleep(cls) -> None:
        cls._execution_state(0x80000000)

    @staticmethod
    def display_info():
        """(GPU name, PCI vendor id, [(x, y, w, h) per output]) for the main graphics card,
        in the order FFmpeg's ddagrab numbers the screens. (None, 0, []) if unknown."""
        if not IS_WINDOWS:
            return None, 0, []
        try:
            return _dxgi_display_info()
        except Exception as e:  # pragma: no cover - depends on the machine
            log.debug("DXGI query failed: %s", e)
            return None, 0, []

    @staticmethod
    def primary_screen_size():
        if not IS_WINDOWS:
            return DEMO_SIZE
        gsm = ctypes.windll.user32.GetSystemMetrics
        return gsm(0), gsm(1)

    # ---- clipboard (for sharing copied text with the browser) ----
    @staticmethod
    def clipboard_sequence() -> int:
        if not IS_WINDOWS:
            return 0
        try:
            return int(ctypes.windll.user32.GetClipboardSequenceNumber())
        except Exception:
            return 0

    @staticmethod
    def get_clipboard_text():
        if not IS_WINDOWS:
            return None
        u, k = ctypes.windll.user32, ctypes.windll.kernel32
        u.OpenClipboard.argtypes = (ctypes.c_void_p,)
        u.IsClipboardFormatAvailable.argtypes = (ctypes.c_uint,)
        u.GetClipboardData.argtypes = (ctypes.c_uint,)
        u.GetClipboardData.restype = ctypes.c_void_p
        k.GlobalLock.argtypes = (ctypes.c_void_p,)
        k.GlobalLock.restype = ctypes.c_void_p
        k.GlobalUnlock.argtypes = (ctypes.c_void_p,)
        if not u.OpenClipboard(None):
            return None
        try:
            if not u.IsClipboardFormatAvailable(13):           # CF_UNICODETEXT
                return None
            if Windows._clipboard_is_sensitive(u):
                return None
            handle = u.GetClipboardData(13)
            if not handle:
                return None
            ptr = k.GlobalLock(handle)
            if not ptr:
                return None
            try:
                return ctypes.wstring_at(ptr)
            finally:
                k.GlobalUnlock(handle)
        finally:
            u.CloseClipboard()

    @staticmethod
    def _clipboard_is_sensitive(u) -> bool:
        """True if the program that copied this marked it as private. Password managers do that with the
        'ExcludeClipboardContentFromMonitorProcessing' format so clipboard tools leave the text alone.
        The clipboard must be open. If the check itself fails, the text counts as ordinary."""
        try:
            u.RegisterClipboardFormatW.argtypes = (wintypes.LPCWSTR,)
            u.RegisterClipboardFormatW.restype = ctypes.c_uint
            fmt = u.RegisterClipboardFormatW("ExcludeClipboardContentFromMonitorProcessing")
            return bool(fmt and u.IsClipboardFormatAvailable(fmt))
        except Exception:
            return False

    @staticmethod
    def set_clipboard_text(text: str) -> bool:
        if not IS_WINDOWS:
            return False
        u, k = ctypes.windll.user32, ctypes.windll.kernel32
        for fn, argt, ret in (
            (u.OpenClipboard, (ctypes.c_void_p,), None), (u.SetClipboardData, (ctypes.c_uint, ctypes.c_void_p), ctypes.c_void_p),
            (k.GlobalAlloc, (ctypes.c_uint, ctypes.c_size_t), ctypes.c_void_p),
            (k.GlobalLock, (ctypes.c_void_p,), ctypes.c_void_p), (k.GlobalUnlock, (ctypes.c_void_p,), None),
            (k.GlobalFree, (ctypes.c_void_p,), ctypes.c_void_p)):
            fn.argtypes = argt
            if ret:
                fn.restype = ret
        data = ctypes.create_unicode_buffer(text.replace("\x00", ""))
        size = ctypes.sizeof(data)
        handle = k.GlobalAlloc(0x0002, size)                   # GMEM_MOVEABLE
        if not handle:
            return False
        ptr = k.GlobalLock(handle)
        if not ptr:
            k.GlobalFree(handle)
            return False
        ctypes.memmove(ptr, data, size)
        k.GlobalUnlock(handle)
        if not u.OpenClipboard(None):
            k.GlobalFree(handle)
            return False
        try:
            u.EmptyClipboard()
            if not u.SetClipboardData(13, handle):             # CF_UNICODETEXT; on success Windows owns it
                k.GlobalFree(handle)
                return False
            return True
        finally:
            u.CloseClipboard()


if IS_WINDOWS:
    class _GUID(ctypes.Structure):
        _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16),
                    ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_ubyte * 8)]

    class _LUID(ctypes.Structure):
        _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]

    class _DXGI_ADAPTER_DESC(ctypes.Structure):
        _fields_ = [("Description", ctypes.c_wchar * 128), ("VendorId", ctypes.c_uint),
                    ("DeviceId", ctypes.c_uint), ("SubSysId", ctypes.c_uint), ("Revision", ctypes.c_uint),
                    ("DedicatedVideoMemory", ctypes.c_size_t), ("DedicatedSystemMemory", ctypes.c_size_t),
                    ("SharedSystemMemory", ctypes.c_size_t), ("AdapterLuid", _LUID)]

    class _DXGI_OUTPUT_DESC(ctypes.Structure):
        _fields_ = [("DeviceName", ctypes.c_wchar * 32), ("DesktopCoordinates", wintypes.RECT),
                    ("AttachedToDesktop", wintypes.BOOL), ("Rotation", ctypes.c_uint),
                    ("Monitor", ctypes.c_void_p)]

    def _com_call(obj, slot, *argtypes):
        """Bind COM method number `slot` of `obj` (a c_void_p interface pointer)."""
        vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        fn = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)(vtable[slot])
        return lambda *args: fn(obj, *args)

    def _dxgi_display_info():
        dxgi = ctypes.WinDLL("dxgi")
        dxgi.CreateDXGIFactory1.argtypes = (ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p))
        dxgi.CreateDXGIFactory1.restype = ctypes.c_long
        iid_factory1 = _GUID(0x770AAE78, 0xF26F, 0x4DBA,
                             (ctypes.c_ubyte * 8)(0xA8, 0x29, 0x25, 0x3C, 0x83, 0xD1, 0xB3, 0x87))
        factory = ctypes.c_void_p()
        if dxgi.CreateDXGIFactory1(ctypes.byref(iid_factory1), ctypes.byref(factory)) != 0 or not factory:
            return None, 0, []
        adapter = ctypes.c_void_p()
        out_ptr = ctypes.POINTER(ctypes.c_void_p)
        try:
            # IDXGIFactory::EnumAdapters (slot 7). Adapter 0 drives the main display and is
            # the one FFmpeg opens when no device is given.
            if _com_call(factory, 7, ctypes.c_uint, out_ptr)(0, ctypes.byref(adapter)) != 0:
                return None, 0, []
            desc = _DXGI_ADAPTER_DESC()
            _com_call(adapter, 8, ctypes.POINTER(_DXGI_ADAPTER_DESC))(ctypes.byref(desc))  # GetDesc
            rects = []
            for index in range(16):
                output = ctypes.c_void_p()
                if _com_call(adapter, 7, ctypes.c_uint, out_ptr)(index, ctypes.byref(output)) != 0:
                    break                                                   # no more outputs
                try:
                    od = _DXGI_OUTPUT_DESC()
                    _com_call(output, 7, ctypes.POINTER(_DXGI_OUTPUT_DESC))(ctypes.byref(od))  # GetDesc
                    r = od.DesktopCoordinates
                    rects.append((r.left, r.top, r.right - r.left, r.bottom - r.top))
                finally:
                    _com_call(output, 2)()                                  # Release
            return desc.Description.strip() or None, int(desc.VendorId), rects
        finally:
            if adapter:
                _com_call(adapter, 2)()
            _com_call(factory, 2)()

    class _MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

    class _KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

    class _HARDWAREINPUT(ctypes.Structure):
        _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]

    class _INPUTUNION(ctypes.Union):
        _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT)]

    class _INPUT(ctypes.Structure):
        _anonymous_ = ("u",)
        _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


def _build_scancodes() -> dict:
    """Browser KeyboardEvent.code -> (PC scan code, extended-key flag).
    Scan codes are layout independent, so a Slovak, German or US keyboard all work."""
    m = {}
    plain = {
        "Escape": 0x01, "Minus": 0x0C, "Equal": 0x0D, "Backspace": 0x0E, "Tab": 0x0F,
        "BracketLeft": 0x1A, "BracketRight": 0x1B, "Enter": 0x1C, "ControlLeft": 0x1D,
        "Semicolon": 0x27, "Quote": 0x28, "Backquote": 0x29, "ShiftLeft": 0x2A, "Backslash": 0x2B,
        "Comma": 0x33, "Period": 0x34, "Slash": 0x35, "ShiftRight": 0x36, "NumpadMultiply": 0x37,
        "AltLeft": 0x38, "Space": 0x39, "CapsLock": 0x3A, "ScrollLock": 0x46,
        "Numpad7": 0x47, "Numpad8": 0x48, "Numpad9": 0x49, "NumpadSubtract": 0x4A,
        "Numpad4": 0x4B, "Numpad5": 0x4C, "Numpad6": 0x4D, "NumpadAdd": 0x4E,
        "Numpad1": 0x4F, "Numpad2": 0x50, "Numpad3": 0x51, "Numpad0": 0x52, "NumpadDecimal": 0x53,
        "IntlBackslash": 0x56, "F11": 0x57, "F12": 0x58, "NumpadEqual": 0x59,
        "KanaMode": 0x70, "IntlRo": 0x73, "Convert": 0x79, "NonConvert": 0x7B, "IntlYen": 0x7D,
        "NumpadComma": 0x7E,
    }
    for code, sc in plain.items():
        m[code] = (sc, False)
    for i, ch in enumerate("1234567890"):
        m["Digit" + ch] = (0x02 + i, False)
    for row, first in (("QWERTYUIOP", 0x10), ("ASDFGHJKL", 0x1E), ("ZXCVBNM", 0x2C)):
        for i, ch in enumerate(row):
            m["Key" + ch] = (first + i, False)
    for i in range(10):
        m[f"F{i + 1}"] = (0x3B + i, False)
    for i, sc in enumerate((0x64, 0x65, 0x66, 0x67, 0x68, 0x69, 0x6A, 0x6B, 0x6C, 0x6D, 0x6E, 0x76)):
        m[f"F{13 + i}"] = (sc, False)
    extended = {
        "NumpadEnter": 0x1C, "ControlRight": 0x1D, "NumpadDivide": 0x35, "PrintScreen": 0x37,
        "AltRight": 0x38, "NumLock": 0x45, "Home": 0x47, "ArrowUp": 0x48, "PageUp": 0x49,
        "ArrowLeft": 0x4B, "ArrowRight": 0x4D, "End": 0x4F, "ArrowDown": 0x50, "PageDown": 0x51,
        "Insert": 0x52, "Delete": 0x53, "MetaLeft": 0x5B, "MetaRight": 0x5C, "OSLeft": 0x5B,
        "OSRight": 0x5C, "ContextMenu": 0x5D,
    }
    for code, sc in extended.items():
        m[code] = (sc, True)
    return m


SCANCODES = _build_scancodes()
# Keys that work better as virtual-key codes: (VK, extended)
VIRTUAL_KEYS = {
    "Pause": (0x13, False),
    "AudioVolumeMute": (0xAD, True), "AudioVolumeDown": (0xAE, True), "AudioVolumeUp": (0xAF, True),
    "MediaTrackNext": (0xB0, True), "MediaTrackPrevious": (0xB1, True),
    "MediaStop": (0xB2, True), "MediaPlayPause": (0xB3, True),
}


class WindowsInput:
    """Replays keyboard and mouse input on this PC with SendInput."""

    KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP, KEYEVENTF_UNICODE, KEYEVENTF_SCANCODE = 0x1, 0x2, 0x4, 0x8
    MOVE, WHEEL, HWHEEL, VIRTUALDESK, ABSOLUTE = 0x1, 0x800, 0x1000, 0x4000, 0x8000
    # browser MouseEvent.button -> (down flag, up flag, mouseData)
    BUTTONS = {0: (0x02, 0x04, 0), 1: (0x20, 0x40, 0), 2: (0x08, 0x10, 0),
               3: (0x80, 0x100, 1), 4: (0x80, 0x100, 2)}

    def __init__(self):
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int)
        self.user32.SendInput.restype = wintypes.UINT
        self.user32.GetSystemMetrics.argtypes = (ctypes.c_int,)
        self.user32.GetSystemMetrics.restype = ctypes.c_int
        self.user32.MapVirtualKeyW.argtypes = (wintypes.UINT, wintypes.UINT)
        self.user32.MapVirtualKeyW.restype = wintypes.UINT
        self.keys_down: set[str] = set()
        self.buttons_down: set[int] = set()
        self._warned = False

    def _send(self, inputs) -> None:
        inputs = [i for i in inputs if i is not None]
        if not inputs:
            return
        arr = (_INPUT * len(inputs))(*inputs)
        if self.user32.SendInput(len(inputs), arr, ctypes.sizeof(_INPUT)) != len(inputs) and not self._warned:
            self._warned = True
            log.warning("Windows refused some input (lock screen or an admin window?). "
                        "If a game ignores the controls, run HomePlay as administrator.")

    @staticmethod
    def _kbd(vk: int, scan: int, flags: int):
        item = _INPUT(type=1)
        item.ki = _KEYBDINPUT(vk, scan, flags, 0, 0)
        return item

    @staticmethod
    def _mouse(dx: int, dy: int, data: int, flags: int):
        item = _INPUT(type=0)
        item.mi = _MOUSEINPUT(dx, dy, data & 0xFFFFFFFF, flags, 0, 0)
        return item

    def _key_input(self, code: str, down: bool):
        up = 0 if down else self.KEYEVENTF_KEYUP
        if code in SCANCODES:
            scan, ext = SCANCODES[code]
            return self._kbd(0, scan, self.KEYEVENTF_SCANCODE | (self.KEYEVENTF_EXTENDEDKEY if ext else 0) | up)
        if code in VIRTUAL_KEYS:
            vk, ext = VIRTUAL_KEYS[code]
            scan = self.user32.MapVirtualKeyW(vk, 0) & 0xFF
            return self._kbd(vk, scan, (self.KEYEVENTF_EXTENDEDKEY if ext else 0) | up)
        return None

    def key(self, code: str, down: bool) -> None:
        item = self._key_input(code, down)
        if item is None:
            return
        (self.keys_down.add if down else self.keys_down.discard)(code)
        self._send([item])

    def text(self, s: str) -> None:
        inputs = []
        named = {"\n": "Enter", "\r": "Enter", "\t": "Tab", "\b": "Backspace"}
        for ch in s:
            if ch in named:
                inputs += [self._key_input(named[ch], True), self._key_input(named[ch], False)]
                continue
            for (unit,) in struct.iter_unpack("<H", ch.encode("utf-16-le")):
                inputs.append(self._kbd(0, unit, self.KEYEVENTF_UNICODE))
                inputs.append(self._kbd(0, unit, self.KEYEVENTF_UNICODE | self.KEYEVENTF_KEYUP))
        self._send(inputs)

    def move_relative(self, dx: int, dy: int) -> None:
        self._send([self._mouse(dx, dy, 0, self.MOVE)])

    def move_absolute(self, nx: int, ny: int, rect) -> None:
        left, top, width, height = rect
        px = left + nx / 65535.0 * max(0, width - 1)
        py = top + ny / 65535.0 * max(0, height - 1)
        gsm = self.user32.GetSystemMetrics
        vx, vy, vw, vh = gsm(76), gsm(77), gsm(78), gsm(79)     # virtual desktop
        ax = round((px - vx) * 65535.0 / max(1, vw - 1))
        ay = round((py - vy) * 65535.0 / max(1, vh - 1))
        self._send([self._mouse(int(ax), int(ay), 0, self.MOVE | self.ABSOLUTE | self.VIRTUALDESK)])

    def button(self, button: int, down: bool) -> None:
        spec = self.BUTTONS.get(button)
        if not spec:
            return
        (self.buttons_down.add if down else self.buttons_down.discard)(button)
        self._send([self._mouse(0, 0, spec[2], spec[0] if down else spec[1])])

    def wheel(self, dy: int, dx: int) -> None:
        self._send([self._mouse(0, 0, dy, self.WHEEL) if dy else None,
                    self._mouse(0, 0, dx, self.HWHEEL) if dx else None])

    def release_all(self) -> None:
        inputs = [self._key_input(code, False) for code in self.keys_down]
        inputs += [self._mouse(0, 0, self.BUTTONS[b][2], self.BUTTONS[b][1]) for b in self.buttons_down]
        self.keys_down.clear()
        self.buttons_down.clear()
        self._send(inputs)


class LoggedInput:
    """Used with --demo: writes input to the console instead of replaying it."""

    def __init__(self):
        self.moves = 0
        self.last_report = time.monotonic()

    def _count_move(self):
        self.moves += 1
        now = time.monotonic()
        if now - self.last_report >= 2:
            log.info("input: %d mouse moves", self.moves)
            self.moves, self.last_report = 0, now

    def key(self, code, down):
        log.info("input: key %s %s", code, "down" if down else "up")

    def text(self, s):
        log.info("input: text %r", s)

    def move_relative(self, dx, dy):
        self._count_move()

    def move_absolute(self, nx, ny, rect):
        self._count_move()

    def button(self, button, down):
        log.info("input: mouse button %d %s", button, "down" if down else "up")

    def wheel(self, dy, dx):
        log.info("input: wheel %d %d", dy, dx)

    def release_all(self):
        pass


# ======================================================================================
# Gamepads (virtual Xbox 360 controllers through ViGEmBus / vgamepad)
# ======================================================================================

XUSB_BITS = (0x0001, 0x0002, 0x0004, 0x0008, 0x0010, 0x0020, 0x0040, 0x0080,
             0x0100, 0x0200, 0x0400, 0x1000, 0x2000, 0x4000, 0x8000)


class GamepadHub:
    MAX_PADS = 4

    def __init__(self, enabled: bool, demo: bool, loop, on_rumble):
        self.loop = loop
        self.on_rumble = on_rumble
        self.demo = demo
        self.vg = None
        self.pads: dict[str, dict] = {}
        if not enabled:
            self.status = "off (--no-gamepad)"
        elif demo:
            self.status = "demo mode (input is only logged)"
        else:
            try:
                import vgamepad  # noqa: F401  (connects to the ViGEmBus driver on import)
                self.vg = vgamepad
                self.status = "ready (virtual Xbox controller)"
            except Exception as e:
                self.status = "not available - " + self._explain(e)

    @staticmethod
    def _explain(error: Exception) -> str:
        if isinstance(error, ImportError):
            return "install it with:  .venv\\Scripts\\python -m pip install vgamepad"
        hint = "install the ViGEmBus driver: https://github.com/nefarius/ViGEmBus/releases"
        with contextlib.suppress(Exception):
            import importlib.util
            spec = importlib.util.find_spec("vgamepad")
            if spec and spec.origin:
                msi = sorted(Path(spec.origin).parent.rglob("ViGEmBus*x64*.msi"))
                if msi:
                    hint = f"install the ViGEmBus driver: run {msi[-1]}"
        return f"{str(error).splitlines()[0] if str(error) else type(error).__name__}; {hint}"

    @property
    def available(self) -> bool:
        return self.vg is not None or self.demo

    def update(self, pad_id, buttons, lt, rt, lx, ly, rx, ry) -> None:
        if self.demo:
            state = (buttons, lt, rt, lx, ly, rx, ry)
            if self.pads.get(pad_id) != state:
                self.pads[pad_id] = state
                log.info("input: gamepad %s buttons=%04x LT=%d RT=%d L=(%d,%d) R=(%d,%d)", pad_id, *state)
            return
        if self.vg is None:
            return
        entry = self.pads.get(pad_id)
        if entry is None:
            if len(self.pads) >= self.MAX_PADS:
                return
            try:
                pad = self.vg.VX360Gamepad()
            except Exception as e:
                self.status = "not available - " + self._explain(e)
                log.warning("Gamepad %s", self.status)
                self.vg = None
                return
            entry = self.pads[pad_id] = {"pad": pad, "buttons": 0, "callback": None}
            try:
                callback = self._rumble_callback(pad_id)
                pad.register_notification(callback_function=callback)
                entry["callback"] = callback       # keep a reference so it is not garbage collected
            except Exception as e:
                log.debug("Rumble not available: %s", e)
            log.info("Virtual Xbox controller connected (%s)", pad_id)
        pad = entry["pad"]
        changed = buttons ^ entry["buttons"]
        for bit in XUSB_BITS:
            if changed & bit:
                button = self.vg.XUSB_BUTTON(bit)
                if buttons & bit:
                    pad.press_button(button=button)
                else:
                    pad.release_button(button=button)
        entry["buttons"] = buttons
        pad.left_trigger(value=lt)
        pad.right_trigger(value=rt)
        pad.left_joystick(x_value=lx, y_value=ly)
        pad.right_joystick(x_value=rx, y_value=ry)
        pad.update()

    def _rumble_callback(self, pad_id):
        def callback(client, target, large_motor, small_motor, led_number, user_data):
            with contextlib.suppress(RuntimeError):            # event loop already closed
                self.loop.call_soon_threadsafe(self.on_rumble, pad_id, int(large_motor), int(small_motor))
        return callback

    def remove(self, pad_id) -> None:
        entry = self.pads.pop(pad_id, None)
        if not entry or self.demo:
            return
        pad = entry["pad"]
        with contextlib.suppress(Exception):
            pad.unregister_notification()
        with contextlib.suppress(Exception):
            pad.reset()
            pad.update()
        log.info("Virtual Xbox controller disconnected (%s)", pad_id)
        del pad, entry                                         # unplugs the virtual controller

    def remove_all(self) -> None:
        for pad_id in list(self.pads):
            self.remove(pad_id)


# ======================================================================================
# Sound: loopback capture of whatever the PC is playing
# ======================================================================================

def downmix_to_stereo(data, np):
    """float32 (frames, channels) -> (frames, 2). Handles mono, stereo, quad, 5.1 and 7.1."""
    ch = data.shape[1]
    if ch == 2:
        return data
    if ch == 1:
        return np.repeat(data, 2, axis=1)
    k = 0.7071
    left, right = data[:, 0].copy(), data[:, 1].copy()
    if ch == 4:                                   # FL FR BL BR
        left += k * data[:, 2]
        right += k * data[:, 3]
    elif ch >= 6:                                 # FL FR FC LFE BL BR [SL SR]
        left += k * data[:, 2] + k * data[:, 4]
        right += k * data[:, 2] + k * data[:, 5]
        if ch >= 8:
            left += k * data[:, 6]
            right += k * data[:, 7]
    return np.stack((left, right), axis=1) * 0.6


class AudioCapture:
    RATE = 48000
    BLOCK = 480                                   # 10 ms

    def __init__(self, loop, on_pcm, demo: bool):
        self.loop = loop
        self.on_pcm = on_pcm
        self.demo = demo
        self._generation = 0
        self.backend = None

    def check(self) -> str:
        """Short status line for the console, without starting a capture."""
        if self.demo:
            return "demo tone"
        import importlib.util
        for module, name in (("soundcard", "soundcard"), ("pyaudiowpatch", "PyAudioWPatch")):
            try:
                found = importlib.util.find_spec(module) and importlib.util.find_spec("numpy")
            except (ImportError, ValueError):
                found = module in sys.modules
            if found:
                return f"ready ({name})"
        return "not available - install it with:  .venv\\Scripts\\python -m pip install numpy soundcard"

    def start(self) -> None:
        self._generation += 1
        threading.Thread(target=self._run, args=(self._generation,), name="audio", daemon=True).start()

    def stop(self) -> None:
        self._generation += 1                     # the running thread notices and exits

    def _alive(self, generation: int) -> bool:
        return generation == self._generation

    def _emit(self, pcm: bytes, rate: int, channels: int, silent: bool = False) -> None:
        """Hand one 10 ms block to the event loop. Every block is reported, silent ones too,
        so the receiver can tell 'the PC went quiet' from 'the network was late'."""
        with contextlib.suppress(RuntimeError):
            self.loop.call_soon_threadsafe(self.on_pcm, pcm, rate, channels, silent)

    def _run(self, generation: int) -> None:
        if self.demo:
            self._run_demo(generation)
            return
        mmcss = Windows.mmcss_begin("Audio")         # keep capturing smoothly under game load
        try:
            errors = []
            for runner in (self._run_soundcard, self._run_pyaudiowpatch):
                try:
                    runner(generation)
                    return
                except ImportError as e:
                    errors.append(str(e))
                except Exception as e:
                    errors.append(f"{type(e).__name__}: {e}")
                    log.debug("Audio backend failed", exc_info=True)
            log.warning("No sound: could not capture audio (%s).", "; ".join(errors))
        finally:
            Windows.mmcss_end(mmcss)

    def _run_soundcard(self, generation: int) -> None:
        import warnings
        import numpy as np
        import soundcard as sc
        warnings.filterwarnings("ignore", module="soundcard")
        while self._alive(generation):
            speaker = sc.default_speaker()
            mic = sc.get_microphone(id=speaker.id, include_loopback=True)
            if self.backend != speaker.name:
                self.backend = speaker.name
                log.info("Sound: capturing '%s'", speaker.name)
            with mic.recorder(samplerate=self.RATE, blocksize=self.BLOCK * 4) as recorder:
                last_check = time.monotonic()
                while self._alive(generation):
                    data = recorder.record(numframes=self.BLOCK)
                    if data.ndim == 1:
                        data = data[:, None]
                    stereo = downmix_to_stereo(data, np)
                    pcm = (np.clip(stereo, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
                    self._emit(pcm, self.RATE, 2, silent=not np.any(stereo))
                    now = time.monotonic()
                    if now - last_check > 3:                  # follow changes of the default output
                        last_check = now
                        if sc.default_speaker().id != speaker.id:
                            break

    def _run_pyaudiowpatch(self, generation: int) -> None:
        import numpy as np
        import pyaudiowpatch as pyaudio
        pa = pyaudio.PyAudio()
        try:
            wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
            device = pa.get_device_info_by_index(wasapi["defaultOutputDevice"])
            if not device.get("isLoopbackDevice"):
                for loopback in pa.get_loopback_device_info_generator():
                    if device["name"] in loopback["name"]:
                        device = loopback
                        break
                else:
                    raise RuntimeError("no loopback device found")
            channels = int(device["maxInputChannels"])
            rate = int(device["defaultSampleRate"])
            frames = max(1, rate // 100)
            log.info("Sound: capturing '%s' (PyAudioWPatch)", device["name"])
            stream = pa.open(format=pyaudio.paInt16, channels=channels, rate=rate, input=True,
                             input_device_index=device["index"], frames_per_buffer=frames)
            try:
                while self._alive(generation):
                    raw = stream.read(frames, exception_on_overflow=False)
                    data = np.frombuffer(raw, dtype="<i2").reshape(-1, channels).astype(np.float32) / 32768.0
                    stereo = downmix_to_stereo(data, np)
                    self._emit((np.clip(stereo, -1, 1) * 32767).astype("<i2").tobytes(), rate, 2,
                               silent=not np.any(stereo))
            finally:
                stream.close()
        finally:
            pa.terminate()

    def _run_demo(self, generation: int) -> None:
        """A short 440 Hz beep every second, paced in real time."""
        rate, block = self.RATE, self.BLOCK
        start, sent = time.monotonic(), 0
        silence = bytes(block * 4)
        while self._alive(generation):
            t0 = sent / rate
            if (t0 % 1.0) < 0.15:
                samples = [int(6000 * math.sin(2 * math.pi * 440 * (sent + i) / rate)) for i in range(block)]
                self._emit(struct.pack(f"<{block * 2}h", *(v for s in samples for v in (s, s))), rate, 2)
            else:
                self._emit(silence, rate, 2, silent=True)
            sent += block
            delay = start + sent / rate - time.monotonic()
            if delay > 0:
                time.sleep(delay)


def wav_stream_header(rate: int, channels: int) -> bytes:
    """A WAV header for an endless 16-bit PCM stream (sizes set to 'unknown')."""
    return (b"RIFF" + struct.pack("<I", 0xFFFFFFFF) + b"WAVE" +
            b"fmt " + struct.pack("<IHHIIHH", 16, 1, channels, rate, rate * channels * 2, channels * 2, 16) +
            b"data" + struct.pack("<I", 0xFFFFFFFF))


def rtp_payload(data: bytes):
    """The payload of an RTP packet, or None for RTCP or anything malformed."""
    if len(data) < 12 or data[0] >> 6 != 2 or (data[1] & 0x7F) < 96:   # not RTP v2 / dynamic type
        return None
    start = 12 + 4 * (data[0] & 0x0F)                     # fixed header + CSRC list
    if data[0] & 0x10:                                    # header extension
        if len(data) < start + 4:
            return None
        start += 4 + 4 * struct.unpack(">H", data[start + 2:start + 4])[0]
    end = len(data) - (data[-1] if data[0] & 0x20 else 0)  # padding
    return bytes(data[start:end]) if end > start else None


class OpusEncoder:
    """Compresses the PC's sound (48 kHz stereo PCM) to Opus with FFmpeg's libopus in
    low-delay mode: ~0.15 instead of ~1.5 Mbit/s, for about 10 ms of extra delay.
    PCM goes in through a pipe; packets come back as RTP on a loopback-only UDP socket,
    which hands over each packet the moment it's encoded (an Ogg pipe would hold one back)."""

    RATE, CHANNELS, BLOCK = 48000, 2, 480                   # 10 ms frames

    def __init__(self, ffmpeg_path: str, loop, on_packet, on_failed, kbps: int = 128):
        self.ffmpeg_path = ffmpeg_path
        self.loop = loop
        self.on_packet = on_packet
        self.on_failed = on_failed
        self.kbps = kbps
        self.proc = None
        self.sock = None
        self.queue: queue.Queue = queue.Queue(maxsize=50)   # ~0.5 s of sound
        self.running = False
        self.fed = 0
        self.packets = 0
        self.tail = collections.deque(maxlen=5)

    def command(self, port: int) -> list[str]:
        return [self.ffmpeg_path, "-hide_banner", "-nostats", "-loglevel", "error",
                "-fflags", "nobuffer", "-probesize", "32", "-analyzeduration", "0",
                "-f", "wav", "-max_size", str(self.BLOCK * self.CHANNELS * 2), "-ignore_length", "1",
                "-i", "pipe:0",
                "-c:a", "libopus", "-application", "lowdelay", "-frame_duration", "10", "-b:a", f"{self.kbps}k",
                # localaddr keeps FFmpeg's own sockets on loopback (no firewall prompt)
                "-f", "rtp", f"rtp://127.0.0.1:{port}?rtcpport={port}&localaddr=127.0.0.1&pkt_size=1400"]

    def start(self) -> bool:
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.bind(("127.0.0.1", 0))
            self.sock.settimeout(0.5)
            self.proc = subprocess.Popen(self.command(self.sock.getsockname()[1]), stdin=subprocess.PIPE,
                                         stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, bufsize=0,
                                         creationflags=NO_WINDOW | ABOVE_NORMAL)
            self.proc.stdin.write(wav_stream_header(self.RATE, self.CHANNELS))
        except (OSError, ValueError) as e:
            log.debug("Opus encoder could not start: %s", e)
            self.stop()
            return False
        Windows.boost_process(self.proc.pid)
        self.running = True
        for target, name in ((self._writer, "opus-in"), (self._reader, "opus-out"), (self._drain, "opus-log")):
            threading.Thread(target=target, name=name, daemon=True).start()
        return True

    def feed(self, pcm: bytes) -> None:
        """Queue 10 ms of 48 kHz stereo s16 PCM. Never blocks; drops if the encoder falls behind."""
        if self.running:
            with contextlib.suppress(queue.Full):
                self.queue.put_nowait(pcm)
                self.fed += 1

    def _writer(self) -> None:
        try:
            while self.running:
                pcm = self.queue.get()
                if pcm is None:
                    return
                self.proc.stdin.write(pcm)
        except (OSError, ValueError):
            self._fail("the encoder stopped taking sound")

    def _reader(self) -> None:
        while self.running:
            try:
                data = self.sock.recv(2048)
            except socket.timeout:
                if self.proc.poll() is not None:
                    self._fail(self.tail[-1] if self.tail else f"FFmpeg exited ({self.proc.returncode})")
                elif self.fed > 300 and self.packets == 0:       # 3 s of sound in, nothing out
                    self._fail("no output")
                continue
            except OSError:
                return
            packet = rtp_payload(data)
            if packet:
                self.packets += 1
                with contextlib.suppress(RuntimeError):          # event loop already closed
                    self.loop.call_soon_threadsafe(self.on_packet, packet)

    def _drain(self) -> None:
        with contextlib.suppress(Exception):
            for line in self.proc.stderr:
                text = line.decode("utf-8", "replace").strip()
                if text:
                    self.tail.append(text)

    def _fail(self, reason: str) -> None:
        if self.running:
            self.running = False
            with contextlib.suppress(RuntimeError):
                self.loop.call_soon_threadsafe(self.on_failed, reason)

    def stop(self) -> None:
        self.running = False
        with contextlib.suppress(Exception):
            self.queue.put_nowait(None)
        if self.proc:
            with contextlib.suppress(Exception):
                self.proc.stdin.close()
            with contextlib.suppress(Exception):
                self.proc.kill()
            with contextlib.suppress(Exception):
                self.proc.wait(timeout=2)
        if self.sock:
            with contextlib.suppress(Exception):
                self.sock.close()


# ======================================================================================
# Video: FFmpeg screen capture + H.264 / HEVC / AV1 encoding, read back as FLV
# ======================================================================================

# Encoder "families" are the hardware/software back-ends; each can encode one or more codecs.
ENCODER_FAMILIES = {"nvenc": "NVIDIA", "amf": "AMD", "qsv": "Intel", "x264": "CPU"}
CODEC_LABEL = {"h264": "H.264", "hevc": "HEVC", "av1": "AV1"}
# Preference order when the browser can decode more than one: newer codecs give a much better
# picture per megabit (HEVC/AV1 ~ half the bitrate of H.264). H.264 is the universal fallback.
CODEC_PREFERENCE = ["av1", "hevc", "h264"]
VENDOR_ENCODER = {0x10DE: "nvenc", 0x1002: "amf", 0x1022: "amf", 0x8086: "qsv"}


def ffmpeg_encoder(family: str, codec: str) -> str | None:
    """The FFmpeg encoder name for a (family, codec) pair, or None if it doesn't exist."""
    if family == "x264":
        return "libx264" if codec == "h264" else None
    return f"{codec}_{family}"                    # h264_nvenc, hevc_nvenc, av1_nvenc, hevc_qsv, ...


def family_label(family: str, codec: str) -> str:
    return f"{ENCODER_FAMILIES.get(family, family)} {CODEC_LABEL.get(codec, codec)}"


class FFmpeg:
    def __init__(self, path: str):
        self.path = path
        self.version = ""
        self.major = None
        self.encoders: set[str] = set()
        self.filters: set[str] = set()

    def inspect(self) -> None:
        def run(*args):
            try:
                r = subprocess.run([self.path, "-hide_banner", *args], capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", timeout=30, creationflags=NO_WINDOW)
                return r.stdout + r.stderr
            except (OSError, subprocess.SubprocessError):
                return ""

        first = (run("-version").splitlines() or [""])[0]
        self.version = first.replace("ffmpeg version ", "").split(" Copyright")[0].strip()
        m = re.match(r"n?(\d+)\.(\d+)", self.version)
        self.major = int(m.group(1)) if m else None
        for line in run("-encoders").splitlines():
            parts = line.split()
            if len(parts) >= 2 and len(parts[0]) == 6:
                self.encoders.add(parts[1])
        for line in run("-filters").splitlines():
            parts = line.split()
            if len(parts) >= 3:
                self.filters.add(parts[1])

    @property
    def fps_mode_args(self) -> list[str]:
        if self.major is not None and self.major < 6:
            return ["-vsync", "passthrough"]
        return ["-fps_mode", "passthrough"]


def find_ffmpeg(explicit: str | None) -> str | None:
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    exe = "ffmpeg.exe" if IS_WINDOWS else "ffmpeg"
    candidates += [BASE_DIR / "ffmpeg" / "bin" / exe, BASE_DIR / "ffmpeg" / exe, BASE_DIR / exe]
    found = shutil.which("ffmpeg")
    if found:
        candidates.append(Path(found))
    if IS_WINDOWS:
        local = os.environ.get("LOCALAPPDATA")
        if local:
            winget = Path(local) / "Microsoft" / "WinGet"
            candidates.append(winget / "Links" / exe)
            candidates += sorted((winget / "Packages").glob("*FFmpeg*/*/bin/" + exe), reverse=True)
        for base in (os.environ.get("ProgramFiles"), "C:\\"):
            if base:
                candidates.append(Path(base) / "ffmpeg" / "bin" / exe)
    for c in candidates:
        with contextlib.suppress(OSError):
            if c.is_file():
                return str(c)
    return None


def scaled_size(native, height: int):
    """Target (w, h) when scaling down to `height`, or None to keep the native size."""
    if not height:
        return None
    if native:
        w, h = native
        if height >= h:
            return None
        return max(2, int(round(w * height / h / 2)) * 2), height
    return -2, height


def filter_variants(enc: str, hw_source: bool, target, filters: set[str]):
    """Possible -vf chains for an encoder, best first: [(label, vf or None)]."""
    cpu_scale = [f"scale={target[0]}:{target[1]}:flags=fast_bilinear"] if target else []
    download = ["hwdownload", "format=bgra"] if hw_source else []
    if enc == "x264":
        return [("cpu", ",".join(download + cpu_scale + ["format=yuv420p"]))]
    variants = []
    if hw_source:
        if enc in ("nvenc", "amf") and not target:
            variants.append(("gpu", None))          # screen texture goes straight into the encoder
        if enc == "amf" and target and "vpp_amf" in filters and target[0] > 0:
            variants.append(("gpu scale", f"vpp_amf=w={target[0]}:h={target[1]}:format=nv12"))
        if enc == "qsv" and "hwmap" in filters:
            if "vpp_qsv" in filters and (not target or target[0] > 0):
                size = f":w={target[0]}:h={target[1]}" if target else ""
                variants.append(("gpu", f"hwmap=derive_device=qsv,format=qsv,vpp_qsv=format=nv12{size}"))
            if not target:
                variants.append(("gpu rgb", "hwmap=derive_device=qsv,format=qsv"))
    variants.append(("cpu copy" if hw_source else "cpu", ",".join(download + cpu_scale + ["format=nv12"])))
    return variants


VBV_SECONDS = 0.05      # ~3 frames at 60 fps


def vbv_kbits(kbps: int, fps: int) -> int:
    """Rate-control (VBV) buffer size. No frame can be bigger than this buffer, so it bounds how
    long one frame takes to send when the link is only about as fast as the bitrate. Keyframes
    are the big ones: with a 1/8 s buffer they could stall the picture for ~125 ms every couple
    of seconds; with ~50 ms they can't, at the cost of a slightly softer keyframe."""
    seconds = max(VBV_SECONDS, 3.0 / max(1, fps))          # never less than ~3 frames
    return max(100, round(kbps * seconds))


def codec_args(family: str, codec: str, profile: str, kbps: int, gop: int, fps: int = 60) -> list[str]:
    rate = f"{kbps}k"
    enc = ffmpeg_encoder(family, codec)
    # -bf 0: no B-frames, so there is no frame reordering and the browser shows each frame at once.
    common = ["-b:v", rate, "-maxrate", rate, "-bufsize", f"{vbv_kbits(kbps, fps)}k",
              "-g", str(gop), "-bf", "0"]
    if family == "nvenc":
        args = ["-c:v", enc, "-preset", "p2", "-tune", "ull", "-rc", "cbr", "-zerolatency", "1",
                "-delay", "0", "-rc-lookahead", "0"]
        if codec == "h264":                       # baseline/main matters only for H.264 playback
            args += ["-profile:v", profile]
        return args + common
    if family == "amf":
        args = ["-c:v", enc, "-usage", "ultralowlatency", "-quality", "speed", "-rc", "cbr"]
        if codec == "h264":
            args += ["-profile:v", "constrained_baseline" if profile == "baseline" else profile]
        return args + common
    if family == "qsv":
        args = ["-c:v", enc, "-preset", "veryfast", "-async_depth", "1"]
        if codec == "h264":
            args += ["-profile:v", profile]
        return args + common
    return ["-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency", "-profile:v", "baseline"] + common


def _reverse32(value: int) -> int:
    out = 0
    for i in range(32):
        out |= ((value >> i) & 1) << (31 - i)
    return out


def codec_config_string(codec: str, record: bytes) -> str:
    """Turn a decoder config record (from the stream) into the exact WebCodecs codec string."""
    try:
        if codec == "h264":                       # avcC: [1]=profile [2]=compat [3]=level
            return "avc1.%02x%02x%02x" % (record[1], record[2], record[3])
        if codec == "hevc":                       # hvcC (ISO 14496-15 §8.3.3.1)
            space = (record[1] >> 6) & 0x3
            tier = (record[1] >> 5) & 0x1
            pidc = record[1] & 0x1F
            compat = _reverse32(int.from_bytes(record[2:6], "big"))
            level = record[12]
            s = "hvc1.%s%d.%X.%s%d" % (["", "A", "B", "C"][space], pidc, compat, "LH"[tier], level)
            constraint = bytes(record[6:12]).rstrip(b"\x00")
            if constraint:
                s += "." + ".".join("%02X" % b for b in constraint)
            return s
        if codec == "av1":                        # av1C (AV1 ISOBMFF §2.3.3)
            profile = (record[1] >> 5) & 0x7
            level = record[1] & 0x1F
            tier = (record[2] >> 7) & 0x1
            depth = 12 if (record[2] >> 5) & 0x1 else (10 if (record[2] >> 6) & 0x1 else 8)
            return "av01.%d.%02d%s.%02d" % (profile, level, "MH"[tier], depth)
    except (IndexError, ValueError):
        pass
    return {"h264": "avc1.42e01f", "hevc": "hvc1.1.6.L93.B0", "av1": "av01.0.08M.08"}[codec]


FOURCC_CODEC = {b"avc1": "h264", b"hvc1": "hevc", b"hev1": "hevc", b"av01": "av1"}


def parse_flv_video(body: bytes):
    """Parse one FLV video tag body.
    Returns ("config", codec, record), ("frame", codec, is_key, payload) or None.
    Handles the classic AVC layout and the 'Enhanced RTMP' layout (H.264, HEVC and AV1)."""
    if len(body) < 5:
        return None
    b0 = body[0]
    if b0 & 0x80:                                         # Enhanced RTMP: [flags][fourcc]...
        frame_type, packet_type = (b0 >> 4) & 0x07, b0 & 0x0F
        codec = FOURCC_CODEC.get(bytes(body[1:5]))
        if not codec:
            return None
        if packet_type == 0:                              # sequence start = decoder config record
            return ("config", codec, bytes(body[5:]))
        if packet_type == 1 and len(body) > 8:            # CodedFrames, with 3-byte composition time
            return ("frame", codec, frame_type == 1, memoryview(body)[8:])
        if packet_type == 3:                              # CodedFramesX, no composition time
            return ("frame", codec, frame_type == 1, memoryview(body)[5:])
        return None
    if b0 & 0x0F != 7:                                    # classic FLV, 7 = AVC
        return None
    frame_type, packet_type = b0 >> 4, body[1]
    if packet_type == 0:
        return ("config", "h264", bytes(body[5:]))
    if packet_type == 1:
        return ("frame", "h264", frame_type == 1, memoryview(body)[5:])
    return None


class VideoPipeline:
    """Runs FFmpeg for the connected browser, restarts it when settings change or it stops,
    and picks the first encoder setup that works on this PC."""

    def __init__(self, app, ff: FFmpeg, demo: bool, gop_seconds: float,
                 forced_family: str | None, forced_codec: str | None):
        self.app = app
        self.ff = ff
        self.demo = demo
        self.gop_seconds = gop_seconds
        self.forced_family = forced_family
        self.forced_codec = forced_codec
        self.settings: StreamSettings | None = None
        self.task: asyncio.Task | None = None
        self.restart_event = asyncio.Event()
        self.started_event = asyncio.Event()
        self.working: dict = {}
        self.preferred_family: str | None = None
        self.last_error = ""
        self.info: dict = {}
        if demo:
            self.source = "testsrc"
        elif "ddagrab" in ff.filters:
            self.source = "ddagrab"
        else:
            self.source = "gdigrab"

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    def start(self, settings: StreamSettings) -> None:
        self.settings = settings
        if self.running:
            self.restart_event.set()
        else:
            self.restart_event.clear()
            self.started_event.clear()
            self.task = asyncio.create_task(self._supervise(), name="video")

    def restart(self) -> None:
        if self.running:
            self.restart_event.set()

    async def stop(self) -> None:
        task, self.task = self.task, None
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def family_order(self) -> list[str]:
        if self.demo:
            return ["x264"]
        if self.forced_family:
            return [self.forced_family]
        order = [e for e in (self.preferred_family, VENDOR_ENCODER.get(self.app.gpu_vendor)) if e]
        order += ["nvenc", "amf", "qsv", "x264"]
        return list(dict.fromkeys(order))

    def codec_order(self, s: StreamSettings) -> list[str]:
        """Codecs to try, best first, limited to what the browser can decode and we can encode."""
        if self.demo:
            return ["h264"]
        caps = self.app.codec_caps or {"h264"}
        if self.forced_codec:
            pref = [self.forced_codec]
        elif s and s.codec != "auto":
            pref = [s.codec, "h264"]              # user picked one, but keep H.264 as a safety net
        else:
            pref = list(CODEC_PREFERENCE)
        if not self.forced_codec and "h264" not in pref:
            pref.append("h264")
        have = lambda c: any(ffmpeg_encoder(f, c) in self.ff.encoders for f in self.family_order())
        return [c for c in dict.fromkeys(pref) if c in caps and have(c)]

    def native_size(self, monitor: int):
        if self.demo:
            return DEMO_SIZE
        monitors = self.app.monitors
        if 0 <= monitor < len(monitors):
            return monitors[monitor][2], monitors[monitor][3]
        return None

    def attempts(self, s: StreamSettings):
        native = self.native_size(s.monitor)
        target = scaled_size(native, s.height)
        hw = self.source == "ddagrab"
        codecs = self.codec_order(s)
        result = []
        cached = self.working.get((s.monitor, target))
        if cached and cached[1] in codecs:        # cached[1] = codec; skip if no longer allowed
            result.append(cached)
        for codec in codecs:
            for family in self.family_order():
                if ffmpeg_encoder(family, codec) not in self.ff.encoders:
                    continue
                profiles = ("baseline", "main") if (codec == "h264" and family != "x264") else ("-",)
                for label, vf in filter_variants(family, hw, target, self.ff.filters):
                    for profile in profiles:
                        attempt = (family, codec, label, vf, profile)
                        if attempt not in result:
                            result.append(attempt)
        return result, target, native

    def command(self, s: StreamSettings, family: str, codec: str, vf, profile: str) -> list[str]:
        gop = max(1, round(s.fps * self.gop_seconds))
        cmd = [self.ff.path, "-hide_banner", "-nostdin", "-nostats", "-loglevel", "warning"]
        if self.source == "testsrc":
            w, h = DEMO_SIZE
            cmd += ["-re", "-f", "lavfi", "-i", f"testsrc2=size={w}x{h}:rate={s.fps}"]
        elif self.source == "ddagrab":
            cmd += ["-f", "lavfi", "-i",
                    f"ddagrab=output_idx={s.monitor}:framerate={s.fps}:draw_mouse={int(s.cursor)}"]
        else:
            cmd += ["-f", "gdigrab", "-framerate", str(s.fps), "-draw_mouse", str(int(s.cursor)), "-i", "desktop"]
        if vf:
            cmd += ["-vf", vf]
        cmd += codec_args(family, codec, profile, s.kbps, gop, s.fps)
        cmd += self.ff.fps_mode_args
        cmd += ["-an", "-sn", "-dn", "-f", "flv", "-flvflags", "no_duration_filesize",
                "-flush_packets", "1", "pipe:1"]
        return cmd

    async def _supervise(self) -> None:
        retry_delay = 1.0
        while True:
            self.restart_event.clear()
            s = self.settings
            self.app.refresh_monitors()
            attempts, target, native = self.attempts(s)
            started = None
            for i, attempt in enumerate(attempts):
                result = await self._try(s, attempt, timeout=12 if i == 0 else 8)
                if result:
                    started = (attempt, result)
                    break
                if self.restart_event.is_set():
                    break
                log.debug("Encoder setup %s failed: %s", attempt[:2] + attempt[3:], self.last_error)
            if not started:
                if self.restart_event.is_set():
                    continue
                msg = "Couldn't start the screen capture. If the PC is locked or shows a UAC prompt, unlock it."
                log.warning("%s (FFmpeg said: %s)", msg, self.last_error or "nothing")
                self.app.on_video_error(msg)
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self.restart_event.wait(), retry_delay)
                retry_delay = min(retry_delay * 2, 15)
                continue
            retry_delay = 1.0
            (family, codec, label, vf, profile), (proc, reader, err_task, tail) = started
            self.working[(s.monitor, target)] = started[0]
            self.preferred_family = family
            size = target if target and target[0] > 0 else native
            self.info = {
                "encoder": family_label(family, codec), "codec": codec, "path": label,
                "width": size[0] if size else 0, "height": size[1] if size else s.height,
                "fps": s.fps, "kbps": s.kbps, "auto": s.auto,
            }
            size_text = f"{size[0]}x{size[1]}" if size else f"{s.height}p" if s.height else "native size"
            log.info("Video: %s, %d fps, %.1f Mbit/s%s, %s (%s)", size_text, s.fps, s.kbps / 1000,
                     " auto" if s.auto else "", family_label(family, codec), label)
            self.started_event.set()
            self.app.on_video_started(self.info)
            restart_wait = asyncio.create_task(self.restart_event.wait())
            try:
                await asyncio.wait({reader, restart_wait}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                restart_wait.cancel()
                await self._kill(proc)
                reader.cancel()
                err_task.cancel()
            if not self.restart_event.is_set():
                log.warning("Screen capture stopped (%s); restarting it.", tail[-1] if tail else "FFmpeg exited")
                self.app.on_video_error("Screen capture restarted")
                await asyncio.sleep(0.5)

    async def _try(self, s: StreamSettings, attempt, timeout: float):
        family, codec, label, vf, profile = attempt
        cmd = self.command(s, family, codec, vf, profile)
        log.debug("Starting: %s", subprocess.list2cmdline(cmd))
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, creationflags=NO_WINDOW | ABOVE_NORMAL)
        except OSError as e:
            self.last_error = f"could not run FFmpeg: {e}"
            return None
        Windows.boost_process(proc.pid)                   # don't let Windows throttle the encoder
        tail = collections.deque(maxlen=12)
        err_task = asyncio.create_task(self._drain(proc.stderr, tail))
        first_keyframe = asyncio.get_running_loop().create_future()
        reader = asyncio.create_task(self._read(proc.stdout, first_keyframe))
        ok = False
        restart_wait = asyncio.create_task(self.restart_event.wait())
        try:
            await asyncio.wait({first_keyframe, restart_wait}, timeout=timeout,
                               return_when=asyncio.FIRST_COMPLETED)
            ok = first_keyframe.done() and first_keyframe.result() is True
        finally:
            restart_wait.cancel()
            if not ok:
                await self._kill(proc)
                reader.cancel()
                err_task.cancel()
                self.last_error = tail[-1] if tail else "no output"
        return (proc, reader, err_task, tail) if ok else None

    @staticmethod
    async def _drain(stream, tail) -> None:
        with contextlib.suppress(Exception):
            while True:
                line = await stream.readline()
                if not line:
                    return
                text = line.decode("utf-8", "replace").strip()
                if text:
                    tail.append(text)
                    log.debug("ffmpeg: %s", text)

    async def _read(self, stream, first_keyframe) -> None:
        try:
            header = await stream.readexactly(9)
            if header[:3] != b"FLV":
                raise ValueError("FFmpeg did not produce FLV")
            skip = struct.unpack(">I", header[5:9])[0] - 9
            if skip > 0:
                await stream.readexactly(skip)
            await stream.readexactly(4)                               # PreviousTagSize0
            while True:
                tag = await stream.readexactly(11)
                size = (tag[1] << 16) | (tag[2] << 8) | tag[3]
                ts = (tag[7] << 24) | (tag[4] << 16) | (tag[5] << 8) | tag[6]
                body = await stream.readexactly(size) if size else b""
                await stream.readexactly(4)                           # PreviousTagSize
                if tag[0] & 0x1F != 9:
                    continue
                parsed = parse_flv_video(body)
                if parsed is None:
                    continue
                if parsed[0] == "config":
                    self.app.on_video_config(parsed[1], parsed[2])        # codec, record
                else:
                    _, codec, key, payload = parsed
                    if key and not first_keyframe.done():
                        first_keyframe.set_result(True)
                    self.app.on_video_frame(codec, key, ts, payload)
        except asyncio.IncompleteReadError:
            pass                                                      # FFmpeg exited
        except Exception as e:
            log.warning("Video stream error: %s", e)
        finally:
            if not first_keyframe.done():
                first_keyframe.set_result(False)

    @staticmethod
    async def _kill(proc) -> None:
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(proc.wait(), 5)


# ======================================================================================
# Minimal HTTP + WebSocket server (standard library only)
# ======================================================================================

class WebSocketClosed(Exception):
    pass


class WebSocketProtocolError(Exception):
    pass


def _unmask(data: bytes, mask: bytes) -> bytes:
    n = len(data)
    key = (mask * (n // 4 + 1))[:n]
    return (int.from_bytes(data, "little") ^ int.from_bytes(key, "little")).to_bytes(n, "little")


class WebSocket:
    MAX_MESSAGE = 1 << 20

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.reader = reader
        self.writer = writer
        self.closed = False

    async def send_frame(self, opcode: int, parts) -> None:
        if self.closed:
            raise WebSocketClosed()
        n = sum(len(p) for p in parts)
        if n < 126:
            header = bytes((0x80 | opcode, n))
        elif n < 65536:
            header = struct.pack("!BBH", 0x80 | opcode, 126, n)
        else:
            header = struct.pack("!BBQ", 0x80 | opcode, 127, n)
        # One writelines() call per frame: header and payload leave together (a single
        # scatter-gather send where the transport supports it) instead of a tiny header
        # packet first; and frames from different tasks can never interleave.
        self.writer.writelines([header, *parts])
        await self.writer.drain()

    async def recv(self):
        """Next data message as (opcode, payload), or None when the browser closes."""
        message_op, fragments = None, []
        while True:
            b0, b1 = await self.reader.readexactly(2)
            opcode, n = b0 & 0x0F, b1 & 0x7F
            if b0 & 0x70 or not b1 & 0x80:
                raise WebSocketProtocolError("unexpected frame flags")
            if n == 126:
                n = struct.unpack("!H", await self.reader.readexactly(2))[0]
            elif n == 127:
                n = struct.unpack("!Q", await self.reader.readexactly(8))[0]
            if n > self.MAX_MESSAGE:
                raise WebSocketProtocolError("message too large")
            mask = await self.reader.readexactly(4)
            data = _unmask(await self.reader.readexactly(n), mask) if n else b""
            if opcode == 0x8:                                         # close
                await self.close()
                return None
            if opcode == 0x9:                                         # ping
                await self.send_frame(0xA, [data])
                continue
            if opcode == 0xA:                                         # pong
                continue
            if opcode == 0x0:
                if message_op is None:
                    raise WebSocketProtocolError("unexpected continuation")
                fragments.append(data)
            else:
                if message_op is not None:
                    raise WebSocketProtocolError("interleaved message")
                message_op, fragments = opcode, [data]
            if sum(len(f) for f in fragments) > self.MAX_MESSAGE:
                raise WebSocketProtocolError("message too large")
            if b0 & 0x80:
                return message_op, b"".join(fragments)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        if self.closed:
            return
        with contextlib.suppress(Exception):
            await asyncio.wait_for(
                self.send_frame(0x8, [struct.pack("!H", code) + reason.encode("utf-8")[:120]]), 2)
        self.abort()

    def abort(self) -> None:
        self.closed = True
        with contextlib.suppress(Exception):
            self.writer.close()


CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png",
    ".webmanifest": "application/manifest+json", ".json": "application/json", ".ico": "image/x-icon",
}
HTTP_REASONS = {200: "OK", 400: "Bad Request", 403: "Forbidden", 404: "Not Found",
                405: "Method Not Allowed"}


async def http_response(writer, status: int, body: bytes = b"", content_type="text/plain; charset=utf-8",
                        length: int | None = None, extra: dict | None = None) -> None:
    lines = [f"HTTP/1.1 {status} {HTTP_REASONS.get(status, 'OK')}",
             f"Content-Type: {content_type}",
             f"Content-Length: {len(body) if length is None else length}",
             "Connection: close", "X-Content-Type-Options: nosniff",
             "Referrer-Policy: no-referrer", "X-Frame-Options: DENY"]
    lines += [f"{k}: {v}" for k, v in (extra or {}).items()]
    writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body)
    await writer.drain()


def parse_request(head: bytes):
    lines = head.decode("latin-1").split("\r\n")
    parts = lines[0].split(" ")
    if len(parts) != 3 or not parts[2].startswith("HTTP/1."):
        return None
    headers = {}
    for line in lines[1:]:
        name, sep, value = line.partition(":")
        if sep:
            headers[name.strip().lower()] = value.strip()
    path = urllib.parse.urlsplit(parts[1]).path or "/"
    return parts[0].upper(), path, headers


def host_port(value: str, default_port: int):
    """'Name', 'name:8080' or '[::1]:8080' -> (lower-case name, port), or None if it makes no sense."""
    try:
        parts = urllib.parse.urlsplit("//" + value.strip().lower())
        name, port = parts.hostname, parts.port
    except ValueError:
        return None
    if not name or parts.username is not None or parts.path or parts.query or parts.fragment:
        return None
    return name, port or default_port


# ======================================================================================
# One connected browser
# ======================================================================================

class LoginGuard:
    """After 6 wrong passwords within 10 minutes, every login is refused for 2 minutes."""

    def __init__(self):
        self.failures = collections.deque()
        self.locked_until = 0.0

    def locked(self) -> bool:
        return time.monotonic() < self.locked_until

    def failed(self) -> None:
        now = time.monotonic()
        self.failures.append(now)
        while self.failures and now - self.failures[0] > 600:
            self.failures.popleft()
        if len(self.failures) >= 6:
            self.failures.clear()
            self.locked_until = now + 120
            log.warning("Too many wrong passwords - logins are paused for 2 minutes.")

    def succeeded(self) -> None:
        self.failures.clear()


class AutoBitrate:
    """Picks the encode bitrate to match the connection: backs off fast when frames start
    dropping or the round-trip climbs, then creeps back up while things stay clean.
    One step at a time, with a cooldown, so it doesn't flap."""

    def __init__(self, settings: StreamSettings):
        self.reset(settings)

    def reset(self, settings: StreamSettings) -> None:
        self.ceiling = settings.kbps
        self.floor = max(800, min(3000, self.ceiling // 5))
        # Start a little below the ceiling and climb, so a limited link isn't flooded up front.
        self.target = max(self.floor, min(self.ceiling, int(self.ceiling * 0.7)))
        self.clean = 0
        self.hold_until = 0.0

    def update(self, drop: int, rtt_ms: float, now: float) -> int | None:
        """New target bitrate (kbps) if it should change, else None."""
        if now < self.hold_until:
            return None
        if drop >= 3 or rtt_ms > 350:                         # link is struggling: back off
            self.clean = 0
            new = max(self.floor, int(self.target * 0.6))
            if new < self.target * 0.92:
                self.target = new
                self.hold_until = now + 8                      # settle before trying to climb again
                return new
            return None
        if drop == 0 and rtt_ms < 150:                        # clean window
            self.clean += 1
            if self.clean >= 3 and self.target < self.ceiling:
                new = min(self.ceiling, int(self.target * 1.25) + 200)
                if new > self.target * 1.08:
                    self.target = new
                    self.hold_until = now + 5
                    self.clean = 1
                    return new
        else:
            self.clean = 0
        return None


class Session:
    """The browser that is currently playing. Only one at a time; a new login replaces it."""

    def __init__(self, app: "HostApp", ws: WebSocket, hello: dict, who: str):
        self.app = app
        self.ws = ws
        self.name = f"{describe_device(str(hello.get('ua', '')))} from {who}"
        self.settings = StreamSettings.from_client(hello.get("cfg") or {}, app.default_settings)
        self.alive = True
        self.finished = asyncio.Event()
        self.out: asyncio.Queue = asyncio.Queue()
        self.inflight: collections.OrderedDict[int, float] = collections.OrderedDict()
        self.seq = 0
        self.waiting_for_keyframe = True
        self.dropped = 0
        self.dropped_reported = 0
        self.rtt_now = self.rtt_before = self.min_rtt = 0.08
        self.rtt_window_start = time.monotonic()
        self.last_keyframe_request = 0.0
        self.pad_ids: set[str] = set()
        self.input_error_logged = False
        self.active_codec = "h264"
        caps = {"h264"}                            # H.264 is assumed; HEVC/AV1 only if advertised
        hc = hello.get("codecs")
        if isinstance(hc, dict):
            for c in ("hevc", "av1"):
                if hc.get(c):
                    caps.add(c)
        self.codec_caps = caps
        self.auto = AutoBitrate(self.settings)
        # Sound: Opus if the browser can decode it (and the user didn't ask for raw), else PCM.
        audio_caps = hello.get("audio") if isinstance(hello.get("audio"), dict) else {}
        self.can_opus = bool(audio_caps.get("opus"))
        self.audio_codec = "opus" if (self.can_opus and not audio_caps.get("raw") and app.opus_ok) else "pcm"
        self.opus_seq = 0

    # ---- outgoing -------------------------------------------------------------------
    def send_json(self, obj) -> None:
        if self.alive:
            self.out.put_nowait((0x1, [json.dumps(obj, separators=(",", ":")).encode("utf-8")]))

    def send_binary(self, *parts) -> None:
        if self.alive:
            self.out.put_nowait((0x2, parts))

    async def _sender(self) -> None:
        try:
            while True:
                item = await self.out.get()
                if item is None:
                    return
                await self.ws.send_frame(*item)
        except (ConnectionError, OSError, WebSocketClosed):
            pass
        finally:
            self.alive = False
            self.ws.abort()                               # wakes up the receive loop

    # ---- video ----------------------------------------------------------------------
    def on_video_config(self, codec: str, record: bytes) -> None:
        self.waiting_for_keyframe = True
        self.active_codec = codec
        codec_str = codec_config_string(codec, record).encode("ascii", "replace")[:60]
        # [MSG_VIDEO_CONFIG][codec-string length][codec string][decoder config record]
        self.send_binary(bytes((MSG_VIDEO_CONFIG, len(codec_str))), codec_str, record)

    def _congested(self, now: float, key: bool) -> bool:
        if self.out.qsize() > 12:
            return True
        if not self.inflight:
            return False
        oldest = next(iter(self.inflight.values()))
        if now - oldest > 3.0:
            # These acks never came back (browser was frozen or busy). Forget them instead of
            # stalling forever; the queue-size check above still guards against a dead browser.
            self.inflight.clear()
            return False
        limit = min(1.5, max(0.25, 3 * self.min_rtt + 0.1))
        return now - oldest > (limit * 1.5 if key else limit)

    def on_video_frame(self, codec: str, key: bool, ts: int, payload) -> None:
        if not self.alive:
            return
        now = time.monotonic()
        if self.waiting_for_keyframe and not key:
            self.dropped += 1
            return
        if self._congested(now, key):
            # The connection can't keep up: skip frames until the next keyframe instead of
            # letting old frames pile up (which would add seconds of delay).
            self.dropped += 1
            self.waiting_for_keyframe = True
            return
        self.waiting_for_keyframe = False
        self.seq += 1
        self.inflight[self.seq] = now
        if len(self.inflight) > 600:
            self.inflight.popitem(last=False)
        header = struct.pack("<BBII", MSG_VIDEO_FRAME, 1 if key else 0, self.seq & 0xFFFFFFFF, ts & 0xFFFFFFFF)
        self.send_binary(header, payload)

    def on_ack(self, seq: int) -> None:
        now, sent_at = time.monotonic(), None
        while self.inflight:
            first = next(iter(self.inflight))
            if first > seq:
                break
            t = self.inflight.pop(first)
            if first == seq:
                sent_at = t
        if sent_at is None:
            return
        rtt = now - sent_at
        if now - self.rtt_window_start > 5:
            self.rtt_before, self.rtt_now, self.rtt_window_start = self.rtt_now, rtt, now
        else:
            self.rtt_now = min(self.rtt_now, rtt)
        self.min_rtt = min(self.rtt_now, self.rtt_before)

    def on_audio(self, pcm: bytes, rate: int, channels: int, seq: int = 0) -> None:
        if self.alive and self.out.qsize() < 40:
            self.send_binary(struct.pack("<BBHI", MSG_AUDIO, channels, seq & 0xFFFF, rate), pcm)

    def on_opus(self, packet: bytes) -> None:
        self.opus_seq = (self.opus_seq + 1) & 0xFFFF
        if self.alive and self.out.qsize() < 40:
            self.send_binary(struct.pack("<BBHI", MSG_AUDIO_OPUS, OpusEncoder.CHANNELS, self.opus_seq,
                                         OpusEncoder.RATE), packet)

    # ---- incoming -------------------------------------------------------------------
    def handle(self, m) -> None:
        if not isinstance(m, list) or not m:
            return
        t, inp = m[0], self.app.input
        try:
            if t == "m":
                inp.move_relative(clamp_int(m[1], -10000, 10000), clamp_int(m[2], -10000, 10000))
            elif t == "a":
                inp.move_absolute(clamp_int(m[1], 0, 65535), clamp_int(m[2], 0, 65535),
                                  self.app.monitor_rect(self.settings.monitor))
            elif t == "b":
                inp.button(clamp_int(m[1], 0, 4), bool(m[2]))
            elif t == "w":
                inp.wheel(clamp_int(m[1], -12000, 12000), clamp_int(m[2], -12000, 12000))
            elif t == "k":
                if isinstance(m[1], str) and len(m[1]) <= 32:
                    inp.key(m[1], bool(m[2]))
            elif t == "t":
                if isinstance(m[1], str):
                    inp.text(m[1][:500])
            elif t == "g":
                pad_id = str(m[1])[:16]
                self.pad_ids.add(pad_id)
                self.app.pads.update(pad_id, clamp_int(m[2], 0, 0xFFFF), clamp_int(m[3], 0, 255),
                                     clamp_int(m[4], 0, 255), *(clamp_int(v, -32768, 32767) for v in m[5:9]))
            elif t == "gx":
                self.pad_ids.discard(str(m[1])[:16])
                self.app.pads.remove(str(m[1])[:16])
            elif t == "ack":
                self.on_ack(int(m[1]))
            elif t == "ping":
                self.send_json({"t": "pong", "v": m[1] if isinstance(m[1], (int, float)) else 0})
            elif t == "rel":
                inp.release_all()
            elif t == "cfg":
                self.apply_settings(m[1] if isinstance(m[1], dict) else {})
            elif t == "kf":
                now = time.monotonic()
                if now - self.last_keyframe_request > 2:
                    self.last_keyframe_request = now
                    self.app.video.restart()
            elif t == "nocodec":                  # the browser can't decode this codec after all
                codec = m[1] if isinstance(m[1], str) else ""
                if codec in ("hevc", "av1") and codec in self.codec_caps:
                    self.codec_caps.discard(codec)
                    self.app.codec_caps = self.codec_caps
                    log.warning("Browser could not decode %s; falling back.", codec.upper())
                    self.app.video.start(self.live_settings())
            elif t == "clip":                     # browser sends text to put on the PC clipboard
                if isinstance(m[1], str):
                    self.app.set_clipboard(m[1][:100000])
            elif t == "audio":                    # browser picks the sound format: "opus" or "pcm"
                if len(m) > 2 and m[2] == "failed":
                    self.can_opus = False         # it couldn't decode Opus after all: don't try again
                self.app.set_audio_codec(self, "opus" if m[1] == "opus" else "pcm")
        except (IndexError, TypeError, ValueError, OverflowError) as e:
            log.debug("Ignored bad message %r: %s", m, e)
        except Exception as e:                    # a failing input device must not end the session
            if not self.input_error_logged:
                self.input_error_logged = True
                log.warning("Input error (%s): %s", t, e)

    def live_settings(self) -> StreamSettings:
        """What the encoder should actually run at: the user's settings, but with the
        auto-controller's current bitrate when Auto is on."""
        if self.settings.auto:
            return replace(self.settings, kbps=self.auto.target)
        return self.settings

    def apply_settings(self, d: dict) -> None:
        new = StreamSettings.from_client(d, self.settings)
        if new != self.settings:
            ceiling_changed = new.kbps != self.settings.kbps or new.auto != self.settings.auto
            self.settings = new
            if ceiling_changed:
                self.auto.reset(new)
            self.app.video.start(self.live_settings())

    # ---- lifecycle ------------------------------------------------------------------
    async def _stats_loop(self) -> None:
        while self.alive:
            await asyncio.sleep(2)
            dropped, self.dropped_reported = self.dropped - self.dropped_reported, self.dropped
            rtt_ms = round(self.min_rtt * 1000)
            self.send_json({"t": "st", "drop": dropped, "rtt": rtt_ms})
            if self.settings.auto and self.alive:
                new_kbps = self.auto.update(dropped, rtt_ms, time.monotonic())
                if new_kbps is not None:
                    log.info("Auto bitrate -> %.1f Mbit/s", new_kbps / 1000)
                    self.app.video.start(self.live_settings())

    async def run(self) -> None:
        sender = asyncio.create_task(self._sender())
        stats = asyncio.create_task(self._stats_loop())
        try:
            self.send_json(self.app.welcome(self))
            await self.app.start_streaming(self.live_settings())
            while self.alive:
                message = await self.ws.recv()
                if message is None:
                    break
                if message[0] != 0x1:
                    continue
                try:
                    self.handle(json.loads(message[1]))
                except ValueError:
                    continue
        except (asyncio.IncompleteReadError, ConnectionError, OSError, WebSocketClosed, WebSocketProtocolError):
            pass
        finally:
            self.alive = False
            stats.cancel()
            self.out.put_nowait(None)
            self.app.input.release_all()
            for pad_id in self.pad_ids:
                self.app.pads.remove(pad_id)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(sender, 2)
            sender.cancel()
            await self.ws.close()
            self.finished.set()

    async def kick(self, reason: str) -> None:
        await self.ws.close(CLOSE_REPLACED, reason)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self.finished.wait(), 5)


# ======================================================================================
# The host application
# ======================================================================================

class HostApp:
    MAX_PENDING_LOGINS = 16     # connections that have not logged in yet
    HELLO_MAX_BYTES = 16384     # the first message (the login) is small

    def __init__(self, args, password: str, ff: FFmpeg):
        self.args = args
        self.password = password
        self.ff = ff
        self.loop = asyncio.get_running_loop()
        self.guard = LoginGuard()
        self.pending_logins = 0
        # Extra host names a browser may use to reach this server (--allow-host, plus the Tailscale address).
        self.extra_hosts = {h.strip().lower() for h in (getattr(args, "allow_host", None) or []) if h.strip()}
        # Names are only checked when the server listens on this PC alone (the default, behind Tailscale).
        # With --lan or --bind the addresses people use are not known in advance.
        self.check_host = not args.lan and (args.bind or "127.0.0.1") in ("127.0.0.1", "localhost", "::1")
        self.own_names = {"localhost", socket.gethostname().lower()}
        self.gpu_name, self.gpu_vendor, self.monitors = Windows.display_info()
        self.input = WindowsInput() if (IS_WINDOWS and not args.demo) else LoggedInput()
        self.pads = GamepadHub(not args.no_gamepad, args.demo, self.loop, self._on_rumble)
        self.audio = None if args.no_audio else AudioCapture(self.loop, self._on_audio, args.demo)
        self.default_settings = StreamSettings(fps=args.fps, kbps=int(args.bitrate * 1000),
                                               monitor=args.monitor, cursor=True,
                                               codec=args.codec, auto=bool(getattr(args, "auto", False)))
        forced_family = None if args.encoder == "auto" else args.encoder
        forced_codec = None if args.codec == "auto" else args.codec
        self.codec_caps: set[str] = {"h264"}      # which codecs the connected browser can decode
        self.opus_ok = "libopus" in ff.encoders and not args.no_audio   # can we compress sound?
        self.opus: OpusEncoder | None = None
        self.audio_seq = 0
        self.video = VideoPipeline(self, ff, args.demo, args.gop, forced_family, forced_codec)
        self.session: Session | None = None
        self.session_lock = asyncio.Lock()
        self._clip_last = ""
        self._stopping = False
        if IS_WINDOWS and not args.demo and not args.no_clipboard:
            threading.Thread(target=self._clipboard_watch, name="clipboard", daemon=True).start()

    # ---- callbacks from video / audio / gamepads ------------------------------------
    def on_video_config(self, codec, record):
        if self.session:
            self.session.on_video_config(codec, record)

    def on_video_frame(self, codec, key, ts, payload):
        if self.session:
            self.session.on_video_frame(codec, key, ts, payload)

    def on_video_started(self, info):
        if self.session:
            self.session.send_json({"t": "stream", **info})

    def on_video_error(self, message):
        if self.session:
            self.session.send_json({"t": "status", "msg": message})

    def _on_audio(self, pcm, rate, channels, silent=False):
        self.audio_seq = (self.audio_seq + 1) & 0xFFFF     # counts every 10 ms block, silent or not
        s = self.session
        if not s:
            return
        if (s.audio_codec == "opus" and self.opus and self.opus.running
                and rate == OpusEncoder.RATE and channels == OpusEncoder.CHANNELS):
            # Silence goes in too: Opus shrinks it to ~20 bytes and the stream stays
            # continuous, so the browser can tell network jitter from a quiet game.
            self.opus.feed(pcm)                    # packets come back through _on_opus
        elif not silent:                           # raw sound: silence isn't sent; the block
            s.on_audio(pcm, rate, channels, self.audio_seq)   # counter shows the gap instead

    # ---- Opus sound ------------------------------------------------------------------
    def _on_opus(self, packet: bytes) -> None:
        if self.session and self.session.audio_codec == "opus":
            self.session.on_opus(packet)

    def _on_opus_failed(self, reason: str) -> None:
        log.warning("Sound compression stopped (%s); sending uncompressed sound instead.", reason)
        self._stop_opus()
        self.opus_ok = False
        if self.session:
            self.session.audio_codec = "pcm"

    def _start_opus(self) -> None:
        if self.opus or not self.opus_ok:
            return
        encoder = OpusEncoder(self.ff.path, self.loop, self._on_opus, self._on_opus_failed)
        if encoder.start():
            self.opus = encoder
            log.info("Sound: Opus, %d kbit/s", encoder.kbps)
        else:
            self.opus_ok = False
            log.warning("Sound compression isn't available; sending uncompressed sound.")

    def _stop_opus(self) -> None:
        encoder, self.opus = self.opus, None
        if encoder:
            encoder.stop()

    def set_audio_codec(self, session, codec: str) -> None:
        """Switch a session between Opus and raw PCM sound (on request, or after a failure)."""
        want = codec == "opus" and session.can_opus and self.opus_ok
        session.audio_codec = "opus" if want else "pcm"
        if session is self.session:
            if want and self.audio:
                self._start_opus()
            elif not want:
                self._stop_opus()

    def _on_rumble(self, pad_id, large, small):
        if self.session:
            self.session.send_json({"t": "rumble", "id": pad_id, "l": large, "s": small})

    # ---- clipboard sync -------------------------------------------------------------
    def set_clipboard(self, text: str) -> None:
        """Put text the browser sent onto the PC's clipboard (browser -> PC)."""
        if not IS_WINDOWS or self.args.no_clipboard or self.args.demo:
            return
        self._clip_last = text
        threading.Thread(target=lambda: Windows.set_clipboard_text(text) and None,
                         daemon=True).start()

    def _clipboard_watch(self) -> None:
        """Watch the PC's clipboard and push changes to the browser (PC -> browser)."""
        seq = Windows.clipboard_sequence()
        while not self._stopping:
            time.sleep(1.0)
            try:
                current = Windows.clipboard_sequence()
                if current == seq:
                    continue
                seq = current
                text = Windows.get_clipboard_text()
                if not text or text == self._clip_last or len(text) > 100000:
                    continue
                self._clip_last = text
                self.loop.call_soon_threadsafe(self._push_clipboard, text)
            except Exception:
                pass

    def _push_clipboard(self, text: str) -> None:
        if self.session:
            self.session.send_json({"t": "clip", "text": text})

    # ---- screen info -----------------------------------------------------------------
    def refresh_monitors(self) -> None:
        name, vendor, monitors = Windows.display_info()
        if monitors:
            self.gpu_name, self.gpu_vendor, self.monitors = name, vendor, monitors

    def monitor_rect(self, index: int):
        if 0 <= index < len(self.monitors):
            return self.monitors[index]
        if self.monitors:
            return self.monitors[0]
        w, h = Windows.primary_screen_size()
        return 0, 0, w, h

    def welcome(self, session: Session) -> dict:
        monitors = self.monitors or ([(0, 0, *DEMO_SIZE)] if self.args.demo else [])
        return {
            "t": "welcome", "host": socket.gethostname(), "version": VERSION, "demo": self.args.demo,
            "gamepad": self.pads.available, "audio": self.audio is not None, "opus": self.opus_ok,
            "clipboard": IS_WINDOWS and not self.args.no_clipboard and not self.args.demo,
            "monitors": [{"w": m[2], "h": m[3]} for m in monitors],
            "cfg": asdict(session.settings), "gpu": self.gpu_name or "",
        }

    # ---- streaming lifecycle -----------------------------------------------------------
    async def start_streaming(self, settings: StreamSettings) -> None:
        Windows.keep_awake(display=True)
        if IS_WINDOWS and not self.args.demo:
            with contextlib.suppress(Exception):          # a 1-pixel mouse wiggle wakes a dark screen
                self.input.move_relative(1, 0)
                self.input.move_relative(-1, 0)
        self.video.start(settings)
        if self.audio:
            if self.session and self.session.audio_codec == "opus":
                self._start_opus()
            else:
                self._stop_opus()
            self.audio.start()

    async def stop_streaming(self) -> None:
        await self.video.stop()
        # Someone may have connected while the video was shutting down (e.g. switching from
        # the phone to the PC). Their session has already started its own sound; leave it alone.
        if self.session is not None:
            return
        if self.audio:
            self.audio.stop()
        self._stop_opus()
        Windows.keep_awake(display=False)

    async def warm_up(self) -> None:
        """Find a working encoder at startup so problems show up before anyone connects."""
        if self.session or self.video.running:
            return
        self.video.start(self.default_settings)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self.video.started_event.wait(), 60)
        if self.session is None:
            await self.video.stop()
            if self.video.info:
                log.info("Ready. Video encoder: %s.", self.video.info["encoder"])
            else:
                log.warning("Could not start a test capture yet (%s). It will be retried when you connect.",
                            self.video.last_error or "unknown error")

    # ---- HTTP / WebSocket ------------------------------------------------------------
    async def handle_connection(self, reader, writer) -> None:
        try:
            try:
                head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 15)
            except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError,
                    ConnectionError, OSError, ssl.SSLError):
                return
            request = parse_request(head)
            if not request:
                await http_response(writer, 400, b"Bad request")
                return
            method, path, headers = request
            if path == "/ws":
                await self.handle_websocket(reader, writer, headers)
            elif method in ("GET", "HEAD"):
                await self.serve_static(writer, method, path)
            else:
                await http_response(writer, 405, b"Method not allowed")
        except (ConnectionError, OSError, asyncio.IncompleteReadError, ssl.SSLError):
            pass
        except Exception:
            log.exception("Unexpected error while handling a connection")
        finally:
            with contextlib.suppress(Exception):
                writer.close()

    async def serve_static(self, writer, method: str, path: str) -> None:
        rel = urllib.parse.unquote(path).lstrip("/") or "index.html"
        try:
            full = (WEB_DIR / rel).resolve()
            ok = full.is_file() and full.is_relative_to(WEB_DIR.resolve())
        except (OSError, ValueError):
            ok = False
        if not ok:
            await http_response(writer, 404, b"Not found")
            return
        body = full.read_bytes()
        content_type = CONTENT_TYPES.get(full.suffix.lower(), "application/octet-stream")
        await http_response(writer, 200, body if method == "GET" else b"", content_type,
                            length=len(body), extra={"Cache-Control": "no-cache"})

    def host_allowed(self, name: str) -> bool:
        """Is this a name that really leads to this PC? (Guards against DNS rebinding, where a web
        page's own host name is made to point at this PC.)"""
        if name in self.own_names or name in self.extra_hosts or name.endswith((".localhost", ".ts.net")):
            return True
        try:
            ipaddress.ip_address(name)            # a bare address: there is no name to rebind
            return True
        except ValueError:
            return False

    def origin_problem(self, headers: dict) -> str | None:
        """Why this WebSocket handshake must be refused, or None if it is fine.

        A browser always sends an Origin header with a WebSocket handshake, and a web page cannot change
        the other headers. So a page from another site (open in a browser on this PC or on your Tailscale
        network) shows up as an Origin that is not this server's own address. Programs that are not
        browsers send no Origin; for them the password is the only gate."""
        origin = headers.get("origin")
        if origin is None:
            return None
        try:
            o = urllib.parse.urlsplit(origin.strip().lower())
            o_name, o_port = o.hostname, o.port
        except ValueError:
            return "malformed Origin header"
        if o.scheme not in ("http", "https") or not o_name:
            return f"page origin {clean_for_log(origin, 40)!r}"
        if o_name in self.extra_hosts:
            return None
        default_port = 443 if o.scheme == "https" else 80
        wanted = (o_name, o_port or default_port)
        # Behind Tailscale the original address may arrive in X-Forwarded-Host instead of Host.
        claims = (headers.get("host", ""), headers.get("x-forwarded-host", "").split(",")[0])
        if not any(host_port(c, default_port) == wanted for c in claims):
            return f"page from {clean_for_log(origin, 40)!r}"
        if self.check_host and not self.host_allowed(o_name):
            return f"address {clean_for_log(o_name, 40)!r}"
        return None

    async def handle_websocket(self, reader, writer, headers: dict) -> None:
        key = headers.get("sec-websocket-key", "")
        if headers.get("upgrade", "").lower() != "websocket" or not key \
                or headers.get("sec-websocket-version") != "13":
            await http_response(writer, 400, b"Expected a WebSocket request")
            return
        problem = self.origin_problem(headers)
        if problem:
            log.warning("Refused a connection from a web page that is not HomePlay's own (%s). If you open "
                        "HomePlay at this address on purpose, start it with --allow-host NAME.", problem)
            await http_response(writer, 403, b"Forbidden: this web page may not connect to HomePlay")
            return
        accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        writer.write(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                      f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode())
        await writer.drain()
        ws = WebSocket(reader, writer)
        peer = writer.get_extra_info("peername")
        peer_ip = str(peer[0]) if peer else "unknown"
        claimed = ""
        if peer_ip in ("127.0.0.1", "::1"):
            # Only the local Tailscale proxy is believed about who is on the other end: a device that
            # connects directly (--lan) could write anything into these headers.
            claimed = (headers.get("tailscale-user-login")
                       or headers.get("x-forwarded-for", "").split(",")[0].strip())
        who = clean_for_log(claimed or peer_ip)
        if self.guard.locked():
            await ws.close(CLOSE_LOCKED_OUT, "Too many wrong passwords")
            return
        if self.pending_logins >= self.MAX_PENDING_LOGINS:
            await ws.close(CLOSE_LOCKED_OUT, "Too many logins at once")
            return
        # Until the password has been checked (including the pause after a wrong guess) this
        # connection counts as "waiting to log in", so one client cannot run a flood of guesses
        # side by side.
        self.pending_logins += 1
        try:
            ws.MAX_MESSAGE = self.HELLO_MAX_BYTES      # nobody is logged in yet: the first message is small
            try:
                first = await asyncio.wait_for(ws.recv(), 20)
                hello = json.loads(first[1]) if first and first[0] == 0x1 else None
            except (asyncio.TimeoutError, asyncio.IncompleteReadError, ValueError, ConnectionError,
                    OSError, WebSocketProtocolError):
                hello = None
            if not isinstance(hello, dict) or hello.get("t") != "hello":
                await ws.close(CLOSE_PROTOCOL, "Expected hello")
                return
            # Look at the lock again: other connections may have used up the guesses while this one
            # was waiting for its hello. Nothing awaits between this check and guard.failed(), so each
            # guess is counted before the next one is even looked at.
            if self.guard.locked():
                await ws.close(CLOSE_LOCKED_OUT, "Too many wrong passwords")
                return
            password = hello.get("pw")
            if not isinstance(password, str) or not hmac.compare_digest(
                    password.strip().encode("utf-8"), self.password.encode("utf-8")):
                self.guard.failed()
                log.warning("Wrong password from %s", who)
                await asyncio.sleep(1.0)
                await ws.close(CLOSE_BAD_PASSWORD, "Wrong password")
                return
            self.guard.succeeded()
            ws.MAX_MESSAGE = WebSocket.MAX_MESSAGE
        finally:
            self.pending_logins -= 1
        session = Session(self, ws, hello, who)
        async with self.session_lock:
            old, self.session = self.session, session
            self.codec_caps = session.codec_caps
            if old:
                log.info("%s took over from %s", session.name, old.name)
                await old.kick("Another device connected")
        log.info("Connected: %s (can decode: %s)", session.name, ", ".join(sorted(session.codec_caps)))
        try:
            await session.run()
        finally:
            log.info("Disconnected: %s", session.name)
            if self.session is session:
                self.session = None
                await self.stop_streaming()

    async def shutdown(self) -> None:
        self._stopping = True
        if self.session:
            with contextlib.suppress(Exception):
                await self.session.ws.close(1001, "Host is shutting down")
        await self.video.stop()
        if self.audio:
            self.audio.stop()
        self._stop_opus()
        self.input.release_all()
        self.pads.remove_all()
        Windows.allow_sleep()


# ======================================================================================
# Tailscale (private access from your phone / other PCs) and HTTPS certificates
# ======================================================================================

class Tailscale:
    @staticmethod
    def find() -> str | None:
        exe = shutil.which("tailscale")
        if exe:
            return exe
        if IS_WINDOWS:
            for env in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
                base = os.environ.get(env)
                if base and (Path(base) / "Tailscale" / "tailscale.exe").is_file():
                    return str(Path(base) / "Tailscale" / "tailscale.exe")
        return None

    @staticmethod
    def _run(exe, *args, timeout=15):
        r = subprocess.run([exe, *args], capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, creationflags=NO_WINDOW)
        return r.returncode, r.stdout or "", r.stderr or ""

    @classmethod
    def _serve_status(cls, exe) -> dict:
        try:
            _, out, _ = cls._run(exe, "serve", "status", "--json")
            data = json.loads(out) if out.strip() else {}
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError, subprocess.SubprocessError):
            return {}

    @classmethod
    def _serve_url(cls, exe, port: int) -> str | None:
        status = cls._serve_status(exe)
        for hostport, cfg in (status.get("Web") or {}).items():
            for path, handler in ((cfg or {}).get("Handlers") or {}).items():
                proxy = str((handler or {}).get("Proxy", ""))
                if re.search(rf"(127\.0\.0\.1|localhost):{port}(/|$)", proxy):
                    host = hostport[:-4] if hostport.endswith(":443") else hostport
                    return f"https://{host}{'' if path == '/' else path}"
        return None

    @classmethod
    def setup(cls, port: int, configure: bool):
        """Returns (url or None, advice for the user)."""
        exe = cls.find()
        if not exe:
            return None, ("To play away from home, install Tailscale on this PC and your other devices "
                          "(see README.md).")
        try:
            _, out, _ = cls._run(exe, "status", "--json")
            status = json.loads(out or "{}")
        except (OSError, ValueError, subprocess.SubprocessError) as e:
            return None, f"Could not talk to Tailscale ({e})."
        if status.get("BackendState") != "Running":
            return None, "Tailscale is installed but not connected. Open Tailscale, sign in, then restart HomePlay."
        url = cls._serve_url(exe, port)
        if url or not configure:
            return url, "" if url else f"To reach this PC from your other devices run:  tailscale serve --bg {port}"
        taken = (cls._serve_status(exe).get("TCP") or {})
        https_port = 443 if "443" not in taken else 8443
        target = f"http://127.0.0.1:{port}"
        manual = f"tailscale serve --bg --https={https_port} {target}"
        log.info("Setting up Tailscale: %s", manual)
        try:
            _, out, err = cls._run(exe, "serve", "--bg", f"--https={https_port}", target, timeout=25)
        except subprocess.TimeoutExpired as e:
            said = b"".join(x for x in (e.stdout, e.stderr) if isinstance(x, bytes)).decode("utf-8", "replace")
            return None, ("Tailscale needs you to allow HTTPS once:\n" + said.strip() +
                          "\nAfter that, start HomePlay again.")
        except (OSError, subprocess.SubprocessError) as e:
            return None, f"Could not set up Tailscale ({e}). Run this yourself:  {manual}"
        url = cls._serve_url(exe, port)
        if url:
            return url, ""
        return None, f"Tailscale said:\n{(out + err).strip()}\nIf needed, run this yourself:  {manual}"


def self_signed_certificate() -> tuple[str, str]:
    cert, key = CERT_DIR / "cert.pem", CERT_DIR / "key.pem"
    if cert.is_file() and key.is_file():
        return str(cert), str(key)
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import NameOID
    except ImportError:
        raise SystemExit("HTTPS mode needs the 'cryptography' package. Install it with:\n"
                         "  .venv\\Scripts\\python -m pip install cryptography\n"
                         "or pass your own certificate with --cert and --key.")
    CERT_DIR.mkdir(exist_ok=True)
    private_key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"HomePlay on {socket.gethostname()}")])
    alt_names = [x509.DNSName("localhost"), x509.DNSName(socket.gethostname())]
    for ip in ["127.0.0.1", *lan_addresses()]:
        with contextlib.suppress(ValueError):
            alt_names.append(x509.IPAddress(ipaddress.ip_address(ip)))
    now = datetime.datetime.now(datetime.timezone.utc)
    certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                   .public_key(private_key.public_key()).serial_number(x509.random_serial_number())
                   .not_valid_before(now - datetime.timedelta(days=1))
                   .not_valid_after(now + datetime.timedelta(days=825))
                   .add_extension(x509.SubjectAlternativeName(alt_names), critical=False)
                   .sign(private_key, hashes.SHA256()))
    key.write_bytes(private_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                              serialization.NoEncryption()))
    cert.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    return str(cert), str(key)


# ======================================================================================
# Startup
# ======================================================================================

def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="HomePlay host: play the games on this PC from a browser on your phone or another PC.")
    p.add_argument("--port", type=int, default=8080, help="local port (default 8080)")
    p.add_argument("--lan", action="store_true",
                   help="also accept devices on your home network directly (HTTPS with a self-signed certificate)")
    p.add_argument("--https", action="store_true", help="serve HTTPS yourself (self-signed unless --cert/--key)")
    p.add_argument("--cert", help="certificate file (PEM) for --https")
    p.add_argument("--key", help="private key file (PEM) for --https")
    p.add_argument("--bind", help="address to listen on (default 127.0.0.1, or 0.0.0.0 with --lan)")
    p.add_argument("--allow-host", action="append", default=[], metavar="NAME",
                   help="also accept web pages opened from this host name (repeat for more); only needed "
                        "if you reach HomePlay through your own domain or reverse proxy")
    p.add_argument("--password", help="set a new password and remember it")
    p.add_argument("--new-password", action="store_true", help="make up a new random password")
    p.add_argument("--fps", type=int, default=60, help="default frame rate (the browser can change it)")
    p.add_argument("--bitrate", type=float, default=15, help="default bitrate in Mbit/s (the browser can change it)")
    p.add_argument("--monitor", type=int, default=0, help="screen to stream: 0 = first (default)")
    p.add_argument("--encoder", choices=["auto", "nvenc", "amf", "qsv", "x264"], default="auto",
                   help="video encoder (default: try the graphics card first)")
    p.add_argument("--codec", choices=["auto", "h264", "hevc", "av1"], default="auto",
                   help="video codec (default: best the browser can decode; H.264 is the fallback)")
    p.add_argument("--auto", action="store_true",
                   help="start in automatic-bitrate mode (host matches the bitrate to the connection)")
    p.add_argument("--gop", type=float, default=2.0, help="seconds between keyframes (default 2)")
    p.add_argument("--ffmpeg", help="path to ffmpeg.exe")
    p.add_argument("--no-audio", action="store_true", help="don't send sound")
    p.add_argument("--no-gamepad", action="store_true", help="don't create virtual controllers")
    p.add_argument("--no-clipboard", action="store_true", help="don't share the clipboard with the browser")
    p.add_argument("--tailscale", action="store_true",
                   help="set up 'tailscale serve' so your other devices can reach this PC")
    p.add_argument("--demo", action="store_true",
                   help="stream a test pattern and only log input (for trying things out)")
    p.add_argument("--verbose", "-v", action="store_true", help="more detailed log")
    p.add_argument("--check-ffmpeg", action="store_true", help=argparse.SUPPRESS)
    return p.parse_args(argv)


def print_banner(app: HostApp, urls, notes) -> None:
    line = "=" * 70
    print(line)
    print(f"  HomePlay {VERSION} is running" + ("   [DEMO: test picture, input is only logged]"
                                                if app.args.demo else ""))
    print()
    print(f"  Password:   {app.password}")
    print()
    for label, url in urls:
        print(f"  {label:<12}{url}")
    for note in notes:
        for text in note.splitlines():
            print(f"  {text}")
    print()
    sound = app.audio.check() if app.audio else "off (--no-audio)"
    if app.audio and app.opus_ok:
        sound += ", Opus compression"
    print(f"  Sound:      {sound}")
    print(f"  Gamepad:    {app.pads.status}")
    if app.gpu_name:
        print(f"  Graphics:   {app.gpu_name}")
    print(f"  FFmpeg:     {app.ff.version or app.ff.path}")
    print()
    print("  Stop with Ctrl+C or by closing this window.")
    print(line, flush=True)


async def run_host(args, password: str, ff: FFmpeg) -> None:
    app = HostApp(args, password, ff)
    ssl_context = None
    if args.https or args.lan:
        cert, key = (args.cert, args.key) if (args.cert and args.key) else self_signed_certificate()
        ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ssl_context.load_cert_chain(cert, key)
    bind = args.bind or ("0.0.0.0" if args.lan else "127.0.0.1")
    if ssl_context is None and bind not in ("127.0.0.1", "localhost", "::1"):
        log.warning("Listening on %s without HTTPS: the password and every key you press would cross the "
                    "network unencrypted. Use --lan or --https, or keep the default (this PC only).", bind)
    try:
        server = await asyncio.start_server(app.handle_connection, bind, args.port, ssl=ssl_context,
                                            limit=65536)
    except OSError as e:
        raise SystemExit(f"Can't use port {args.port} ({e}). Is HomePlay already running? "
                         f"Otherwise pick another port with --port.")
    scheme = "https" if ssl_context else "http"
    urls = [("This PC:", f"{scheme}://localhost:{args.port}")]
    notes = []
    if args.lan:
        for ip in lan_addresses():
            label = "Tailscale:" if ip.startswith("100.") else "Home Wi-Fi:"
            urls.append((label, f"https://{ip}:{args.port}   (accept the certificate warning once)"))
    if not args.lan and bind in ("127.0.0.1", "localhost") and not ssl_context:
        ts_url, advice = await asyncio.to_thread(Tailscale.setup, args.port, args.tailscale)
        if ts_url:
            urls.append(("Anywhere:", f"{ts_url}   (via Tailscale)"))
            ts_host = urllib.parse.urlsplit(ts_url).hostname
            if ts_host:                           # pages opened from this address are HomePlay's own
                app.extra_hosts.add(ts_host.lower())
        if advice:
            notes.append(advice)
    Windows.keep_awake(display=False)          # the PC must stay awake so you can connect
    print_banner(app, urls, notes)
    asyncio.create_task(app.warm_up())
    try:
        async with server:
            await server.serve_forever()
    finally:
        await app.shutdown()


def main(argv=None) -> None:
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
    if sys.version_info < (3, 9):
        raise SystemExit("HomePlay needs Python 3.9 or newer.")
    ffmpeg_path = find_ffmpeg(args.ffmpeg)
    if args.check_ffmpeg:
        print(ffmpeg_path or "")
        raise SystemExit(0 if ffmpeg_path else 1)
    if not ffmpeg_path:
        raise SystemExit("FFmpeg was not found. Run start.bat (it installs FFmpeg), or download the "
                         "'release essentials' build from https://www.gyan.dev/ffmpeg/builds/ and unzip it "
                         "into a folder named 'ffmpeg' next to this file.")
    if not IS_WINDOWS and not args.demo:
        raise SystemExit("Screen capture and input only work on Windows. Use --demo to try HomePlay here.")
    Windows.prepare_process()

    config = load_config()
    if args.password:
        config["password"] = args.password.strip()
        if len(config["password"]) < MIN_PASSWORD_LENGTH:
            raise SystemExit(f"The password must have at least {MIN_PASSWORD_LENGTH} characters. After 6 wrong "
                             "tries logins only pause for 2 minutes, so a short password can still be guessed.\n"
                             "Use a longer phrase, or leave out --password to get a random one.")
    elif args.new_password or not config.get("password"):
        config["password"] = generate_password()
    save_config(config)
    if len(str(config["password"])) < MIN_PASSWORD_LENGTH:
        log.warning("The saved password is short and could be guessed. Run start.bat --new-password for a "
                    "random one, or choose a longer one with --password.")

    ff = FFmpeg(ffmpeg_path)
    ff.inspect()
    if not ff.encoders:
        raise SystemExit(f"Could not run FFmpeg at {ffmpeg_path}.")
    if not args.demo and "ddagrab" not in ff.filters:
        log.warning("This FFmpeg (%s) has no 'ddagrab' screen capture; using the slower 'gdigrab'. "
                    "Install FFmpeg 6 or newer for much better performance.", ff.version)
    try:
        asyncio.run(run_host(args, config["password"], ff))
    except KeyboardInterrupt:
        pass
    finally:
        Windows.allow_sleep()
        print("HomePlay stopped.")


if __name__ == "__main__":
    main()
