#!C:\Python313\pythonw.exe
"""copy-save: periodically snapshot games' autosave folders into timestamped zips.

Each game profile has a save folder, a game executable and a cadence. While a
profile's game is running, its save folder is zipped into
<parent of save folder>/copy-save-zips/<timestamp>.zip, where <timestamp> is the
folder's latest modification time. If nothing changed since the last zip, the
attempt is skipped.

Any zip can be restored from the GUI. The current save is zipped first, so a
restore can always be undone.
"""

import base64
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:
    import psutil
except ImportError:
    psutil = None

# Optional Windows 11 look (pip install sv-ttk darkdetect); falls back to plain ttk.
try:
    import sv_ttk
except ImportError:
    sv_ttk = None
try:
    import darkdetect
except ImportError:
    darkdetect = None

__version__ = "1.0.0"

FROZEN = getattr(sys, "frozen", False)  # running as the packaged copy-save.exe
APP_DIR = Path(__file__).resolve().parent
RES_DIR = Path(getattr(sys, "_MEIPASS", APP_DIR))  # bundled files when packaged
CONFIG_DIR = Path(os.environ.get("APPDATA", Path.home())) / "copy-save"
CONFIG_FILE = DEFAULT_CONFIG_FILE = CONFIG_DIR / "config.json"
LEGACY_CONFIG_FILE = APP_DIR / "copy_save_config.json"  # used before v1.0
ICON_FILE = RES_DIR / "copy_save.ico"
APP_ID = "copy-save"  # Windows taskbar identity
ZIP_DIR_NAME = "copy-save-zips"
TS_FORMAT = "%Y-%m-%d_%H-%M-%S"  # colons aren't allowed in Windows file names

# Held while zipping or restoring so the monitor never zips a half-restored folder.
fs_lock = threading.RLock()


# ---------------------------------------------------------------- core logic

