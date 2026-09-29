"""Thunderstore's Balatro package registry.

A second, independent registry with its own submissions -- some mods are here
and not in the community index. One request returns every package with its
full version history.

Thunderstore's own packaging layer shows up in the data and has to be filtered:
`Thunderstore-lovely` and similar are repackages of things we already track,
and deprecated packages are still served by the API.
"""

from __future__ import annotations

from .. import config, net

SOURCE = "thunderstore"

# Thunderstore's modman scaffolding, not Balatro mods.
_SKIP_PACKAGES = {"lovely", "BepInExPack"}


def fetch(log=print) -> list[dict]:
    log("thunderstore: fetching package list")
    packages = net.get_json(config.THUNDERSTORE_API)
    if not isinstance(packages, list):
        log("thunderstore: unexpected response shape, skipping source")
        return []

    entries: list[dict] = []
    deprecated = 0
    for package in packages:
        if package.get("is_deprecated"):
            deprecated += 1
            continue
        name = str(package.get("name") or "").strip()
        if not name or name in _SKIP_PACKAGES:
            continue
        versions = package.get("versions") or []
        if not versions:
            continue
        # Thunderstore returns versions newest-first.
        latest = versions[0]
        entries.append(
            {
                "source": SOURCE,
                "title": name.replace("_", " ").strip(),
                "author": str(package.get("owner") or "").strip(),
                # website_url usually points at the source repo, which is what
                # lets merge.py spot index/Thunderstore duplicates.
                "repo": str(latest.get("website_url") or "").strip(),
                "download_url": str(latest.get("download_url") or "").strip(),
                "declared_version": str(latest.get("version_number") or "").strip(),
                "categories": list(package.get("categories") or []),
                "description": str(latest.get("description") or "").strip(),
                "requires_steamodded": any(
                    "steamodded" in str(d).lower()
                    for d in (latest.get("dependencies") or [])
                ),
                "requires_talisman": any(
                    "talisman" in str(d).lower()
                    for d in (latest.get("dependencies") or [])
                ),
                "folder_name": "",
                "package_url": str(package.get("package_url") or ""),
                "thunderstore_dependencies": list(latest.get("dependencies") or []),
                "raw": {"package": {k: v for k, v in package.items() if k != "versions"},
                        "latest": latest},
            }
        )

    log(f"thunderstore: {len(entries)} packages ({deprecated} deprecated, skipped)")
    return entries
