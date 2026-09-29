"""Downloading a mod and placing it in the vault.

Layout, per mod:

    <library>\\<CATEGORY>\\<Author>@<ModName>\\
        mod\\                     the extracted mod, which gets junctioned
        _bml.json                 our record: manifest, hashes, provenance
        _archive\\<name>.zip       the original download, for rollback

Two properties matter more than anything else here:

*Nothing lands in the vault until it has fully succeeded.* Downloads are
staged, extracted and parsed in a temp directory, then moved into place.

*Archives are never trusted.* Extraction blocks `../` escapes and absolute
paths.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import tarfile
import tempfile
import zipfile
from pathlib import Path

from . import categorize, config, net
from .db import now
from .manifest import all_manifests, find_manifest, scan_smods_objects
from .sources.merge import repo_slug

_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def folder_name(entry: dict) -> str:
    """`Author@ModName`, matching the community index's own convention."""
    author = _safe(entry.get("author") or "unknown")
    title = _safe(entry.get("folder_name") or entry.get("title") or "unnamed")
    return f"{author}@{title}"


def _safe(text: str) -> str:
    cleaned = _UNSAFE.sub("-", str(text)).strip(" .")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:80] or "unnamed"


def fetch_one(entry: dict, log=print, force: bool = False) -> dict:
    """Download, inspect and place one mod. Returns a vault row for the DB.

    Never raises for one mod. Failures come back with status != "ok".
    """
    key = entry["key"]
    row = {
        "key": key,
        "mod_id": "",
        "category": "",
        "category_reason": "",
        "vault_path": "",
        "version": entry.get("declared_version", ""),
        "sha256": "",
        "archive_name": "",
        "manifest": "{}",
        "smods_objects": "{}",
        "status": "failed",
        "note": "",
        "fetched_at": now(),
    }

    source_dir = Path(entry["local_source"]) if entry.get("local_source") else None
    urls = [] if source_dir else _candidate_urls(entry)
    if source_dir is None and not urls:
        row["note"] = "no download URL in any source"
        return row
    if source_dir is not None and not source_dir.is_dir():
        row["note"] = f"localSource {source_dir} is not a directory"
        return row

    with tempfile.TemporaryDirectory(prefix="bml-") as tmp:
        tmpdir = Path(tmp)
        archive = None
        last_error = ""
        used_url = ""

        if source_dir is not None:
            # A folder on disk, zipped so it follows the same path as a download.
            try:
                archive = _zip_folder(source_dir, tmpdir)
                used_url = f"file:///{source_dir.as_posix()}"
            except Exception as exc:
                row["note"] = f"could not read {source_dir} ({type(exc).__name__}: {exc})"
                return row
        else:
            for url in urls:
                try:
                    archive = _download(url, tmpdir)
                    used_url = url
                    break
                except Exception as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
                    log(f"  ! {entry.get('title')}: {url} -> {last_error}")
        if archive is None:
            row["note"] = f"download failed ({last_error})"
            return row
        row["used_url"] = used_url
        if urls and used_url != urls[0]:
            # Falling back is normal, but logged.
            row["note"] = f"fell back to {used_url}"
            log(f"  ~ fell back to {used_url}")

        row["sha256"] = _sha256(archive)
        row["archive_name"] = archive.name

        extracted = tmpdir / "extracted"
        try:
            _extract(archive, extracted)
        except Exception as exc:
            row["note"] = f"extract failed ({type(exc).__name__}: {exc})"
            return row

        root = _mod_root(extracted)
        manifest = find_manifest(root)
        manifest_count = len({m.mod_id for m in all_manifests(root) if m.mod_id})
        objects = scan_smods_objects(root)

        if manifest is None:
            # Not fatal: texture packs often have no manifest.
            row["status"] = "no_manifest"
            row["note"] = "no Steamodded manifest found in download"
        else:
            row["status"] = "ok"
            row["mod_id"] = manifest.mod_id
            if manifest.version_raw:
                row["version"] = manifest.version_raw
            row["manifest"] = json.dumps(manifest.to_dict())

        row["smods_objects"] = json.dumps(objects)
        category, reason = categorize.categorise(
            entry, manifest=manifest, smods_objects=objects,
            manifest_count=max(manifest_count, 1),
        )
        row["category"] = category
        row["category_reason"] = reason

        destination = _destination(config.vault() / category, entry, log=log)
        _place(root, archive, destination)
        row["vault_path"] = str(destination)

        _write_record(destination, entry, row, manifest)

    return row


