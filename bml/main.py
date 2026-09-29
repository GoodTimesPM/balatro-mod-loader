"""Balatro Mod Loader CLI.

    py -m bml.main sync                     refresh the catalog (no downloads)
    py -m bml.main fetch --limit 5          download into the vault
    py -m bml.main fetch --all
    py -m bml.main import-installed         snapshot the live Mods folder
    py -m bml.main report                   write the dependency report
    py -m bml.main search cryptid
    py -m bml.main verify                   re-hash the vault

Milestone 2 adds profiles and the only commands that write to the game:

    py -m bml.main profile new live --from-installed
    py -m bml.main resolve live             what that profile actually needs
    py -m bml.main apply live --dry-run     what would change in Mods\\
    py -m bml.main apply live --adopt
    py -m bml.main status                   what is enabled right now
    py -m bml.main launch

Everything else still leaves the game alone. `apply` is the one command that
does not, and it refuses to delete anything it did not create.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import traceback
from pathlib import Path

from . import apply as apply_mod
from . import categorize, config, db, graph, links, pipeline, profiles, resolve, vault
from .log import log
from .manifest import all_manifests, find_manifest, scan_smods_objects
from .sources import local_index
from .sources.merge import normalise


def cmd_sync(args) -> int:
    return 0 if pipeline.sync(log=log, only=args.source) >= 0 else 1


def cmd_fetch(args) -> int:
    conn = db.connect()
    empty = not db.catalog_rows(conn)
    conn.close()
    if empty:
        log("fetch: catalog is empty -- run `sync` first")
        return 1
    pipeline.fetch(log=log, mod=args.mod, category=args.category,
                   limit=args.limit, force=args.force)
    return 0


def cmd_recategorise(args) -> int:
    """Re-sort the vault in place after a rule change. Downloads nothing.

    Re-derives each category from the mod already on disk, so a rule change
    or a hand-pinned category doesn't need a full re-download.
    """
    root = config.vault()
    if not root.is_dir():
        log(f"recategorise: vault {root} does not exist")
        return 1

    conn = db.connect()
    entries = {row["key"]: _entry_from_row(row) for row in db.catalog_rows(conn)}

    stats = {"checked": 0, "moved": 0, "unchanged": 0, "orphan": 0}
    for record_path in sorted(root.rglob("_bml.json")):
        stats["checked"] += 1
        folder = record_path.parent
        try:
            record = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log(f"  ! {folder.name}: unreadable _bml.json")
            stats["orphan"] += 1
            continue

        entry = entries.get(record.get("key") or "")
        if entry is None:
            # Vaulted from a catalog entry that no longer exists. Fall back to
            # what the record itself remembers rather than skipping the mod.
            entry = {
                "title": record.get("title") or folder.name,
                "description": "",
                "categories": record.get("registry_categories") or [],
                "forced_category": "",
            }
            stats["orphan"] += 1

        mod_dir = folder / "mod"
        if not mod_dir.is_dir():
            log(f"  ! {folder.name}: no mod/ directory")
            continue

        try:
            manifest = find_manifest(mod_dir)
            objects = scan_smods_objects(mod_dir)
            manifest_count = len({m.mod_id for m in all_manifests(mod_dir) if m.mod_id})
            category, reason = categorize.categorise(
                entry, manifest=manifest, smods_objects=objects,
                manifest_count=max(manifest_count, 1),
            )
        except Exception as exc:
            # One unreadable file must not leave the vault half-sorted.
            log(f"  ! {folder.name}: {type(exc).__name__}: {exc}")
            continue

        current = folder.parent.name
        if category == current:
            stats["unchanged"] += 1
            continue

        log(f"  {folder.name}: {current} -> {category}  ({reason})")
        stats["moved"] += 1
        if args.dry_run:
            continue

        destination = root / category / folder.name
        if destination.exists():
            shutil.rmtree(destination, ignore_errors=True)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(folder), str(destination))

        record["category"] = category
        record["category_reason"] = reason
        (destination / "_bml.json").write_text(
            json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        with conn:
            conn.execute(
                "UPDATE vault SET category=?, category_reason=?, vault_path=? "
                "WHERE key=?",
                (category, reason, str(destination), record.get("key") or ""),
            )

    # Remove category folders a rule change emptied.
    if not args.dry_run:
        for child in sorted(root.iterdir()):
            if child.is_dir() and not any(child.iterdir()):
                child.rmdir()

    log("recategorise: {checked} checked, {moved} moved, {unchanged} unchanged, "
        "{orphan} without a catalog entry".format(**stats)
        + (" (dry run -- nothing moved)" if args.dry_run else ""))
    conn.close()
    return 0


def cmd_import_installed(args) -> int:
    """Snapshot the live mods folder. Read-only, always."""
    mods_dir = config.game_mods()
    if not mods_dir.is_dir():
        log(f"import-installed: {mods_dir} does not exist")
        return 1

    conn = db.connect()
    # Match on normalised titles and known mod ids; installed folders have no repo URL.
    known_titles = {
        normalise(row["title"])
        for row in conn.execute("SELECT title FROM catalog")
        if row["title"]
    }
    known_ids = {
        normalise(row["mod_id"])
        for row in conn.execute("SELECT mod_id FROM vault WHERE mod_id != ''")
    }

    folders = sorted(p for p in mods_dir.iterdir() if p.is_dir())
    unknown: list[str] = []
    with conn:
        conn.execute("DELETE FROM installed")
        for folder in folders:
            manifest = find_manifest(folder)
            mod_id = manifest.mod_id if manifest else ""
            candidates = {normalise(folder.name), normalise(mod_id)}
            if manifest and manifest.name:
                candidates.add(normalise(manifest.name))
            candidates.discard("")
            in_catalog = bool(candidates & (known_titles | known_ids))
            if not in_catalog:
                unknown.append(f"{folder.name} ({mod_id or 'no manifest'})")
            conn.execute(
                """INSERT OR REPLACE INTO installed
                   (folder, mod_id, version, fmt, manifest, in_catalog, seen_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    folder.name,
                    mod_id,
                    manifest.version_raw if manifest else "",
                    manifest.fmt if manifest else "",
                    json.dumps(manifest.to_dict()) if manifest else "{}",
                    int(in_catalog),
                    db.now(),
                ),
            )

    log(f"import-installed: recorded {len(folders)} installed mods "
        f"(read-only, nothing modified)")
    if unknown:
        log(f"import-installed: {len(unknown)} not matched to any catalog entry "
            f"-- candidates for local-index/:")
        for name in unknown:
            log(f"    - {name}")
    conn.close()
    return 0


