"""The community mod index -- the public mirror of the Discord's mod list.

`skyline69/balatro-mod-index` stores one folder per mod, `mods/Author@ModName/`,
each holding a `meta.json` and a `description.md`.

Pulled as one tarball, since walking the contents API would blow through the
60-an-hour unauthenticated limit.
"""

from __future__ import annotations

import io
import json
import tarfile

from .. import config, net

SOURCE = "index"


def fetch(log=print) -> list[dict]:
    log(f"index: downloading {config.INDEX_REPO} tarball")
    response = net.session().get(
        config.INDEX_TARBALL, timeout=config.DOWNLOAD_TIMEOUT
    )
    response.raise_for_status()

    entries: list[dict] = []
    skipped = 0
    with tarfile.open(fileobj=io.BytesIO(response.content), mode="r:gz") as tar:
        for member in tar.getmembers():
            if not member.isfile() or not member.name.endswith("/meta.json"):
                continue
            # `<repo>-main/mods/Author@ModName/meta.json`
            parts = member.name.split("/")
            if len(parts) < 4 or parts[1] != "mods":
                continue
            folder = parts[2]
            handle = tar.extractfile(member)
            if handle is None:
                continue
            try:
                meta = json.loads(handle.read().decode("utf-8-sig"))
            except (ValueError, UnicodeDecodeError):
                skipped += 1
                continue
            entry = _to_entry(folder, meta)
            if entry:
                entries.append(entry)
            else:
                skipped += 1

    log(f"index: {len(entries)} mods ({skipped} entries skipped as unreadable)")
    return entries


def _to_entry(folder: str, meta: dict) -> dict | None:
    title = str(meta.get("title") or "").strip()
    if not title:
        return None
    author = str(meta.get("author") or "").strip()
    if not author and "@" in folder:
        author = folder.split("@", 1)[0]

    return {
        "source": SOURCE,
        "title": title,
        "author": author,
        "repo": str(meta.get("repo") or "").strip(),
        "download_url": str(meta.get("downloadURL") or "").strip(),
        "declared_version": str(meta.get("version") or "").strip(),
        "categories": list(meta.get("categories") or []),
        "description": "",
        "requires_steamodded": bool(meta.get("requires-steamodded")),
        "requires_talisman": bool(meta.get("requires-talisman")),
        "folder_name": str(meta.get("folderName") or "").strip(),
        "index_folder": folder,
        "raw": meta,
    }