def _destination(category_dir: Path, entry: dict, log=print) -> Path:
    """Where this mod goes, guaranteed not to be another mod's slot.

    `Author@ModName` is not unique across sources, and a silent overwrite
    once replaced a current Steamodded with an old one.
    """
    base = folder_name(entry)
    destination = category_dir / base
    occupant = destination / "_bml.json"
    if occupant.is_file():
        try:
            existing_key = json.loads(occupant.read_text(encoding="utf-8")).get("key")
        except (OSError, ValueError):
            existing_key = None
        if existing_key and existing_key != entry.get("key"):
            destination = category_dir / f"{base} ({_safe(entry.get('source') or 'alt')})"
            log(f"  ! folder {base} already holds {existing_key}; "
                f"using {destination.name} instead")
    return destination


def _candidate_urls(entry: dict) -> list[str]:
    """Every URL worth trying for one mod, best first.

    Release asset URLs break when authors rename assets, and the index
    sometimes puts a commit SHA in a tag path. Both fall back to the default
    branch; the URL that worked is recorded in `_bml.json`.
    """
    urls = [entry.get("download_url") or ""]
    # The other source's URL for the same mod usually still resolves.
    for alternate in entry.get("alternates") or []:
        if alternate.get("download_url"):
            urls.append(alternate["download_url"])

    # A SHA misfiled as a tag, served from the path GitHub actually uses.
    for url in list(urls):
        match = re.match(
            r"(https://github\.com/[^/]+/[^/]+)/archive/refs/tags/([0-9a-f]{7,40})\.zip$",
            url, re.IGNORECASE,
        )
        if match:
            urls.append(f"{match.group(1)}/archive/{match.group(2)}.zip")

    slug = repo_slug(entry.get("repo") or "") or repo_slug(entry.get("download_url") or "")
    if slug:
        for branch in ("main", "master"):
            urls.append(f"https://github.com/{slug}/archive/refs/heads/{branch}.zip")

    return [u for u in dict.fromkeys(urls) if u]


# -- Steps -----------------------------------------------------------------


def _download(url: str, tmpdir: Path) -> Path:
    name = url.rstrip("/").split("/")[-1] or "download"
    if not re.search(r"\.(zip|tar\.gz|tgz|tar)$", name, re.IGNORECASE):
        name += ".zip"
    dest = tmpdir / _safe(name)
    net.download(url, dest)
    if dest.stat().st_size == 0:
        raise ValueError("empty download")
    return dest