def cmd_add(args) -> int:
    """Write a local-index entry for a mod the public sources don't carry."""
    try:
        fields = local_index.from_github_url(args.url)
    except ValueError as exc:
        log(f"add: {exc}")
        return 1
    if args.name:
        fields["title"] = args.name

    try:
        folder = local_index.write_entry(
            fields["title"], fields["author"],
            repo=fields["repo"], download_url=fields["download_url"],
            version=args.version or fields["version"],
            category=args.category or "",
            overwrite=args.force,
        )
    except FileExistsError as path:
        log(f"add: {path} already exists -- edit it, or pass --force")
        return 1

    log(f"add: wrote {folder / 'meta.json'}")
    log(f"add: {fields['title']} <- {fields['download_url']}")
    log("add: now run `sync` then `fetch --mod \"%s\"`" % fields["title"])
    return 0


def cmd_capture(args) -> int:
    """Vault the hand-installed mods no public source carries.

    Mods shared on Discord or bundled in a modpack have no public download.
    Each gets a local-index entry whose `localSource` is its installed folder,
    so `sync` keeps it in the catalog and `fetch` vaults it like any other.

    Reads the game folder. Writes only to local-index/.
    """
    mods_dir = config.game_mods()
    if not mods_dir.is_dir():
        log(f"capture: {mods_dir} does not exist")
        return 1

    conn = db.connect()
    known = {normalise(row["title"]) for row in conn.execute("SELECT title FROM catalog")
             if row["title"]}
    known |= {normalise(row["mod_id"])
              for row in conn.execute("SELECT mod_id FROM vault WHERE mod_id != ''")}
    conn.close()

    written, skipped = [], []
    for folder in sorted(p for p in mods_dir.iterdir() if p.is_dir()):
        if folder.name.lower() in config.PROTECTED_MOD_FOLDERS:
            continue
        if args.folder and args.folder.lower() not in folder.name.lower():
            continue
        manifest = find_manifest(folder)
        mod_id = manifest.mod_id if manifest else ""
        names = {normalise(folder.name), normalise(mod_id)}
        if manifest and manifest.name:
            names.add(normalise(manifest.name))
        names.discard("")
        if not args.all and (names & known):
            skipped.append(folder.name)
            continue
        if manifest is None:
            log(f"  ! {folder.name}: no Steamodded manifest -- skipped "
                f"(nothing here could describe it)")
            continue

        author = (manifest.authors or ["unknown"])[0] if manifest else "unknown"
        try:
            written_to = local_index.write_entry(
                manifest.name or mod_id or folder.name,
                str(author).split(",")[0].strip() or "unknown",
                local_source=str(folder.resolve()),
                version=manifest.version_raw or "",
                category=args.category or "",
                description=f"Captured from the installed copy at {folder}. "
                            "No public download is known for this mod.",
                overwrite=args.force,
            )
        except FileExistsError as path:
            log(f"  = {folder.name}: {Path(str(path)).parent.name} already indexed")
            continue
        written.append(written_to.name)
        log(f"  + {folder.name} -> local-index/{written_to.name}  ({mod_id})")

    log(f"capture: {len(written)} entries written, {len(skipped)} already in the catalog")
    if written:
        log("capture: run `sync` then `fetch --all` to vault them")
    return 0


