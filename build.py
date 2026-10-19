"""Build copy-save.exe (PyInstaller) and the installer (Inno Setup).

    pip install -r requirements.txt pyinstaller
    python build.py            # -> dist/copy-save-<version>-setup.exe
    python build.py --no-installer   # just dist/copy-save/copy-save.exe

Inno Setup 6 must be installed for the installer step (https://jrsoftware.org/isinfo.php).
"""
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BUILD = ROOT / "build"
DIST = ROOT / "dist"


def app_version() -> str:
    src = (ROOT / "copy_save.pyw").read_text(encoding="utf-8")
    return re.search(r'^__version__ = "([^"]+)"', src, re.M).group(1)


def write_version_file(version: str) -> Path:
    """Windows version resource, shown in the exe's Properties > Details."""
    nums = (version.split(".") + ["0"] * 4)[:4]
    tup = ", ".join(str(int(n)) for n in nums)
    path = BUILD / "version_info.txt"
    path.parent.mkdir(exist_ok=True)
    path.write_text(f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers=({tup}), prodvers=({tup})),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('FileDescription', 'copy-save - game autosave snapshots'),
      StringStruct('ProductName', 'copy-save'),
      StringStruct('FileVersion', '{version}'),
      StringStruct('ProductVersion', '{version}'),
      StringStruct('OriginalFilename', 'copy-save.exe'),
      StringStruct('InternalName', 'copy-save')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
""", encoding="utf-8")
    return path


def find_iscc() -> str:
    candidates = [
        shutil.which("iscc"),
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"),
        os.path.expandvars(r"%ProgramFiles%\Inno Setup 6\ISCC.exe"),
    ]
    for c in candidates:
        if c and Path(c).is_file():
            return c
    sys.exit("Inno Setup 6 (ISCC.exe) not found; install it or use --no-installer.")


def main():
    version = app_version()
    print(f"Building copy-save {version}")
    shutil.rmtree(DIST / "copy-save", ignore_errors=True)

    subprocess.run([
        sys.executable, "-m", "PyInstaller", "copy_save.pyw",
        "--name", "copy-save",
        "--windowed",                      # no console window
        "--noconfirm", "--clean",
        # absolute paths: relative ones would resolve against --specpath
        "--icon", str(ROOT / "copy_save.ico"),               # exe / taskbar icon
        "--add-data", f"{ROOT / 'copy_save.ico'}{os.pathsep}.",  # title bar icon
        "--collect-data", "sv_ttk",        # theme images and Tcl files
        "--version-file", str(write_version_file(version)),
        "--distpath", str(DIST), "--workpath", str(BUILD), "--specpath", str(BUILD),
    ], cwd=ROOT, check=True)
    print(f"-> {DIST / 'copy-save' / 'copy-save.exe'}")

    if "--no-installer" in sys.argv:
        return
    subprocess.run([find_iscc(), f"/DAppVersion={version}", "installer.iss"],
                   cwd=ROOT, check=True)
    print(f"-> {DIST / f'copy-save-{version}-setup.exe'}")


if __name__ == "__main__":
    main()