def _zip_folder(source: Path, tmpdir: Path) -> Path:
    """Pack a folder into an archive `fetch_one` can treat as a download.

    Deterministic order, and `.git` is left out.
    """
    dest = tmpdir / f"{_safe(source.name)}-captured.zip"
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(source.rglob("*")):
            if any(part in (".git", "__pycache__") for part in path.parts):
                continue
            if path.is_dir() or path.is_symlink():
                continue
            zf.write(path, f"{source.name}/{path.relative_to(source).as_posix()}")
    if dest.stat().st_size == 0:
        raise ValueError("nothing to capture")
    return dest


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _extract(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as zf:
            for member in zf.infolist():
                target = _safe_join(destination, member.filename)
                if target is None:
                    raise ValueError(f"unsafe path in archive: {member.filename}")
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(member) as src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
        return

    if tarfile.is_tarfile(archive):
        with tarfile.open(archive) as tf:
            for member in tf.getmembers():
                if not (member.isfile() or member.isdir()):
                    continue  # skip links and devices outright
                target = _safe_join(destination, member.name)
                if target is None:
                    raise ValueError(f"unsafe path in archive: {member.name}")
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                src = tf.extractfile(member)
                if src is None:
                    continue
                with src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
        return

    raise ValueError("not a zip or tar archive (download URL may point at a web page)")


def _safe_join(root: Path, name: str) -> Path | None:
    """Resolve an archive member against `root`, or None if it escapes.

    Blocks absolute paths, drive letters and `../` traversal.
    """
    name = name.replace("\\", "/")
    if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        return None
    candidate = (root / name).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return None
    return candidate


def _mod_root(extracted: Path) -> Path:
    """Strip the `<Repo>-main/` wrapper GitHub archives add.

    Only when it is the sole top-level entry.
    """
    entries = [p for p in extracted.iterdir() if not p.name.startswith("__MACOSX")]
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return extracted


def _place(root: Path, archive: Path, destination: Path) -> None:
    """Move the staged mod into the vault. Last step, after everything passed."""
    if destination.exists():
        shutil.rmtree(destination, ignore_errors=True)
    (destination / "_archive").mkdir(parents=True, exist_ok=True)
    shutil.move(str(root), str(destination / "mod"))
    shutil.copy2(archive, destination / "_archive" / archive.name)


def _write_record(destination: Path, entry: dict, row: dict, manifest) -> None:
    """`_bml.json` -- everything known about this mod, next to the mod.

    Written to disk as well as the DB so the vault stays self-describing if
    the catalog is ever deleted or rebuilt.
    """
    record = {
        "key": entry.get("key"),
        "title": entry.get("title"),
        "author": entry.get("author"),
        "source": entry.get("source"),
        "also_in": entry.get("also_in") or [],
        "repo": entry.get("repo"),
        "download_url": entry.get("download_url"),
        "used_url": row.get("used_url") or entry.get("download_url"),
        "alternates": entry.get("alternates") or [],
        "declared_version": entry.get("declared_version"),
        "resolved_version": row.get("version"),
        "sha256": row.get("sha256"),
        "archive": row.get("archive_name"),
        "category": row.get("category"),
        "category_reason": row.get("category_reason"),
        "status": row.get("status"),
        "note": row.get("note"),
        "fetched_at": row.get("fetched_at"),
        "registry_categories": entry.get("categories") or [],
        "smods_objects": json.loads(row.get("smods_objects") or "{}"),
        "manifest": manifest.to_dict() if manifest else None,
    }
    try:
        (destination / "_bml.json").write_text(
            json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except OSError:
        pass


# -- Verification ----------------------------------------------------------


def verify(log=print) -> dict:
    """Re-check every vault folder against its recorded hash and shape."""
    root = config.vault()
    stats = {"checked": 0, "ok": 0, "missing_mod": 0, "missing_record": 0,
             "hash_mismatch": 0, "missing_archive": 0}
    if not root.is_dir():
        log(f"verify: vault {root} does not exist")
        return stats

    for record_path in sorted(root.rglob("_bml.json")):
        stats["checked"] += 1
        folder = record_path.parent
        try:
            record = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            stats["missing_record"] += 1
            log(f"verify: {folder.name}: unreadable _bml.json")
            continue

        if not (folder / "mod").is_dir():
            stats["missing_mod"] += 1
            log(f"verify: {folder.name}: mod/ directory missing")
            continue

        archive_name = record.get("archive") or ""
        archive = folder / "_archive" / archive_name if archive_name else None
        if archive is None or not archive.is_file():
            stats["missing_archive"] += 1
            log(f"verify: {folder.name}: archive missing")
            continue

        expected = record.get("sha256") or ""
        if expected and _sha256(archive) != expected:
            stats["hash_mismatch"] += 1
            log(f"verify: {folder.name}: archive hash changed")
            continue

        stats["ok"] += 1

    log(
        "verify: {checked} checked, {ok} ok, {missing_mod} missing mod dir, "
        "{missing_archive} missing archive, {hash_mismatch} hash mismatches".format(**stats)
    )
    return stats
