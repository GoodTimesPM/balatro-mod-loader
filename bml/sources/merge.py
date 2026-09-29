"""Merging three sources into one catalog without duplicating mods.

Names and authors disagree between sources, so the join is the GitHub repo
URL normalised to `owner/name`, falling back to a normalised title.

Precedence is local > index > thunderstore. Losing entries' download URLs are
kept as fallbacks for `fetch`.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from . import index_repo, local_index, thunderstore

_PRECEDENCE = {"local": 0, "index": 1, "thunderstore": 2}

_GITHUB_HOSTS = {"github.com", "www.github.com", "codeload.github.com",
                 "raw.githubusercontent.com"}


def collect(log=print, only: str | None = None) -> tuple[list[dict], list[str]]:
    """Fetch every source and merge.

    Returns `(mods, failed_sources)`; the caller must not prune after a failure.
    """
    modules = [
        ("local", local_index),
        ("index", index_repo),
        ("thunderstore", thunderstore),
    ]
    raw: list[dict] = []
    failed: list[str] = []
    for name, module in modules:
        if only and only != name:
            continue
        try:
            raw.extend(module.fetch(log=log))
        except Exception as exc:  # one dead source must not kill the sync
            log(f"{name}: FAILED ({type(exc).__name__}: {exc}) -- continuing")
            failed.append(name)

    return merge(raw, log=log), failed


def merge(entries: list[dict], log=print) -> list[dict]:
    buckets: dict[str, list[dict]] = {}
    for entry in entries:
        buckets.setdefault(identity(entry), []).append(entry)
    buckets = _fold_moved_repos(buckets, log=log)

    merged: list[dict] = []
    collisions = 0
    for key, group in buckets.items():
        group.sort(key=lambda e: _PRECEDENCE.get(e.get("source", ""), 9))
        winner = dict(group[0])
        winner["key"] = key
        if len(group) > 1:
            collisions += len(group) - 1
            winner["alternates"] = [
                {
                    "source": other.get("source"),
                    "download_url": other.get("download_url"),
                    "declared_version": other.get("declared_version"),
                    "repo": other.get("repo"),
                }
                for other in group[1:]
            ]
            # Categories are additive: a Thunderstore label is still a useful
            # hint even when the index entry wins on everything else.
            categories = list(winner.get("categories") or [])
            for other in group[1:]:
                for category in other.get("categories") or []:
                    if category not in categories:
                        categories.append(category)
            winner["categories"] = categories
            winner["also_in"] = sorted({o.get("source", "") for o in group[1:]})
        # Only `raw` is stored, so merge output has to ride inside it.
        winner["raw"] = {
            **(winner.get("raw") or {}),
            "_alternates": winner.get("alternates") or [],
            "_also_in": winner.get("also_in") or [],
            "_merged_categories": winner.get("categories") or [],
        }
        merged.append(winner)

    merged.sort(key=lambda e: (e.get("title") or "").lower())
    log(f"merge: {len(entries)} raw entries -> {len(merged)} mods "
        f"({collisions} duplicates folded in)")
    return merged


def _fold_moved_repos(buckets: dict[str, list[dict]], log=print) -> dict[str, list[dict]]:
    """Second pass: same author and title, different repo URL.

    Catches projects that moved repos, like Steamodded did.
    """
    by_name: dict[tuple[str, str], str] = {}
    folded: dict[str, list[dict]] = {}
    merges = 0

    # Repo-keyed buckets are the anchors; name-keyed ones fold into them.
    for key in sorted(buckets, key=lambda k: (not k.startswith("repo:"), k)):
        group = buckets[key]
        name_key = (normalise(group[0].get("author") or ""),
                    normalise(group[0].get("title") or ""))
        target = by_name.get(name_key) if all(name_key) else None
        if target is not None and target != key:
            folded[target].extend(group)
            merges += 1
            log(f"merge: folded {key} into {target} (same author+title, repo moved)")
            continue
        folded[key] = list(group)
        if all(name_key):
            by_name.setdefault(name_key, key)

    if merges:
        log(f"merge: {merges} entry group(s) folded on author+title after a repo move")
    return folded


def identity(entry: dict) -> str:
    """A stable dedup key: repo slug when known, else a normalised title."""
    slug = repo_slug(entry.get("repo") or "")
    if slug:
        return f"repo:{slug}"
    title = normalise(entry.get("title") or "")
    author = normalise(entry.get("author") or "")
    if title and author:
        return f"name:{author}@{title}"
    return f"name:{title or entry.get('download_url', '')}"


def repo_slug(url: str) -> str:
    """`https://github.com/Aikoyori/Vocalatro/` -> `aikoyori/vocalatro`."""
    url = (url or "").strip()
    if not url:
        return ""
    try:
        parsed = urlparse(url if "://" in url else f"https://{url}")
    except ValueError:
        return ""
    if parsed.netloc.lower() not in _GITHUB_HOSTS:
        return ""
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 2:
        return ""
    owner, name = parts[0], parts[1]
    # Trim the tail of an archive URL: `/archive/refs/heads/main.zip`.
    name = re.sub(r"\.git$", "", name)
    return f"{owner.lower()}/{name.lower()}"


def normalise(text: str) -> str:
    """Case-, space-, and separator-insensitive comparison form."""
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())