def cmd_report(args) -> int:
    conn = db.connect()
    findings = graph.build(conn)
    graph.write_report(findings, log=log)
    conn.close()
    return 0


def cmd_search(args) -> int:
    conn = db.connect()
    term = f"%{args.term}%"
    rows = conn.execute(
        """SELECT c.title, c.author, c.source, c.declared_version,
                  v.category, v.mod_id, v.status
           FROM catalog c LEFT JOIN vault v ON v.key = c.key
           WHERE c.title LIKE ? OR c.author LIKE ? OR v.mod_id LIKE ?
           ORDER BY c.title COLLATE NOCASE""",
        (term, term, term),
    ).fetchall()
    if not rows:
        log(f"search: nothing matches {args.term!r}")
        return 0
    for row in rows:
        state = row["category"] or ("not fetched" if row["status"] is None else row["status"])
        log(f"{row['title'][:34]:<36} {row['author'][:18]:<20} "
            f"{(row['declared_version'] or '')[:14]:<16} {row['source']:<13} {state}")
    log(f"search: {len(rows)} match(es)")
    conn.close()
    return 0


def cmd_verify(args) -> int:
    stats = vault.verify(log=log)
    bad = stats["missing_mod"] + stats["missing_archive"] + stats["hash_mismatch"]
    return 1 if bad else 0


# -- Milestone 2: profiles, resolution, and the live mods folder -----------


def cmd_profile(args) -> int:
    action = args.action

    if action == "list":
        found = profiles.all_profiles()
        if not found:
            log("profile: none yet -- `profile new live --from-installed` "
                "captures the current setup")
            return 0
        for profile in found:
            log(f"{profile.slug:<20} {len(profile.mods):>3} mods  "
                f"{profile.description or ''}")
        return 0

    if action == "new":
        if profiles.load(args.name) and not args.force:
            log(f"profile: {args.name} already exists (use --force to replace)")
            return 1
        if args.from_installed:
            conn = db.connect()
            if not conn.execute("SELECT COUNT(*) FROM installed").fetchone()[0]:
                log("profile: no snapshot of the live folder yet -- "
                    "run `import-installed` first")
                conn.close()
                return 1
            profile = profiles.from_installed(conn, args.name, args.desc or "")
            conn.close()
        else:
            profile = profiles.Profile(name=args.name, description=args.desc or "")
        if args.steamodded:
            profile.steamodded = args.steamodded
        path = profile.save()
        log(f"profile: wrote {path} with {len(profile.mods)} mods"
            + (f", steamodded={profile.steamodded}" if profile.steamodded else ""))
        return 0

    profile = profiles.load(args.name)
    if profile is None:
        log(f"profile: no profile named {args.name!r}")
        return 1

    if action == "show":
        log(f"{profile.name} — {profile.description or 'no description'}")
        log(f"  steamodded: {profile.steamodded or '(leave installed alone)'}")
        log(f"  lovely:     {profile.lovely or '(leave installed alone)'}")
        log(f"  {len(profile.mods)} mods:")
        for name in profile.mods:
            log(f"    - {name}")
        return 0

    if action == "add":
        added = [m for m in args.mods if profile.add(m)]
        profile.save()
        log(f"profile {profile.slug}: added {len(added)} "
            f"({len(args.mods) - len(added)} already present)")
        return 0

    if action == "rm":
        removed = [m for m in args.mods if profile.remove(m)]
        profile.save()
        log(f"profile {profile.slug}: removed {len(removed)} "
            f"({len(args.mods) - len(removed)} not in the profile)")
        return 0

    if action == "set":
        if args.steamodded is not None:
            profile.steamodded = args.steamodded
        if args.lovely is not None:
            profile.lovely = args.lovely
        if args.desc is not None:
            profile.description = args.desc
        profile.save()
        log(f"profile {profile.slug}: updated")
        return 0

    if action == "delete":
        profiles.delete(args.name)
        log(f"profile: deleted {profile.slug}")
        return 0

    log(f"profile: unknown action {action!r}")
    return 1