def is_running(exe_path: str) -> bool:
    """True if a process with the same executable name (or full path) is running."""
    exe_name = Path(exe_path).name.lower()
    exe_full = os.path.normcase(os.path.abspath(exe_path))
    if psutil:
        for p in psutil.process_iter(["name", "exe"]):
            try:
                if p.info["exe"] and os.path.normcase(p.info["exe"]) == exe_full:
                    return True
                if (p.info["name"] or "").lower() == exe_name:
                    return True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return False
    out = subprocess.run(
        ["tasklist", "/FI", f"IMAGENAME eq {Path(exe_path).name}", "/NH", "/FO", "CSV"],
        capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout
    return exe_name in out.lower()


def folder_mtime(folder: Path) -> float:
    """Latest modification time of the folder or anything inside it.

    Windows only bumps a directory's own mtime when entries are added/removed,
    not when an existing save file is overwritten, so we look at the contents too.
    """
    latest = folder.stat().st_mtime
    for root, dirs, files in os.walk(folder):
        for name in dirs + files:
            try:
                latest = max(latest, os.stat(os.path.join(root, name)).st_mtime)
            except OSError:
                pass
    return latest


def zip_name_for(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime(TS_FORMAT) + ".zip"


def zip_belongs_to(zip_path: Path, save_dir: Path) -> bool:
    """True if the zip's top-level entry is this save folder."""
    try:
        with zipfile.ZipFile(zip_path) as zf:
            first = zf.namelist()[0]
    except (zipfile.BadZipFile, IndexError, OSError):
        return False
    return first.lower() == save_dir.name.lower() + "/"


def snapshot(save_dir: Path, log):
    """Zip save_dir if it changed since the last zip.

    Returns the zip holding the folder's current state (new or existing), or
    None if zipping failed.
    """
    with fs_lock:
        return _snapshot(save_dir, log)


def _snapshot(save_dir: Path, log):
    zip_dir = save_dir.parent / ZIP_DIR_NAME
    zip_dir.mkdir(exist_ok=True)

    ts = folder_mtime(save_dir)
    # The zip name *is* the folder timestamp, so an existing zip of this folder
    # with that name means nothing changed since it was made. If another game
    # sharing this zip folder already took the name, add our folder name to it.
    plain = zip_name_for(ts)
    candidates = [zip_dir / plain, zip_dir / f"{plain[:-4]}_{save_dir.name}.zip"]
    for target in candidates:
        if not target.exists():
            break
        if zip_belongs_to(target, save_dir):
            log(f"No changes since {target.name}, skipping.")
            return target

    tmp = target.with_suffix(".zip.part")
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
            # Store the folder itself as the top-level entry so restoring is a
            # simple "extract here" next to the original folder.
            zf.write(save_dir, save_dir.name)
            for root, dirs, files in os.walk(save_dir):
                for name in dirs + files:
                    full = Path(root) / name
                    zf.write(full, Path(save_dir.name) / full.relative_to(save_dir))
    except Exception as e:
        tmp.unlink(missing_ok=True)
        log(f"Zip failed ({e}); will retry next time.")
        return None

    # If the game wrote to the folder while we were zipping, the copy may be
    # inconsistent: throw it away and try again on the next tick.
    if folder_mtime(save_dir) != ts:
        tmp.unlink(missing_ok=True)
        log("Save folder changed during zipping; will retry next time.")
        return None

    tmp.replace(target)
    os.utime(target, (ts, ts))  # zip's own timestamp == folder timestamp
    size_kb = target.stat().st_size / 1024
    log(f"Saved {target.name} ({size_kb:,.0f} KB)")
    return target


def zips_for(save_dir: Path):
    """This save folder's zips, newest first.

    Several games may share one copy-save-zips folder, so each zip is checked
    for the save folder's name as its top-level entry.
    """
    zip_dir = save_dir.parent / ZIP_DIR_NAME
    found = [z for z in zip_dir.glob("*.zip") if zip_belongs_to(z, save_dir)]
    return sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)


def restore(zip_path: Path, save_dir: Path, log) -> None:
    """Replace save_dir with the contents of zip_path. Raises on failure.

    The zip is extracted next to the save folder first and only then swapped in,
    so a failure (e.g. the game has a file locked) leaves the current save intact.
    File timestamps are restored too, so the restored folder's timestamp matches
    the zip's name and the monitor doesn't immediately re-zip it as "new".
    """
    parent, name = save_dir.parent, save_dir.name
    staging = parent / f".copy-save-restore-{name}"
    old = parent / f".copy-save-old-{name}"
    with fs_lock:
        for leftover in (staging, old):
            if leftover.exists():
                shutil.rmtree(leftover)
        with zipfile.ZipFile(zip_path) as zf:
            infos = zf.infolist()
            if not all(i.filename.lower().startswith(name.lower() + "/") for i in infos):
                raise ValueError(f"{zip_path.name} doesn't contain a “{name}” folder.")
            zf.extractall(staging)
            # Files first, then directories deepest-first: writing into a
            # directory would bump its timestamp again.
            for info in sorted(infos, key=lambda i: (i.is_dir(), -i.filename.count("/"))):
                t = time.mktime(info.date_time + (0, 0, -1))
                os.utime(staging / info.filename, (t, t))
        restored = staging / name

        save_dir.rename(old)
        try:
            restored.rename(save_dir)
        except Exception:
            old.rename(save_dir)
            raise
        ts = zip_path.stat().st_mtime  # exact original folder timestamp
        os.utime(save_dir, (ts, ts))
        shutil.rmtree(old, ignore_errors=True)
        shutil.rmtree(staging, ignore_errors=True)
    log(f"Restored {zip_path.name}")


class Monitor(threading.Thread):
    """Checks every enabled profile on its own cadence and snapshots its saves.

    Profiles are read live on every tick, so edits in the GUI (including adding,
    removing or disabling profiles) take effect without restarting.
    """

    def __init__(self, get_profiles, log, set_status):
        super().__init__(daemon=True)
        self.get_profiles, self.log, self.set_status = get_profiles, log, set_status
        self.stop_event = threading.Event()
        self.next_due = {}      # profile id -> monotonic time of next check
        self.was_running = {}   # profile id -> last seen game state

    def run(self):
        while not self.stop_event.is_set():
            now = time.monotonic()
            for prof in self.get_profiles():
                pid = prof["id"]
                if not prof.get("enabled", True):
                    if self.was_running.pop(pid, None) is not None:
                        self.set_status(pid, None)
                    continue
                if now < self.next_due.get(pid, 0):
                    continue
                self.next_due[pid] = now + 60 / prof["saves_per_minute"]
                self.check(prof)
            self.stop_event.wait(1)

    def check(self, prof):
        pid, name = prof["id"], prof["name"]
        log = lambda msg: self.log(f"[{name}] {msg}")
        try:
            if not prof["exe"] or not Path(prof["save_dir"]).is_dir():
                if self.was_running.get(pid) != "invalid":
                    log("Save folder or executable not set/found; skipping.")
                    self.was_running[pid] = "invalid"
                    self.set_status(pid, None)
                return
            running = is_running(prof["exe"])
            if running != self.was_running.get(pid):
                log(f"{Path(prof['exe']).name} is {'running' if running else 'not running'}.")
                self.was_running[pid] = running
                self.set_status(pid, running)
            if running:
                snapshot(Path(prof["save_dir"]), log)
        except Exception as e:
            log(f"Error: {e}")

    def stop(self):
        self.stop_event.set()


# ---------------------------------------------------------------------- GUI

def system_theme() -> str:
    return "dark" if darkdetect and darkdetect.isDark() else "light"


UI_SCALE = 1.0  # display scaling factor, set once the Tk root exists


def dp(n: float) -> int:
    """Convert a size in 100%-scale pixels to real pixels on this display."""
    return round(n * UI_SCALE)


def leave_store_python() -> None:
    """Relaunch under the interpreter on this script's #! line if running under
    the Microsoft Store Python.

    Windows always shows a Store (packaged) app's own logo on its taskbar
    button, so our icon can't appear there. Double-clicking a .pyw may start
    the Store Python directly, bypassing the py launcher that honours the #! line.
    """
    if "WindowsApps" not in sys.executable:
        return
    first_line = Path(__file__).read_text(encoding="utf-8").partition("\n")[0]
    target = first_line[2:].strip() if first_line.startswith("#!") else ""
    if target and "WindowsApps" not in target and Path(target).is_file():
        subprocess.Popen([target, str(Path(__file__).resolve()), *sys.argv[1:]])
        sys.exit(0)


def enable_dpi_awareness() -> None:
    """Render crisply on scaled displays instead of being bitmap-stretched (blurry)."""
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor aware
        except Exception:
            pass


def scale_theme_fonts(root) -> None:
    """sv-ttk sizes its fonts in pixels, which come out tiny on scaled displays.

    Redefine them in points so Tk scales them with the display's DPI.
    """
    import tkinter.font as tkfont
    for name in tkfont.names(root):
        if name.startswith("SunValley"):
            font = tkfont.nametofont(name, root)
            size = font.cget("size")
            if size < 0:  # negative = pixels at 96 DPI; 1 px = 0.75 pt
                font.configure(size=round(-size * 0.75))


def fit_row_height(root) -> None:
    """Treeview row height is in fixed pixels; size it from the font so rows
    don't overlap on scaled displays (with or without sv-ttk)."""
    import tkinter.font as tkfont
    font, factor = ("SunValleyBodyFont", 1.9) if sv_ttk else ("TkDefaultFont", 1.5)
    row_px = round(tkfont.nametofont(font, root).metrics("linespace") * factor)
    ttk.Style(root).configure("Treeview", rowheight=row_px)


def set_app_id() -> None:
    """Give the process its own taskbar identity. Must run before any window exists.

    Without it, Windows groups the window under python(w).exe and shows
    Python's icon in the taskbar.
    """
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
        except Exception:
            pass


def ensure_start_menu_shortcut() -> None:
    """Create/refresh a Start Menu shortcut carrying our app ID and icon.

    The taskbar draws its button with the icon of the owning executable
    (pythonw.exe), ignoring the window's own icon, unless a Start Menu shortcut
    carries the same app ID as the process; then it uses that shortcut's icon.
    Bonus: copy-save shows up in Start and pins cleanly. (Doesn't help under the
    Microsoft Store Python: Windows always shows a packaged app's own logo.)
    """
    if sys.platform != "win32" or not ICON_FILE.exists():
        return
    try:
        import ctypes
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [("d1", wintypes.DWORD), ("d2", wintypes.WORD),
                        ("d3", wintypes.WORD), ("d4", ctypes.c_ubyte * 8)]

        class PROPERTYKEY(ctypes.Structure):
            _fields_ = [("fmtid", GUID), ("pid", wintypes.DWORD)]

        class PROPVARIANT(ctypes.Structure):
            _fields_ = [("vt", ctypes.c_ushort), ("r1", ctypes.c_ushort), ("r2", ctypes.c_ushort),
                        ("r3", ctypes.c_ushort), ("value", ctypes.c_wchar_p), ("pad", ctypes.c_void_p)]

        def guid(s):
            return ctypes.byref(GUID.from_buffer_copy(uuid.UUID(s).bytes_le))

        def method(obj, index, *argtypes):
            """Bind a COM vtable method by index."""
            vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
            fn = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)(vtbl[index])
            return lambda *args: fn(obj, *args)

        def query(obj, iid):
            out = ctypes.c_void_p()
            method(obj, 0, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))(guid(iid), ctypes.byref(out))
            return out

        script = Path(__file__).resolve()
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        target = shutil.which("pyw") or str(pythonw)  # same launcher as double-clicking
        programs = Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs"
        lnk = programs / "copy-save (from source).lnk"  # installer owns "copy-save.lnk"
        stamp = CONFIG_DIR / "source-shortcut"  # remembers what the shortcut was made for
        wanted = f"{APP_ID}|{target}|{script}|{ICON_FILE.stat().st_mtime}"
        if lnk.exists() and stamp.exists() and stamp.read_text(encoding="utf-8") == wanted:
            return

        ole32 = ctypes.windll.ole32
        ole32.CoInitialize(None)
        link = ctypes.c_void_p()
        ole32.CoCreateInstance(guid("00021401-0000-0000-C000-000000000046"), None, 1,  # ShellLink
                               guid("000214F9-0000-0000-C000-000000000046"),         # IShellLinkW
                               ctypes.byref(link))
        if not link:
            return
        method(link, 20, ctypes.c_wchar_p)(target)                     # SetPath
        method(link, 11, ctypes.c_wchar_p)(f'"{script}"')              # SetArguments
        method(link, 9, ctypes.c_wchar_p)(str(APP_DIR))                # SetWorkingDirectory
        method(link, 17, ctypes.c_wchar_p, ctypes.c_int)(str(ICON_FILE), 0)  # SetIconLocation
        method(link, 7, ctypes.c_wchar_p)("Snapshot and restore game autosaves")  # SetDescription

        store = query(link, "886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99")   # IPropertyStore
        app_id_key = PROPERTYKEY(GUID.from_buffer_copy(
            uuid.UUID("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3").bytes_le), 5)  # System.AppUserModel.ID
        VT_LPWSTR = 31
        method(store, 6, ctypes.POINTER(PROPERTYKEY), ctypes.POINTER(PROPVARIANT))(
            ctypes.byref(app_id_key), ctypes.byref(PROPVARIANT(vt=VT_LPWSTR, value=APP_ID)))
        method(store, 7)()                                             # Commit
        method(store, 2)()                                             # Release

        persist = query(link, "0000010b-0000-0000-C000-000000000046")  # IPersistFile
        hr = method(persist, 6, ctypes.c_wchar_p, ctypes.c_int)(str(lnk), 1)  # Save
        method(persist, 2)()
        method(link, 2)()
        if hr == 0:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            stamp.write_text(wanted, encoding="utf-8")
    except Exception:
        pass  # cosmetic only


