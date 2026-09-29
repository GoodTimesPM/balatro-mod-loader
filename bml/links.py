"""Directory junctions: the mechanism that enables and disables a mod.

Enabling a mod creates a junction in `%APPDATA%\\Balatro\\Mods` pointing into
the vault; disabling deletes the junction. The vault keeps the only copy.
Junctions rather than symlinks, because symlinks need Developer Mode or admin.

Two rules, enforced here:

*Only ever delete a junction.* `os.rmdir` removes just the link, while
`shutil.rmtree` would delete the vault files behind it.

*Only ever delete a junction we own*, meaning one that points into the vault.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def is_junction(path: Path) -> bool:
    """True if `path` is a junction or a directory symlink.

    Both are reparse points and both are safe to `rmdir`; treating them the
    same way means a mods folder set up by hand with `mklink /D` still works.
    """
    try:
        return os.path.isjunction(path) or (path.is_symlink() and path.is_dir())
    except OSError:
        return False


def target_of(path: Path) -> Path | None:
    """What a junction points at, or None if `path` isn't one.

    `os.readlink` handles junctions on Windows and returns them in the
    `\\??\\C:\\...` device form, which is not a usable path -- strip the prefix.
    """
    if not is_junction(path):
        return None
    try:
        raw = os.readlink(path)
    except OSError:
        return None
    for prefix in ("\\\\?\\", "\\??\\"):
        if raw.startswith(prefix):
            raw = raw[len(prefix):]
            break
    return Path(raw)


def points_into(path: Path, root: Path) -> bool:
    """Does the junction at `path` resolve to somewhere inside `root`?

    This is the ownership test. It compares resolved paths so a vault reached
    through a different but equivalent spelling still counts as ours.
    """
    target = target_of(path)
    if target is None:
        return False
    try:
        target.resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        return False
    return True


def create(link: Path, target: Path) -> None:
    """Make `link` a junction to `target`. Fails loudly if it can't."""
    if not target.is_dir():
        raise FileNotFoundError(f"junction target does not exist: {target}")
    if link.exists() or is_junction(link):
        raise FileExistsError(f"refusing to overwrite existing path: {link}")
    link.parent.mkdir(parents=True, exist_ok=True)

    # `mklink` needs cmd.exe; list-form arguments keep odd folder names safe.
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True, text=True,
    )
    if result.returncode != 0 or not is_junction(link):
        detail = (result.stderr or result.stdout or "").strip()
        raise OSError(f"mklink failed for {link}: {detail or 'unknown error'}")


def remove(link: Path, vault_root: Path | None = None) -> None:
    """Delete a junction. Never deletes a real directory or its contents.

    `os.rmdir` on a junction unlinks it and leaves the target untouched, which
    is exactly what disabling a mod means. Passing `vault_root` additionally
    requires the junction to point into the vault, so a link the user made
    themselves is never collateral damage.
    """
    if not is_junction(link):
        raise ValueError(f"not a junction, refusing to remove: {link}")
    if vault_root is not None and not points_into(link, vault_root):
        raise ValueError(
            f"junction {link} points outside the vault ({target_of(link)}); "
            "not ours to remove"
        )
    os.rmdir(link)


def describe(path: Path, vault_root: Path) -> str:
    """One word for what `path` is, for planning and for `status`.

    The distinction that matters is `ours` versus everything else: `ours` is
    the only value any code in this project is allowed to delete.
    """
    if is_junction(path):
        return "ours" if points_into(path, vault_root) else "foreign-link"
    if path.is_dir():
        return "real"
    if path.exists():
        return "file"
    return "absent"
