"""Cross-source dedup.

    py -m tests.test_merge
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bml.sources.merge import merge, repo_slug  # noqa: E402

FAILURES: list[str] = []


def check(label: str, actual, expected) -> None:
    if actual != expected:
        FAILURES.append(f"{label}: expected {expected!r}, got {actual!r}")


def quiet(*args, **kwargs) -> None:
    pass


def test_repo_slug() -> None:
    check("plain repo", repo_slug("https://github.com/Aikoyori/Vocalatro/"),
          "aikoyori/vocalatro")
    check("archive URL",
          repo_slug("https://github.com/MathIsFun0/Cryptid/archive/refs/heads/main.zip"),
          "mathisfun0/cryptid")
    check("double slash after host",
          repo_slug("https://github.com//CeruleanKay/sharpcards"),
          "ceruleankay/sharpcards")
    check("not github", repo_slug("https://gitlab.com/a/b"), "")
    check("empty", repo_slug(""), "")


def test_same_repo_dedups() -> None:
    merged = merge([
        {"source": "index", "title": "Vocalatro", "author": "Aikoyori",
         "repo": "https://github.com/Aikoyori/Vocalatro",
         "download_url": "https://example.invalid/index.zip"},
        {"source": "thunderstore", "title": "Vocalatro", "author": "aikoyori",
         "repo": "https://github.com/Aikoyori/Vocalatro/",
         "download_url": "https://example.invalid/ts.zip"},
    ], log=quiet)
    check("one mod after dedup", len(merged), 1)
    check("index wins", merged[0]["source"], "index")
    check("loser kept as an alternate", len(merged[0]["alternates"]), 1)
    check("alternate carries its URL",
          merged[0]["alternates"][0]["download_url"], "https://example.invalid/ts.zip")


def test_moved_repo_still_dedups() -> None:
    """Steamodded migrated Steamopollys/Steamodded -> Steamodded/smods.

    Regression: the differing repo URLs defeated slug-based dedup.
    """
    merged = merge([
        {"source": "index", "title": "Steamodded", "author": "Steamodded",
         "repo": "https://github.com/Steamodded/smods",
         "declared_version": "1.0.0-beta-1814a",
         "download_url": "https://example.invalid/smods.zip"},
        {"source": "thunderstore", "title": "Steamodded", "author": "steamodded",
         "repo": "https://github.com/Steamopollys/Steamodded",
         "declared_version": "0.9.8",
         "download_url": "https://example.invalid/old.zip"},
    ], log=quiet)
    check("moved repo folded to one entry", len(merged), 1)
    check("the maintained repo wins", merged[0]["source"], "index")
    check("winning version", merged[0]["declared_version"], "1.0.0-beta-1814a")


def test_different_mods_stay_separate() -> None:
    """The name-based fold must not merge unrelated mods."""
    merged = merge([
        {"source": "index", "title": "Bunco", "author": "Firch",
         "repo": "https://github.com/Firch/Bunco",
         "download_url": "https://example.invalid/a.zip"},
        {"source": "index", "title": "Bunco", "author": "SomeoneElse",
         "repo": "https://github.com/SomeoneElse/Bunco",
         "download_url": "https://example.invalid/b.zip"},
        {"source": "thunderstore", "title": "Cryptid", "author": "MathIsFun0",
         "repo": "", "download_url": "https://example.invalid/c.zip"},
    ], log=quiet)
    check("same title, different authors stay apart", len(merged), 3)


def main() -> int:
    for fn in (test_repo_slug, test_same_repo_dedups,
               test_moved_repo_still_dedups, test_different_mods_stay_separate):
        fn()
    if FAILURES:
        print(f"FAIL ({len(FAILURES)})")
        for failure in FAILURES:
            print("  -", failure)
        return 1
    print("test_merge: all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