def _resolve_profile(profile):
    """Resolve a profile against the vault. Shared by `resolve` and `apply`."""
    conn = db.connect()
    findings = graph.build(conn)
    conn.close()
    wanted = list(profile.mods)
    if profile.steamodded:
        wanted.append(profile.steamodded)
    if profile.lovely:
        wanted.append(profile.lovely)
    return resolve.resolve(findings, wanted)


def _report_resolution(resolution) -> None:
    log(f"resolve: {len(resolution.include)} mods "
        f"({len(resolution.requested)} requested, "
        f"{len(resolution.added)} pulled in as dependencies)")
    for node in resolution.added:
        key = graph.norm_id(node.mod_id)
        log(f"  + {node.title} ({node.version_raw or 'no version'}) "
            f"— {resolution.reasons.get(key, '')}")
    for name in resolution.unknown:
        log(f"  ? {name!r} matches nothing in the vault "
            f"(add it to local-index/, or check the spelling)")
    for node, dep in resolution.missing:
        log(f"  MISSING     {node.mod_id} needs {dep}")
    for node, dep, have in resolution.unsatisfiable:
        log(f"  WRONG VER   {node.mod_id} needs {dep} — have {have}")
    for a, b, why in resolution.conflicts:
        log(f"  CONFLICT    {a.mod_id} vs {b.mod_id} — declared {why}")
    if resolution.environment:
        count = len(resolution.environment)
        log(f"  {count} mod{'' if count == 1 else 's'} also require Lovely or "
            f"the base game (see `status`)")
    log("resolve: OK" if resolution.ok else "resolve: NOT clean — see above")


def cmd_resolve(args) -> int:
    profile = profiles.load(args.name)
    if profile is None:
        log(f"resolve: no profile named {args.name!r}")
        return 1
    resolution = _resolve_profile(profile)
    _report_resolution(resolution)
    return 0 if resolution.ok else 1


def cmd_apply(args) -> int:
    profile = profiles.load(args.name)
    if profile is None:
        log(f"apply: no profile named {args.name!r}")
        return 1

    resolution = _resolve_profile(profile)
    _report_resolution(resolution)
    # A profile with problems can still be applied, but only on purpose.
    # A dry run is exempt, since the plan helps fix the problems.
    if not resolution.ok and not args.force and not args.dry_run:
        log("apply: refusing to apply an unclean profile (--force to override)")
        return 1

    plan = apply_mod.build_plan(resolution, profile, adopt=args.adopt)
    for action in plan.changes:
        log(f"  {action}")
    for action in plan.blocked:
        log(f"  BLOCKED {action.folder}: {action.note}")
    # Hand-installed folders can't be switched off here, so name them.
    stuck = [a for a in plan.actions if a.kind == "keep" and "hand-installed" in a.note]
    for action in stuck:
        log(f"  STILL LOADS {action.folder}: hand-installed, not managed here")
    if stuck:
        log(f"  ^ {len(stuck)} mod(s) load regardless of this profile. "
            f"Apply a profile containing them with --adopt to take them over.")
    counts = plan.counts()
    log("apply: " + ", ".join(f"{v} {k}" for k, v in sorted(counts.items()))
        + f", {len(plan.blocked)} blocked")

    if args.dry_run:
        log("apply: dry run — nothing was changed")
        return 0
    if not plan.changes and not (args.lovely and profile.lovely):
        log("apply: already in the requested state")
        return 0

    stats = apply_mod.execute(plan, log=log)
    log("apply: {linked} linked, {unlinked} unlinked, {adopted} adopted, "
        "{failed} failed".format(**stats))

    if args.lovely and profile.lovely:
        node = resolution.include.get(graph.norm_id(profile.lovely))
        dll = apply_mod.find_lovely_dll(node) if node else None
        if dll is None:
            log(f"apply: no version.dll found for lovely={profile.lovely!r}")
        else:
            apply_mod.swap_lovely(dll, log=log, dry_run=False)

    return 1 if stats["failed"] else 0