def png_from_rgba(size: int, rgba: bytes) -> bytes:
    """Encode square RGBA pixels as PNG (which Tk can display with alpha)."""
    import struct, zlib
    stride = size * 4
    raw = b"".join(b"\0" + rgba[y * stride:(y + 1) * stride] for y in range(size))

    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data)))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def exe_icon_rgba(exe: str, size: int):
    """The exe's icon as RGBA pixels at exactly size x size, or None."""
    if sys.platform != "win32" or not exe or not Path(exe).is_file():
        return None
    import ctypes
    from ctypes import wintypes
    user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32

    class ICONINFO(ctypes.Structure):
        _fields_ = [("fIcon", wintypes.BOOL), ("xHotspot", wintypes.DWORD),
                    ("yHotspot", wintypes.DWORD), ("hbmMask", wintypes.HBITMAP),
                    ("hbmColor", wintypes.HBITMAP)]

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                    ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                    ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                    ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                    ("biClrImportant", wintypes.DWORD)]

    user32.PrivateExtractIconsW.argtypes = [
        wintypes.LPCWSTR, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ctypes.POINTER(wintypes.HICON), ctypes.c_void_p, wintypes.UINT, wintypes.UINT]
    gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
                                ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]
    user32.GetIconInfo.argtypes = [wintypes.HICON, ctypes.POINTER(ICONINFO)]
    user32.DestroyIcon.argtypes = [wintypes.HICON]
    user32.GetDC.restype = wintypes.HDC
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]

    hicon = wintypes.HICON()
    if user32.PrivateExtractIconsW(exe, 0, size, size, ctypes.byref(hicon), None, 1, 0) != 1 \
            or not hicon:
        return None
    info = ICONINFO()
    try:
        if not user32.GetIconInfo(hicon, ctypes.byref(info)):
            return None
        bmi = BITMAPINFOHEADER(biSize=ctypes.sizeof(BITMAPINFOHEADER), biWidth=size,
                               biHeight=-size, biPlanes=1, biBitCount=32)  # top-down BGRA
        hdc = user32.GetDC(None)
        try:
            def bits(hbm):
                buf = ctypes.create_string_buffer(size * size * 4)
                gdi32.GetDIBits(hdc, hbm, 0, size, buf, ctypes.byref(bmi), 0)
                return bytearray(buf.raw)
            color = bits(info.hbmColor) if info.hbmColor else None
            mask = bits(info.hbmMask) if info.hbmMask else None
        finally:
            user32.ReleaseDC(None, hdc)
    finally:
        for hbm in (info.hbmColor, info.hbmMask):
            if hbm:
                gdi32.DeleteObject(hbm)
        user32.DestroyIcon(hicon)
    if color is None:
        return None
    has_alpha = any(color[3::4])
    out = bytearray(size * size * 4)
    for i in range(0, len(out), 4):
        b, g, r, a = color[i:i + 4]
        if not has_alpha:  # old-style icon: transparency comes from the mask
            a = 0 if (mask and mask[i]) else 255
        out[i:i + 4] = bytes((r, g, b, a))
    return bytes(out)


