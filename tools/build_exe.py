"""Build `dist/Balatro Mod Loader.exe` — the double-clickable window.

    py -m pip install pyinstaller
    py tools/build_exe.py                 # build
    py tools/build_exe.py --shortcut      # build, then put it on the Desktop
    py tools/build_exe.py --console       # same, but keep a console for errors
    py tools/build_exe.py --release       # the public build: no path to this checkout

One file, windowed, icon embedded. The exe runs `bml.gui.run()`. Data (the
library, catalog, profiles, `.env`) stays on disk and is never bundled. Output
goes to `build/` and `dist/`, both gitignored.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NAME = "Balatro Mod Loader"
ICON = ROOT / "assets" / "bml.ico"
ENTRY = ROOT / "bml-gui.pyw"


def build(console: bool = False, clean: bool = False, release: bool = False) -> Path:
    if not ICON.is_file():
        print("assets/bml.ico is missing — running tools/make_icon.py first")
        subprocess.run([sys.executable, str(ROOT / "tools" / "make_icon.py")],
                       check=True)

    # `bml_home.txt` tells a local build where its data lives (see
    # `config._resolve_root`). A release build leaves it out, so a new user's
    # first launch opens the setup wizard.
    home_file = ROOT / "build" / "bml_home.txt"
    home_file.parent.mkdir(parents=True, exist_ok=True)
    if release:
        home_file.unlink(missing_ok=True)
    else:
        home_file.write_text(str(ROOT), encoding="utf-8")

    sep = ";" if sys.platform == "win32" else ":"
    command = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--onefile",
        "--name", NAME,
        "--icon", str(ICON),
        # The icon goes on the exe for Explorer and inside it for the window.
        "--add-data", f"{ICON}{sep}assets",
        # PIL is imported inside a function, so PyInstaller needs telling.
        "--hidden-import", "PIL.Image",
        "--hidden-import", "PIL.ImageTk",
        "--collect-submodules", "bml",
        "--distpath", str(ROOT / "dist"),
        "--workpath", str(ROOT / "build"),
        "--specpath", str(ROOT / "build"),
    ]
    if not release:
        command[command.index("--hidden-import"):command.index("--hidden-import")] = [
            "--add-data", f"{home_file}{sep}."]
    command.append("--console" if console else "--windowed")
    if clean:
        command.append("--clean")
    command.append(str(ENTRY))

    exe = ROOT / "dist" / f"{NAME}.exe"
    if exe.is_file() and _is_locked(exe):
        raise SystemExit(
            f"{exe.name} is open — close the window and run this again.\n"
            "PyInstaller reports that as PermissionError [WinError 5] near the\n"
            "very end of a two-minute build, which reads like a build failure.")

    print("$ " + " ".join(command))
    subprocess.run(command, check=True, cwd=str(ROOT))
    return exe


def _is_locked(path: Path) -> bool:
    """Is a running copy of the exe holding this file open?

    Windows refuses to overwrite a running executable.
    """
    try:
        with path.open("ab"):
            return False
    except OSError:
        return True


def make_shortcut(target: Path) -> Path | None:
    """Drop a Desktop shortcut pointing at the built exe.

    Opt-in, via WScript.Shell.
    """
    if sys.platform != "win32":
        print("shortcuts are a Windows thing; skipping")
        return None
    script = (
        "$desktop = [Environment]::GetFolderPath('Desktop');"
        f"$link = Join-Path $desktop '{NAME}.lnk';"
        "$shell = New-Object -ComObject WScript.Shell;"
        "$sc = $shell.CreateShortcut($link);"
        f"$sc.TargetPath = '{target}';"
        f"$sc.WorkingDirectory = '{target.parent}';"
        f"$sc.IconLocation = '{ICON}';"
        "$sc.Description = 'Balatro Mod Loader';"
        "$sc.Save();"
        "Write-Output $link"
    )
    done = subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                           "-Command", script],
                          capture_output=True, text=True)
    if done.returncode != 0:
        print("could not create the shortcut:", done.stderr.strip())
        return None
    path = Path(done.stdout.strip())
    print(f"shortcut: {path}")
    return path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--shortcut", action="store_true",
                        help="also put a shortcut on the Desktop")
    parser.add_argument("--console", action="store_true",
                        help="keep a console window (for debugging a crash)")
    parser.add_argument("--clean", action="store_true",
                        help="discard PyInstaller's cache first")
    parser.add_argument("--release", action="store_true",
                        help="build for publishing: no bml_home.txt, so a new "
                             "user gets the setup wizard")
    args = parser.parse_args(argv)

    if shutil.which("pyinstaller") is None:
        try:
            import PyInstaller  # noqa: F401
        except ImportError:
            print("PyInstaller is not installed:\n\n    py -m pip install pyinstaller")
            return 1

    exe = build(console=args.console, clean=args.clean, release=args.release)
    if not exe.is_file():
        print(f"build finished but {exe} is not there")
        return 1
    print(f"\nbuilt {exe}  ({exe.stat().st_size / 1024 / 1024:.1f} MB)")
    if args.shortcut:
        make_shortcut(exe)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