def cmd_status(args) -> int:
    """What the game would actually load right now."""
    mods_dir = config.game_mods()
    vault_root = config.vault()
    if not mods_dir.is_dir():
        log(f"status: {mods_dir} does not exist")
        return 1

    buckets: dict[str, list[str]] = {}
    for entry in sorted(mods_dir.iterdir()):
        if not entry.is_dir():
            continue
        if entry.name.lower() in config.PROTECTED_MOD_FOLDERS:
            state = "protected"
        else:
            state = links.describe(entry, vault_root)
        buckets.setdefault(state, []).append(entry.name)

    labels = {
        "ours": "enabled from the vault",
        "real": "hand-installed (not managed here, but still loaded)",
        "foreign-link": "linked somewhere outside the vault",
        "protected": "not a mod — left alone",
    }
    for state, names in sorted(buckets.items()):
        log(f"{labels.get(state, state)}: {len(names)}")
        for name in names:
            log(f"    {name}")

    lovely = apply_mod.lovely_status()
    if lovely["present"]:
        log(f"lovely: {config.LOVELY_DLL} present, {lovely['bytes']} bytes, "
            f"sha256 {lovely['sha256']}…"
            + (f", {len(lovely['backups'])} previous build(s) kept"
               if lovely["backups"] else ""))
    else:
        log(f"lovely: NOT INSTALLED — {lovely['path']} is missing, "
            f"so no mods will load at all")
    return 0


def cmd_launch(args) -> int:
    """Start Balatro through Steam so the overlay and cloud saves still work."""
    url = f"steam://rungameid/{config.STEAM_APPID}"
    log(f"launch: {url}")
    try:
        subprocess.Popen(["cmd", "/c", "start", "", url], shell=False)
    except OSError as exc:
        log(f"launch: failed ({exc})")
        return 1
    return 0


def cmd_gui(args) -> int:
    """Open the desktop window.

    Imported here so the CLI still works on a Python without tkinter.
    """
    try:
        from . import gui
    except ImportError as exc:
        log(f"gui: tkinter is not available in this Python ({exc})")
        return 1
    return gui.run()


