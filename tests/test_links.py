"""Junction handling, exercised against real NTFS junctions in a temp dir.

    py -m tests.test_links

Real junctions, because "removing a link never touches its target" is
filesystem behaviour a mock can't check. Everything happens under `tempfile`.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bml import links  # noqa: E402

FAILURES: list[str] = []


def check(label: str, actual, expected) -> None:
    if actual != expected:
        FAILURES.append(f"{label}: expected {expected!r}, got {actual!r}")


def ok(label: str, condition: bool) -> None:
    if not condition:
        FAILURES.append(label)


def raises(label: str, exc_types, fn) -> None:
    try:
        fn()
    except exc_types:
        return
    except Exception as exc:
        FAILURES.append(f"{label}: raised {type(exc).__name__} instead")
        return
    FAILURES.append(f"{label}: did not raise")


def _sandbox():
    root = Path(tempfile.mkdtemp(prefix="bml-test-"))
    vault = root / "vault"
    mods = root / "mods"
    (vault / "SomeMod" / "mod").mkdir(parents=True)
    (vault / "SomeMod" / "mod" / "main.lua").write_text("-- content", encoding="utf-8")
    mods.mkdir()
    return root, vault, mods


# -- Tests -----------------------------------------------------------------


def test_create_and_detect() -> None:
    root, vault, mods = _sandbox()
    try:
        target = vault / "SomeMod" / "mod"
        link = mods / "SomeMod"
        links.create(link, target)

        ok("junction detected", links.is_junction(link))
        ok("resolves to the target", links.target_of(link).resolve() == target.resolve())
        ok("points into the vault", links.points_into(link, vault))
        check("described as ours", links.describe(link, vault), "ours")
        # The game has to be able to read through it, or none of this works.
        check("file readable through the link",
              (link / "main.lua").read_text(encoding="utf-8"), "-- content")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_remove_does_not_touch_the_target() -> None:
    """The guarantee the vault depends on.

    `shutil.rmtree` on a junction deletes the files it points at. `os.rmdir`
    removes only the link. Disabling a mod must never be able to delete it.
    """
    root, vault, mods = _sandbox()
    try:
        target = vault / "SomeMod" / "mod"
        link = mods / "SomeMod"
        links.create(link, target)
        links.remove(link, vault)

        ok("link gone", not link.exists() and not links.is_junction(link))
        ok("target still there", target.is_dir())
        ok("target contents intact", (target / "main.lua").is_file())
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_refuses_to_remove_a_real_directory() -> None:
    """The mods folder may hold hand-installed mods."""
    root, vault, mods = _sandbox()
    try:
        real = mods / "HandInstalled"
        real.mkdir()
        (real / "keep.lua").write_text("precious", encoding="utf-8")

        raises("real directory refused", ValueError, lambda: links.remove(real, vault))
        ok("still there", (real / "keep.lua").is_file())
        check("described as real", links.describe(real, vault), "real")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_refuses_to_remove_a_junction_we_do_not_own() -> None:
    """A link the user made themselves is not ours to clean up."""
    root, vault, mods = _sandbox()
    try:
        elsewhere = root / "elsewhere"
        elsewhere.mkdir()
        link = mods / "TheirLink"
        links.create(link, elsewhere)

        check("described as foreign", links.describe(link, vault), "foreign-link")
        raises("foreign junction refused", ValueError, lambda: links.remove(link, vault))
        ok("still linked", links.is_junction(link))

        # Without a vault to check against it is removable -- that path exists
        # for callers that have already established ownership themselves.
        links.remove(link)
        ok("removed when unrestricted", not links.is_junction(link))
        ok("its target survived", elsewhere.is_dir())
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_create_refuses_to_clobber() -> None:
    root, vault, mods = _sandbox()
    try:
        target = vault / "SomeMod" / "mod"
        real = mods / "SomeMod"
        real.mkdir()
        raises("existing path refused", FileExistsError,
               lambda: links.create(real, target))
        raises("missing target refused", FileNotFoundError,
               lambda: links.create(mods / "Other", vault / "NoSuchMod" / "mod"))
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_names_with_spaces_and_punctuation() -> None:
    """Real vault folders are `Author@Mod Name (thing)` -- mklink via cmd.exe
    is the one place in this project where that could go wrong."""
    root, vault, mods = _sandbox()
    try:
        target = vault / "Some Author@Mod & Friends (v2)" / "mod"
        target.mkdir(parents=True)
        (target / "x.lua").write_text("ok", encoding="utf-8")
        link = mods / "Mod & Friends (v2)"
        links.create(link, target)
        ok("created", links.is_junction(link))
        check("readable", (link / "x.lua").read_text(encoding="utf-8"), "ok")
        links.remove(link, vault)
        ok("target survived", (target / "x.lua").is_file())
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_describe_absent() -> None:
    root, vault, mods = _sandbox()
    try:
        check("absent", links.describe(mods / "Nope", vault), "absent")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main() -> int:
    if sys.platform != "win32":
        print("test_links: skipped (junctions are Windows-only)")
        return 0
    for test in (
        test_create_and_detect,
        test_remove_does_not_touch_the_target,
        test_refuses_to_remove_a_real_directory,
        test_refuses_to_remove_a_junction_we_do_not_own,
        test_create_refuses_to_clobber,
        test_names_with_spaces_and_punctuation,
        test_describe_absent,
    ):
        test()
    if FAILURES:
        print(f"FAIL ({len(FAILURES)})")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("test_links: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