def dim_rgba(rgba: bytes) -> bytes:
    """Greyscale and fade an icon for disabled profiles."""
    out = bytearray(rgba)
    for i in range(0, len(out), 4):
        r, g, b, a = out[i:i + 4]
        lum = round(0.299 * r + 0.587 * g + 0.114 * b)
        out[i:i + 4] = bytes((lum, lum, lum, a // 2))
    return bytes(out)


def ring_rgba(size: int, rgb=(0x8a, 0x8a, 0x8a)) -> bytes:
    """An anti-aliased empty circle: the placeholder when there's no exe icon."""
    import math
    out = bytearray(size * size * 4)
    centre, radius = size / 2, size * 0.36
    width = max(1.0, size / 14)
    ss = 4  # supersampling for smooth edges
    for y in range(size):
        for x in range(size):
            hits = sum(abs(math.hypot(x + (sx + .5) / ss - centre, y + (sy + .5) / ss - centre)
                           - radius) <= width / 2
                       for sy in range(ss) for sx in range(ss))
            if hits:
                i = (y * size + x) * 4
                out[i:i + 4] = bytes((*rgb, 255 * hits // (ss * ss)))
    return bytes(out)


_icon_handles = []  # Windows icons must stay alive while windows use them


def set_window_icon(win) -> None:
    """Use copy_save.ico for a window's title bar and taskbar button.

    Tk's iconbitmap only hands Windows a small icon, which gets upscaled
    (pixelated) on scaled displays. So load the icon at the exact small/large
    sizes this display's DPI calls for.
    """
    if not ICON_FILE.exists():
        return
    win.iconbitmap(str(ICON_FILE))  # fallback for other platforms / dialogs
    if sys.platform != "win32":
        return
    try:
        import ctypes
        user32 = ctypes.windll.user32
        user32.LoadImageW.restype = ctypes.c_void_p
        user32.LoadImageW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint,
                                      ctypes.c_int, ctypes.c_int, ctypes.c_uint]
        user32.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                        ctypes.c_size_t, ctypes.c_void_p]
        win.update_idletasks()
        hwnd = int(win.wm_frame(), 16)
        dpi = user32.GetDpiForWindow(ctypes.c_void_p(hwnd)) or 96
        WM_SETICON, IMAGE_ICON, LR_LOADFROMFILE = 0x80, 1, 0x10
        for which, metric in ((0, 49), (1, 11)):  # ICON_SMALL/SM_CXSMICON, ICON_BIG/SM_CXICON
            size = user32.GetSystemMetricsForDpi(metric, dpi)
            icon = user32.LoadImageW(None, str(ICON_FILE), IMAGE_ICON, size, size, LR_LOADFROMFILE)
            if icon:
                _icon_handles.append(icon)
                user32.SendMessageW(hwnd, WM_SETICON, which, icon)
    except Exception:
        pass  # cosmetic only


def style_title_bar(win, theme: str, bg: str = None) -> None:
    """Make the Windows title bar match the theme (it ignores Tk styling).

    Dark mode alone makes Windows fade the caption over ~300 ms; setting its
    colour too (Windows 11) switches it almost at once, and to the window's own
    background colour. The window must exist already (e.g. after set_window_icon)."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        hwnd = int(win.wm_frame(), 16)  # no update_idletasks: callers rely on this being instant

        def set_attr(attr, v):
            value = ctypes.c_int(v)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(value), ctypes.sizeof(value))
        DWMWA_USE_IMMERSIVE_DARK_MODE, DWMWA_CAPTION_COLOR = 20, 35
        set_attr(DWMWA_USE_IMMERSIVE_DARK_MODE, theme == "dark")
        if bg and len(bg) == 7 and bg.startswith("#"):
            r, g, b = int(bg[1:3], 16), int(bg[3:5], 16), int(bg[5:7], 16)
            set_attr(DWMWA_CAPTION_COLOR, r | g << 8 | b << 16)  # COLORREF is 0x00BBGGRR
    except Exception:
        pass  # cosmetic only (e.g. older Windows)


def theme_bg(root, theme: str):
    """The sv-ttk theme's window background colour ("#rrggbb"), or None."""
    try:
        return str(root.tk.call("set", f"ttk::theme::sv_{theme}::colors(-bg)"))
    except tk.TclError:
        return None  # plain ttk (sv-ttk not installed)


def _enlarge_9slice(tk_, img, edge, width=None, height=None):
    """Grow a 9-slice sprite (to width and/or height), keeping its `edge`-px
    border and tiling its uniform centre, so it looks the same when stretched."""
    w0, h0 = int(tk_.call("image", "width", img)), int(tk_.call("image", "height", img))
    w1, h1 = max(w0, width or 0), max(h0, height or 0)
    if (w1, h1) == (w0, h0):
        return

    def spans(old, new):
        if new == old:
            return [(0, old, 0, old)]
        return [(0, edge, 0, edge), (edge, old - edge, edge, new - edge), (old - edge, old, new - edge, new)]
    tmp = tk_.call("image", "create", "photo")
    tk_.call(tmp, "copy", img)
    tk_.call(img, "blank")
    tk_.call(img, "configure", "-width", w1, "-height", h1)
    for sx1, sx2, dx1, dx2 in spans(w0, w1):
        for sy1, sy2, dy1, dy2 in spans(h0, h1):
            tk_.call(img, "copy", tmp, "-from", sx1, sy1, sx2, sy2, "-to", dx1, dy1, dx2, dy2)
    tk_.call("image", "delete", tmp)


# sv-ttk draws widgets by tiling the small centre of 9-slice sprites (as little
# as 10x10 px): thousands of alpha blits per repaint on a high-DPI screen, which
# made resizing and theme switches crawl. Stretching the centres cuts that a
# lot. A sprite's size is also its element's minimum size, so each one only
# grows where that doesn't matter:
#   (sprites, border px to keep, new width, new height)
SV_TTK_SPRITES = [
    # Treeview.field (no minimum size), the Profile Labelframe and the log's
    # Card frame: both far bigger than 150 px even at 100% display scaling
    (("card",), 12, 150, 150),
    # column widths are set explicitly; 36 px is under the heading's own height (text + 28 px padding)
    (("heading-rest", "heading-hover", "heading-pressed"), 5, 400, 36),
    # stays under the smallest Entry/Spinbox, even at 100% display scaling
    (("textbox-rest", "textbox-hover", "textbox-focus", "textbox-error", "textbox-dis"), 5, 60, 28),
]


def speed_up_sv_ttk(root) -> None:
    if getattr(root, "_sv_sprites_done", False):
        return  # both themes' sprites are loaded up front; once is enough
    root._sv_sprites_done = True
    for ns in ("sv_light", "sv_dark"):
        for names, edge, width, height in SV_TTK_SPRITES:
            for name in names:
                try:
                    img = root.tk.call("set", f"ttk::theme::{ns}::I({name})")
                except tk.TclError:
                    continue
                _enlarge_9slice(root.tk, img, edge, width, height)


def no_repaint_on_resize(win) -> None:
    """Tk registers its window classes with CS_HREDRAW | CS_VREDRAW, so every
    size change makes Windows repaint every widget, even ones that didn't move.
    Tk widgets already redraw themselves when their own size changes, so drop
    those styles (class-wide, for this process)."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        user32 = ctypes.windll.user32
        user32.GetClassLongPtrW.restype = ctypes.c_size_t
        user32.GetClassLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]
        user32.SetClassLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_size_t]
        GCL_STYLE, CS_VREDRAW, CS_HREDRAW = -26, 0x1, 0x2
        for hwnd in (int(win.wm_frame(), 16), win.winfo_id()):  # TkTopLevel, TkChild
            style = user32.GetClassLongPtrW(hwnd, GCL_STYLE)
            user32.SetClassLongPtrW(hwnd, GCL_STYLE, style & ~(CS_HREDRAW | CS_VREDRAW))
    except Exception:
        pass


def window_bounds(win):
    """Visible on-screen rectangle (x, y, w, h) of a window, title bar included."""
    import ctypes
    from ctypes import wintypes
    hwnd = wintypes.HWND(int(win.wm_frame(), 16))
    r = wintypes.RECT()
    DWMWA_EXTENDED_FRAME_BOUNDS = 9  # excludes the invisible resize border
    if ctypes.windll.dwmapi.DwmGetWindowAttribute(
            hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(r), ctypes.sizeof(r)):
        ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right - r.left, r.bottom - r.top


def grab_screen(x, y, w, h) -> bytes:
    """Screenshot of a screen rectangle as binary PPM (which Tk reads natively)."""
    import ctypes
    from ctypes import wintypes
    user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
    user32.GetDC.restype = wintypes.HDC
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi32.BitBlt.argtypes = [wintypes.HDC] + [ctypes.c_int] * 4 + [wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD]
    gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
                                ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi32.DeleteDC.argtypes = [wintypes.HDC]

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                    ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD),
                    ("biCompression", wintypes.DWORD), ("biSizeImage", wintypes.DWORD),
                    ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
                    ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]

    screen = user32.GetDC(None)
    mem = gdi32.CreateCompatibleDC(screen)
    bmp = gdi32.CreateCompatibleBitmap(screen, w, h)
    old = gdi32.SelectObject(mem, bmp)
    gdi32.BitBlt(mem, 0, 0, w, h, screen, x, y, 0x00CC0020)  # SRCCOPY
    gdi32.SelectObject(mem, old)
    bmi = BITMAPINFOHEADER(biSize=ctypes.sizeof(BITMAPINFOHEADER), biWidth=w, biHeight=-h,
                           biPlanes=1, biBitCount=32)
    buf = ctypes.create_string_buffer(w * h * 4)
    lines = gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bmi), 0)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mem)
    user32.ReleaseDC(None, screen)
    if lines != h:
        raise OSError("screen capture failed")
    bgra = buf.raw
    rgb = bytearray(w * h * 3)
    rgb[0::3], rgb[1::3], rgb[2::3] = bgra[2::4], bgra[1::4], bgra[0::4]
    return b"P6\n%d %d\n255\n" % (w, h) + bytes(rgb)


def cover_window(win):
    """Put a still snapshot of a window on top of it, so it can restyle and
    repaint underneath and swap the new look in at once; returns the cover."""
    if sys.platform != "win32" or win.state() not in ("normal", "zoomed"):
        return None
    try:
        win.update_idletasks()  # let a clicked switch draw its new state first
        x, y, w, h = window_bounds(win)
        img = tk.PhotoImage(master=win, data=grab_screen(x, y, w, h), format="ppm")
    except Exception:
        return None  # cosmetic only
    cover = tk.Toplevel(win)
    cover.overrideredirect(True)
    cover.attributes("-alpha", 0.0)
    cover.attributes("-topmost", True)  # above the window even if it's topmost
    cover.geometry(f"{w}x{h}+{x}+{y}")
    tk.Label(cover, image=img, borderwidth=0, highlightthickness=0).pack()
    cover.image = img
    cover.update()
    cover.attributes("-alpha", 1.0)
    cover.update_idletasks()
    return cover


class SmoothWindow:
    """Mixin for the app's windows (tk.Tk / tk.Toplevel): appear in one go
    after a restore, and don't repaint on every step of a drag-resize.

    Call smooth_window(content) once the window has been revealed, with the
    frame that holds everything else."""

    def smooth_window(self, content):
        self._content = content
        self._hide_job = None
        self._thaw_job = None
        self._size = (self.winfo_width(), self.winfo_height())
        self.bind("<Unmap>", self._on_unmap, add="+")
        self.bind("<Map>", self._on_map, add="+")
        self.bind("<Configure>", self._on_configure, add="+")

    # Restoring from the taskbar repaints every widget from scratch (a minimized
    # window keeps no picture of itself), so stay transparent while minimized
    # and reveal once painted, like at startup.
    def _on_unmap(self, event):
        if event.widget is self and self.state() in ("iconic", "withdrawn"):
            # wait for the minimize animation, which uses the window's picture
            self._hide_job = self.after(500, lambda: self.attributes("-alpha", 0.0))

    def _on_map(self, event):
        if event.widget is not self:
            return
        if self._hide_job:
            self.after_cancel(self._hide_job)
            self._hide_job = None
        self.on_shown()
        if float(self.attributes("-alpha")) < 1.0:
            self.after(1, self._reveal)

    def on_shown(self):
        """Called whenever the window is (re)shown, before it's revealed."""

    def _reveal(self):
        self.update()
        self.attributes("-alpha", 1.0)

    # Re-laying out and repainting every widget takes ~0.1-0.3 s on a high-DPI
    # screen, so doing it on every step of a drag-resize is choppy. While the
    # size keeps changing, pin the content at its old size (only the bare
    # window background around it changes); fill the window once it settles.
    def _on_configure(self, event):
        if event.widget is not self or self.state() == "iconic":
            return
        size = (event.width, event.height)
        if size == self._size:
            return  # just moved
        self._size = size
        if self._thaw_job:
            self.after_cancel(self._thaw_job)
        else:  # runs before pack re-lays out the content, which happens when idle
            c = self._content
            c.configure(width=c.winfo_width(), height=c.winfo_height())
            c.grid_propagate(False)
            c.pack_configure(fill="none", expand=False)
        self._thaw_job = self.after(150, self._thaw)

    def _thaw(self):
        self._thaw_job = None
        self._content.grid_propagate(True)
        self._content.pack_configure(fill="both", expand=True)


def new_profile(name="New game", save_dir="", exe="", rate=2.0):
    return {"id": uuid.uuid4().hex, "name": name, "save_dir": save_dir,
            "exe": exe, "saves_per_minute": rate, "enabled": True}


def describe_age(ts: float) -> str:
    secs = int(time.time() - ts)
    for unit, size in (("day", 86400), ("h", 3600), ("min", 60)):
        if secs >= size:
            n = secs // size
            return f"{n} {unit}{'s' if unit == 'day' and n > 1 else ''} ago"
    return "just now"


class RestoreDialog(SmoothWindow, tk.Toplevel):
    """Lists a profile's zips and restores or deletes the selected one."""

    def __init__(self, app, prof):
        super().__init__(app)
        self.attributes("-alpha", 0.0)  # build invisibly, reveal when ready (see App)
        self.app, self.prof = app, prof
        self.save_dir = Path(prof["save_dir"])
        self.title(f"Restore – {prof['name']}")
        self.minsize(dp(520), dp(340))
        self.transient(app)
        set_window_icon(self)  # also creates the window, which style_title_bar needs
        bg = theme_bg(self, app.theme)
        style_title_bar(self, app.theme, bg)
        if bg:
            self.configure(background=bg)  # shows around the content while resizing

        frm = ttk.Frame(self, padding=dp(16))
        frm.pack(fill="both", expand=True, anchor="nw")
        frm.columnconfigure(0, weight=1)
        frm.rowconfigure(1, weight=1)
        ttk.Label(frm, text="Double-click a backup to restore it. The current save is "
                            "backed up first, so you can always undo.",
                  wraplength=dp(480)).grid(row=0, column=0, columnspan=2, sticky="w", pady=(dp(0), dp(10)))

        self.tree = ttk.Treeview(frm, columns=("age", "size", "note"), selectmode="browse")
        self.tree.heading("#0", text="Saved at")
        self.tree.heading("age", text="Age")
        self.tree.heading("size", text="Size")
        self.tree.heading("note", text="")
        self.tree.column("#0", width=dp(170))
        self.tree.column("age", width=dp(90))
        self.tree.column("size", width=dp(80), anchor="e")
        self.tree.column("note", width=dp(110))
        self.tree.grid(row=1, column=0, sticky="nsew")
        sb = ttk.Scrollbar(frm, orient="vertical", command=self.tree.yview)
        sb.grid(row=1, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.bind("<Double-1>", lambda e: self.do_restore())

        btns = ttk.Frame(frm)
        btns.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(dp(12), dp(0)))
        ttk.Button(btns, text="Restore", style="Accent.TButton",
                   command=self.do_restore).pack(side="left")
        ttk.Button(btns, text="Delete", command=self.do_delete).pack(side="left", padx=dp(6))
        ttk.Button(btns, text="Refresh", command=self.refresh).pack(side="left")
        ttk.Button(btns, text="Open zips folder", command=self.open_folder).pack(side="left", padx=dp(6))
        ttk.Button(btns, text="Close", command=self.destroy).pack(side="right")

        self.refresh()
        self.update()  # lay out *and* paint everything before revealing
        self.attributes("-alpha", 1.0)
        self.smooth_window(frm)

    def on_shown(self):
        # Windows hides this (transient) window while the main one is minimized,
        # and its title bar comes back in the default (light) style.
        style_title_bar(self, self.app.theme, theme_bg(self, self.app.theme))

    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        try:
            current = int(folder_mtime(self.save_dir))
        except OSError:
            current = None
        for z in zips_for(self.save_dir):
            st = z.stat()
            note = "   ← current save" if int(st.st_mtime) == current else ""
            self.tree.insert("", "end", iid=str(z), text=z.stem[:19].replace("_", "  "),
                             values=(describe_age(st.st_mtime), f"{st.st_size / 1024:,.0f} KB", note))
        kids = self.tree.get_children()
        if kids:
            self.tree.selection_set(kids[0])
            self.tree.focus(kids[0])

    def chosen(self):
        sel = self.tree.selection()
        return Path(sel[0]) if sel else None

    def do_restore(self):
        z = self.chosen()
        if not z:
            return
        prof = self.prof
        log = lambda m: self.app.log(f"[{prof['name']}] {m}")
        if prof["exe"] and is_running(prof["exe"]):
            if not messagebox.askyesno(
                    "copy-save", f"{Path(prof['exe']).name} is running.\n\n"
                    "Games usually keep progress in memory and may overwrite the restored "
                    "save or lock its files. Exit the game first if you can.\n\n"
                    "Restore anyway?", icon="warning", default="no", parent=self):
                return
        if not messagebox.askyesno(
                "copy-save", f"Replace the current save in\n{self.save_dir}\n"
                f"with the backup from {z.stem.replace('_', ' ')}?", parent=self):
            return

        backup = snapshot(self.save_dir, log)
        if backup is None and not messagebox.askyesno(
                "copy-save", "Couldn't back up the current save first (see log).\n\n"
                "Restore anyway? The current save will be lost.",
                icon="warning", default="no", parent=self):
            return
        try:
            restore(z, self.save_dir, log)
        except Exception as e:
            log(f"Restore failed: {e}")
            messagebox.showerror("copy-save", "Restore failed, current save left as it was:"
                                 f"\n\n{e}", parent=self)
            return
        self.refresh()
        messagebox.showinfo("copy-save", f"Restored {z.stem.replace('_', ' ')}.\n\n"
                            "Load the latest checkpoint in the game to continue from it.",
                            parent=self)

    def do_delete(self):
        z = self.chosen()
        if z and messagebox.askyesno("copy-save", f"Delete backup {z.name}?",
                                     default="no", parent=self):
            z.unlink(missing_ok=True)
            self.app.log(f"[{self.prof['name']}] Deleted {z.name}")
            self.refresh()

    def open_folder(self):
        d = self.save_dir.parent / ZIP_DIR_NAME
        if d.is_dir():
            os.startfile(d)
        else:
            messagebox.showinfo("copy-save", f"{d} doesn't exist yet.", parent=self)


class App(SmoothWindow, tk.Tk):
    def __init__(self):
        set_app_id()
        super().__init__()
        # Windows shows the window as soon as we ask for its handle (title bar,
        # icons), so keep it transparent while widgets are built and themed;
        # otherwise each step visibly redraws. Revealed at the end of __init__.
        self.attributes("-alpha", 0.0)
        global UI_SCALE
        UI_SCALE = self.winfo_fpixels("1i") / 96
        self.title("copy-save")
        set_window_icon(self)
        self.minsize(dp(780), dp(500))
        self.theme = None
        self.theme_pref = None  # "light"/"dark" once chosen with the switch
        self.monitor = None
        self.log_queue = queue.Queue()
        self.status = {}        # profile id -> True (game running) / False / None
        self.loading = False    # suppresses write-back while filling the form
        self.icon_cache = {}    # (exe path, enabled) -> PhotoImage for the profile list

        self.profiles = self.load_config()
        self.selected = None

        main = ttk.Frame(self, padding=dp(16))
        main.pack(fill="both", expand=True, anchor="nw")
        main.columnconfigure(1, weight=1)
        main.rowconfigure(3, weight=1)

        # --- profile list
        left = ttk.Frame(main)
        left.grid(row=0, column=0, sticky="ns", padx=(dp(0), dp(16)))
        ttk.Label(left, text="Games", font=("Segoe UI Semibold", 11)).pack(anchor="w", pady=(dp(0), dp(6)))
        self.tree = ttk.Treeview(left, show="tree", selectmode="browse", height=7)
        self.tree.column("#0", width=dp(220))
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self.on_select)
        lb_btns = ttk.Frame(left)
        lb_btns.pack(fill="x", pady=(dp(8), dp(0)))
        ttk.Button(lb_btns, text="Add", command=self.add_profile).pack(side="left", fill="x", expand=True)
        ttk.Button(lb_btns, text="Remove", command=self.remove_profile).pack(
            side="left", fill="x", expand=True, padx=(dp(6), dp(0)))

        # --- profile editor
        self.form = ttk.LabelFrame(main, text="Profile", padding=dp(12))
        self.form.grid(row=0, column=1, sticky="nsew")
        self.form.columnconfigure(1, weight=1)
        self.name_var = tk.StringVar()
        self.save_var = tk.StringVar()
        self.exe_var = tk.StringVar()
        self.rate_var = tk.StringVar()
        self.enabled_var = tk.BooleanVar()

        rows = [("Name", self.name_var, None),
                ("Save folder", self.save_var, self.pick_folder),
                ("Game executable", self.exe_var, self.pick_exe)]
        for r, (label, var, browse) in enumerate(rows):
            ttk.Label(self.form, text=label).grid(row=r, column=0, sticky="w", pady=dp(4))
            ttk.Entry(self.form, textvariable=var).grid(row=r, column=1, sticky="ew", padx=dp(8))
            if browse:
                ttk.Button(self.form, text="Browse…", command=browse).grid(row=r, column=2, sticky="ew")
        ttk.Label(self.form, text="Saves per minute").grid(row=3, column=0, sticky="w", pady=dp(4))
        ttk.Spinbox(self.form, from_=0.5, to=60, increment=0.5, width=8,
                    textvariable=self.rate_var).grid(row=3, column=1, sticky="w", padx=dp(8))
        ttk.Checkbutton(self.form, text="Back up this game", style="Switch.TCheckbutton",
                        variable=self.enabled_var).grid(row=4, column=1, sticky="w", padx=dp(8), pady=(dp(8), dp(0)))
        ttk.Button(self.form, text="Restore…", command=self.open_restore).grid(
            row=4, column=2, sticky="ew", pady=(dp(8), dp(0)))

        for var in (self.name_var, self.save_var, self.exe_var, self.rate_var, self.enabled_var):
            var.trace_add("write", self.on_edit)

        # --- controls + log
        bar = ttk.Frame(main)
        bar.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(dp(16), dp(12)))
        self.start_btn = ttk.Button(bar, text="Start monitoring", style="Accent.TButton",
                                    width=18, command=self.toggle)
        self.start_btn.pack(side="left")
        self.status_lbl = ttk.Label(bar, text="Not monitoring")
        self.status_lbl.pack(side="left", padx=dp(12))
        self.dark_var = tk.BooleanVar()
        ttk.Checkbutton(bar, text="Dark mode", style="Switch.TCheckbutton",
                        variable=self.dark_var, command=self.on_dark_toggle).pack(side="right")

        ttk.Label(main, text="Activity", font=("Segoe UI Semibold", 11)).grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(dp(0), dp(6)))
        log_frame = ttk.Frame(main, style="Card.TFrame", padding=dp(1))
        log_frame.grid(row=3, column=0, columnspan=2, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log_box = tk.Text(log_frame, height=10, state="disabled", wrap="word",
                               relief="flat", borderwidth=0, highlightthickness=0,
                               padx=dp(10), pady=dp(8), font=("Cascadia Mono", 10))
        self.log_box.grid(row=0, column=0, sticky="nsew")
        log_sb = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_box.yview)
        log_sb.grid(row=0, column=1, sticky="ns")
        self.log_box.configure(yscrollcommand=log_sb.set)

        self.apply_theme()
        self.refresh_list()
        if self.profiles:
            self.select(0)
        else:
            self.set_form_state("disabled")

        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(200, self.drain_queue)
        self.after(3000, self.watch_theme)
        self.update()  # lay out *and* paint everything before revealing
        self.attributes("-alpha", 1.0)
        no_repaint_on_resize(self)  # class-wide: covers the Restore window too
        self.smooth_window(main)

    # ---- theme
    def apply_theme(self):
        theme = self.theme_pref or system_theme()
        self.dark_var.set(theme == "dark")
        if theme == self.theme:
            return
        # Restyling repaints every widget, one by one; on visible windows, do
        # it underneath a snapshot and swap the new look in at once.
        windows = [self, *[w for w in self.winfo_children() if isinstance(w, RestoreDialog)]]
        covers = [c for c in map(cover_window, windows) if c] if self.theme else []
        self.theme = theme
        if sv_ttk:
            sv_ttk.set_theme(theme)
            speed_up_sv_ttk(self)
            scale_theme_fonts(self)
            # sv-ttk forces a "selected" text colour that hides the faded/green
            # row colours; its selection is just a subtle tint, so drop it.
            ttk.Style().map("Treeview", foreground=[])
        fit_row_height(self)
        style = ttk.Style()
        bg = style.lookup("TEntry", "fieldbackground") or ("#1c1c1c" if theme == "dark" else "#fafafa")
        fg = style.lookup("TLabel", "foreground") or ("#fafafa" if theme == "dark" else "#1c1c1c")
        self.log_box.configure(bg=bg, fg=fg, insertbackground=fg,
                               selectbackground=style.lookup("Treeview", "selectbackground") or "#0078d4")
        self.tree.tag_configure("running", foreground="#6ccb5f" if theme == "dark" else "#0f7b0f")
        # Windows' own "disabled text" greys for each theme
        self.tree.tag_configure("off", foreground="#6a6a6a" if theme == "dark" else "#a3a3a3")
        # Windows fades the title bar colour over ~250 ms; start it before the
        # (slow) repaint so both finish under the snapshot.
        win_bg = theme_bg(self, theme)
        for win in windows:
            style_title_bar(win, theme, win_bg)
            if win_bg:
                win.configure(background=win_bg)  # shows around the content while resizing
        if covers:
            started = time.perf_counter()
            self.update()  # restyle and paint everything, unseen
            waited = int(1000 * (time.perf_counter() - started))
            for cover in covers:
                self.after(max(0, 300 - waited), cover.destroy)

    def watch_theme(self):
        """Follow Windows light/dark switches while the app is open (unless the
        user picked a theme with the switch)."""
        self.apply_theme()
        self.after(3000, self.watch_theme)

    def on_dark_toggle(self):
        self.theme_pref = "dark" if self.dark_var.get() else "light"
        self.save_config()
        self.apply_theme()

    # ---- config
    def load_config(self):
        if (CONFIG_FILE == DEFAULT_CONFIG_FILE and not CONFIG_FILE.exists()
                and LEGACY_CONFIG_FILE.exists()):  # migrate from next to the script
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            shutil.copy2(LEGACY_CONFIG_FILE, CONFIG_FILE)
        try:
            cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        self.theme_pref = cfg.get("theme")  # None = follow Windows
        if "profiles" in cfg:
            return cfg["profiles"]
        if cfg.get("save_dir"):  # single-game config from the first version
            return [new_profile(Path(cfg["save_dir"]).name,
                                cfg["save_dir"], cfg.get("exe", ""),
                                cfg.get("saves_per_minute", 2.0))]
        return []

    def save_config(self):
        cfg = {"profiles": self.profiles}
        if self.theme_pref:
            cfg = {"theme": self.theme_pref, **cfg}
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

    # ---- profile list
    def icon_for(self, prof):
        """The game's exe icon (greyed out if disabled), or an empty circle.

        Every row gets an image of the same size, so names stay aligned.
        """
        enabled = prof.get("enabled", True)
        key = (prof["exe"], enabled)
        if key not in self.icon_cache:
            size = dp(20)
            rgba = exe_icon_rgba(prof["exe"], size)
            if rgba is None:
                rgba = ring_rgba(size)
                if not enabled:
                    rgba = dim_rgba(rgba)
            elif not enabled:
                rgba = dim_rgba(rgba)
            png = base64.b64encode(png_from_rgba(size, rgba))
            self.icon_cache[key] = tk.PhotoImage(data=png, format="png")
        return self.icon_cache[key]

    def row_for(self, prof):
        """Tree label, icon and tag for a profile's state."""
        image = self.icon_for(prof)
        if not prof.get("enabled", True):
            return f"  {prof['name']}", image, "off"                   # disabled
        if self.status.get(prof["id"]):
            return f"  {prof['name']}  · running", image, "running"    # being backed up
        return f"  {prof['name']}", image, ""                          # enabled, waiting

    def refresh_list(self):
        self.tree.delete(*self.tree.get_children())
        for prof in self.profiles:
            text, image, tag = self.row_for(prof)
            self.tree.insert("", "end", iid=prof["id"], text=text, image=image, tags=(tag,))
        if self.selected is not None:
            self.tree.selection_set(self.profiles[self.selected]["id"])

    def refresh_row(self, i):
        text, image, tag = self.row_for(self.profiles[i])
        self.tree.item(self.profiles[i]["id"], text=text, image=image, tags=(tag,))

    def select(self, i):
        self.selected = i
        self.tree.selection_set(self.profiles[i]["id"])
        prof = self.profiles[i]
        self.loading = True
        self.name_var.set(prof["name"])
        self.save_var.set(prof["save_dir"])
        self.exe_var.set(prof["exe"])
        self.rate_var.set(f"{prof['saves_per_minute']:g}")
        self.enabled_var.set(prof.get("enabled", True))
        self.loading = False
        self.set_form_state("normal")

    def set_form_state(self, state):
        for child in self.form.winfo_children():
            try:
                child.configure(state=state)
            except tk.TclError:
                pass

    def on_select(self, _event):
        sel = self.tree.selection()
        # selection_set() fires this event too; ignore the echo so the form
        # isn't reloaded (and the cursor reset) while typing.
        if sel and self.tree.index(sel[0]) != self.selected:
            self.select(self.tree.index(sel[0]))

    def add_profile(self):
        self.profiles.append(new_profile())
        self.save_config()
        self.refresh_list()
        self.select(len(self.profiles) - 1)

    def remove_profile(self):
        if self.selected is None:
            return
        prof = self.profiles[self.selected]
        if not messagebox.askyesno("copy-save", f"Remove profile “{prof['name']}”?\n"
                                   "(Existing zips are not deleted.)"):
            return
        del self.profiles[self.selected]
        self.save_config()
        self.selected = None
        self.refresh_list()
        if self.profiles:
            self.select(0)
        else:
            self.loading = True
            for var in (self.name_var, self.save_var, self.exe_var, self.rate_var):
                var.set("")
            self.loading = False
            self.set_form_state("disabled")

    def on_edit(self, *_):
        if self.loading or self.selected is None:
            return
        prof = self.profiles[self.selected]
        prof["name"] = self.name_var.get().strip() or "Unnamed"
        prof["save_dir"] = self.save_var.get().strip()
        prof["exe"] = self.exe_var.get().strip()
        prof["enabled"] = self.enabled_var.get()
        try:
            rate = float(self.rate_var.get())
            if rate > 0:
                prof["saves_per_minute"] = rate
        except ValueError:
            pass  # keep the previous rate while the user is mid-typing
        self.save_config()
        self.refresh_row(self.selected)

    # ---- form actions
    def pick_folder(self):
        d = filedialog.askdirectory(initialdir=self.save_var.get() or None)
        if d:
            self.save_var.set(os.path.normpath(d))

    def pick_exe(self):
        f = filedialog.askopenfilename(filetypes=[("Executables", "*.exe"), ("All files", "*.*")])
        if f:
            self.exe_var.set(os.path.normpath(f))
            if self.name_var.get() in ("", "New game"):
                self.name_var.set(Path(f).stem)

    def open_restore(self):
        if self.selected is None:
            return
        prof = self.profiles[self.selected]
        if not Path(prof["save_dir"]).is_dir():
            messagebox.showerror("copy-save", "Save folder doesn't exist.")
            return
        RestoreDialog(self, prof)

    # ---- monitoring
    def toggle(self):
        if self.monitor:
            self.monitor.stop()
            self.monitor = None
            self.status.clear()
            self.refresh_list()
            self.start_btn.config(text="Start monitoring")
            self.status_lbl.config(text="Not monitoring")
            self.log("Stopped.")
            return
        enabled = [p["name"] for p in self.profiles if p.get("enabled", True)]
        if not enabled:
            messagebox.showerror("copy-save", "No enabled profiles.")
            return
        self.log(f"Monitoring: {', '.join(enabled)}")
        # Hand the thread a fresh copy each tick so GUI edits never race with it.
        self.monitor = Monitor(lambda: [dict(p) for p in self.profiles], self.log,
                               lambda pid, running: self.log_queue.put(("status", pid, running)))
        self.monitor.start()
        self.start_btn.config(text="Stop monitoring")
        self.status_lbl.config(text=f"Watching {len(enabled)} game{'s' * (len(enabled) > 1)}")

    # ---- thread -> GUI plumbing
    def log(self, msg):
        self.log_queue.put(("log", f"[{datetime.now():%H:%M:%S}] {msg}"))

    def drain_queue(self):
        while not self.log_queue.empty():
            item = self.log_queue.get_nowait()
            if item[0] == "log":
                self.log_box.config(state="normal")
                self.log_box.insert("end", item[1] + "\n")
                self.log_box.see("end")
                self.log_box.config(state="disabled")
            elif self.monitor:  # status update; ignore stragglers after Stop
                _, pid, running = item
                self.status[pid] = running
                for i, p in enumerate(self.profiles):
                    if p["id"] == pid:
                        self.refresh_row(i)
        self.after(200, self.drain_queue)

    def on_close(self):
        if self.monitor:
            self.monitor.stop()
        self.destroy()


if __name__ == "__main__":
    if not FROZEN:  # the packaged exe has its own icon and installer shortcut
        leave_store_python()
        ensure_start_menu_shortcut()
    enable_dpi_awareness()
    App().mainloop()