_entry_from_row = pipeline.entry_from_row


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bml", description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    p = subparsers.add_parser("sync", help="refresh the catalog from all sources")
    p.add_argument("--source", choices=("local", "index", "thunderstore"),
                   help="limit to one source")
    p.set_defaults(func=cmd_sync)

    p = subparsers.add_parser("fetch", help="download mods into the vault")
    p.add_argument("--all", action="store_true", help="fetch everything (default)")
    p.add_argument("--mod", help="mods whose title or key contains this text")
    p.add_argument("--category", help="only mods with this registry category")
    p.add_argument("--limit", type=int, help="stop after N mods")
    p.add_argument("--force", action="store_true",
                   help="re-download mods already in the vault")
    p.set_defaults(func=cmd_fetch)

    p = subparsers.add_parser("recategorise",
                              aliases=["recategorize"],
                              help="re-sort the vault after a rule change (no downloads)")
    p.add_argument("--dry-run", action="store_true", help="show moves, change nothing")
    p.set_defaults(func=cmd_recategorise)

    p = subparsers.add_parser("import-installed",
                              help="snapshot %%APPDATA%%\\Balatro\\Mods (read-only)")
    p.set_defaults(func=cmd_import_installed)

    p = subparsers.add_parser("add", help="add a mod the indexes don't carry")
    p.add_argument("url", help="its GitHub repo, release, or archive URL")
    p.add_argument("--name", help="override the mod name taken from the URL")
    p.add_argument("--version", help="pin a tag")
    p.add_argument("--category", choices=config.CATEGORIES,
                   help="pin the vault folder, overriding the categoriser")
    p.add_argument("--force", action="store_true",
                   help="overwrite an existing local-index entry")
    p.set_defaults(func=cmd_add)

    p = subparsers.add_parser(
        "capture",
        help="index hand-installed mods that no public source carries")
    p.add_argument("--folder", help="only folders whose name contains this")
    p.add_argument("--all", action="store_true",
                   help="include mods the catalog already knows")
    p.add_argument("--category", choices=config.CATEGORIES,
                   help="pin the vault folder for everything captured")
    p.add_argument("--force", action="store_true",
                   help="overwrite existing local-index entries")
    p.set_defaults(func=cmd_capture)

    p = subparsers.add_parser("report", help="write the dependency graph report")
    p.set_defaults(func=cmd_report)

    p = subparsers.add_parser("search", help="search the catalog")
    p.add_argument("term")
    p.set_defaults(func=cmd_search)

    p = subparsers.add_parser("verify", help="re-hash and shape-check the vault")
    p.set_defaults(func=cmd_verify)

    # -- Milestone 2 -------------------------------------------------------

    p = subparsers.add_parser("profile", help="create and edit mod profiles")
    p.set_defaults(func=cmd_profile)
    actions = p.add_subparsers(dest="action", required=True)

    q = actions.add_parser("list", help="every profile")
    q = actions.add_parser("new", help="create a profile")
    q.add_argument("name")
    q.add_argument("--from-installed", action="store_true",
                   help="seed it from the last `import-installed` snapshot")
    q.add_argument("--desc", help="description")
    q.add_argument("--steamodded", help="which vaulted Steamodded to use")
    q.add_argument("--force", action="store_true", help="overwrite an existing one")

    q = actions.add_parser("show", help="what a profile contains")
    q.add_argument("name")

    q = actions.add_parser("add", help="add mods to a profile")
    q.add_argument("name")
    q.add_argument("mods", nargs="+")

    q = actions.add_parser("rm", help="remove mods from a profile")
    q.add_argument("name")
    q.add_argument("mods", nargs="+")

    q = actions.add_parser("set", help="change a profile's framework slots")
    q.add_argument("name")
    q.add_argument("--steamodded", help="vaulted Steamodded to use ('' to unset)")
    q.add_argument("--lovely", help="vaulted Lovely to use ('' to unset)")
    q.add_argument("--desc")

    q = actions.add_parser("delete", help="delete a profile")
    q.add_argument("name")

    p = subparsers.add_parser(
        "resolve", help="expand a profile to its full dependency set")
    p.add_argument("name")
    p.set_defaults(func=cmd_resolve)

    p = subparsers.add_parser(
        "apply", help="make %%APPDATA%%\\Balatro\\Mods match a profile")
    p.add_argument("name")
    p.add_argument("--dry-run", action="store_true",
                   help="show the plan, change nothing")
    p.add_argument("--adopt", action="store_true",
                   help="move hand-installed folders aside instead of skipping them")
    p.add_argument("--lovely", action="store_true",
                   help="also swap the game's version.dll to the profile's Lovely")
    p.add_argument("--force", action="store_true",
                   help="apply even though resolution found problems")
    p.set_defaults(func=cmd_apply)

    p = subparsers.add_parser("status", help="what the game would load right now")
    p.set_defaults(func=cmd_status)

    p = subparsers.add_parser("launch", help="start Balatro through Steam")
    p.set_defaults(func=cmd_launch)

    p = subparsers.add_parser("gui", help="open the desktop window")
    p.set_defaults(func=cmd_gui)

    return parser


def main(argv=None) -> int:
    config.load_env()
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        log("interrupted")
        return 130
    except Exception:
        log("UNCAUGHT:\n" + traceback.format_exc())
        return 1


if __name__ == "__main__":
    sys.exit(main())
