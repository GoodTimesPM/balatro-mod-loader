"""The dependency graph across everything in the vault.

The community index reduces requirements to two booleans. Here the real
version constraints come from each mod's manifest. This module reports;
`resolve` picks a working set.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime

from . import config
from .version import Version, parse_dependency, parse_version


@dataclass
class Node:
    key: str
    mod_id: str
    title: str
    version: Version | None
    version_raw: str
    category: str
    # `apply` links `vault_path/mod` into the game as `folder_name`.
    vault_path: str = ""
    folder_name: str = ""
    dependencies: list = field(default_factory=list)
    conflicts: list = field(default_factory=list)
    provides: list = field(default_factory=list)


@dataclass
class Findings:
    nodes: dict[str, Node] = field(default_factory=dict)
    # id -> version for every id something in the vault answers to, including
    # `provides` aliases. Built once here so `resolve` doesn't rebuild it.
    available: dict[str, "Version | None"] = field(default_factory=dict)
    provider_of: dict[str, str] = field(default_factory=dict)
    missing: list[tuple[Node, str]] = field(default_factory=list)
    environment: list[tuple[Node, str]] = field(default_factory=list)
    unsatisfiable: list[tuple[Node, str, str]] = field(default_factory=list)
    conflicts: list[tuple[Node, Node, str]] = field(default_factory=list)
    cycles: list[list[str]] = field(default_factory=list)
    substitutions: list[tuple[Node, str, str]] = field(default_factory=list)
    version_drift: list[tuple[Node, str, str]] = field(default_factory=list)
    unsorted: list[Node] = field(default_factory=list)
    no_manifest: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)


def build(conn) -> Findings:
    findings = Findings()

    # id -> version, for constraint checks. Includes `provides` aliases so a
    # mod that stands in for another satisfies dependencies on it.
    available = findings.available
    provider_of = findings.provider_of

    rows = list(conn.execute("SELECT * FROM vault"))
    catalog = {r["key"]: r for r in conn.execute("SELECT * FROM catalog")}

    for row in rows:
        if row["status"] == "failed":
            findings.failed.append((row["key"], row["note"] or "unknown error"))
            continue
        if row["status"] == "no_manifest":
            findings.no_manifest.append(row["key"])

        manifest = json.loads(row["manifest"] or "{}")
        mod_id = manifest.get("mod_id") or row["mod_id"] or ""
        if not mod_id:
            continue

        entry = catalog.get(row["key"])
        node = Node(
            key=row["key"],
            mod_id=mod_id,
            title=(entry["title"] if entry else mod_id),
            version=parse_version(manifest.get("version") or row["version"]),
            version_raw=manifest.get("version") or row["version"] or "",
            category=row["category"] or "",
            vault_path=row["vault_path"] or "",
            folder_name=(entry["folder_name"] if entry else "") or "",
            dependencies=[parse_dependency(d) for d in manifest.get("dependencies", [])],
            conflicts=[parse_dependency(d) for d in manifest.get("conflicts", [])],
            provides=[parse_dependency(d) for d in manifest.get("provides", [])],
        )
        node.dependencies = [d for d in node.dependencies if d]
        node.conflicts = [d for d in node.conflicts if d]
        node.provides = [d for d in node.provides if d]

        findings.nodes[_norm(mod_id)] = node
        available[_norm(mod_id)] = node.version
        if row["category"] == config.UNSORTED:
            findings.unsorted.append(node)

        # The index's claimed version versus what the mod actually ships.
        if entry and entry["declared_version"]:
            declared = entry["declared_version"]
            if node.version_raw and declared != node.version_raw:
                findings.version_drift.append((node, declared, node.version_raw))

    for node in findings.nodes.values():
        for provided in node.provides:
            for alt in provided.alternatives:
                key = _norm(alt.mod_id)
                if key not in available:
                    available[key] = node.version
                    provider_of[key] = node.mod_id

    # -- dependency satisfaction ------------------------------------------
    for node in findings.nodes.values():
        for dep in node.dependencies:
            names = [_norm(a.mod_id) for a in dep.alternatives]
            if not any(n in available for n in names):
                if all(n in _ENVIRONMENT for n in names):
                    # Lovely and the base game are never in the vault.
                    findings.environment.append((node, str(dep)))
                else:
                    findings.missing.append((node, str(dep)))
                continue
            if dep.satisfied_by(available) is None:
                have = ", ".join(
                    f"{a.mod_id}={available.get(_norm(a.mod_id)) or '?'}"
                    for a in dep.alternatives
                    if _norm(a.mod_id) in available
                )
                findings.unsatisfiable.append((node, str(dep), have or "unknown version"))
            else:
                satisfied = dep.satisfied_by(available)
                key = _norm(satisfied.mod_id)
                if key in provider_of:
                    findings.substitutions.append(
                        (node, satisfied.mod_id, provider_of[key])
                    )

    # -- conflicts ---------------------------------------------------------
    for node in findings.nodes.values():
        for conflict in node.conflicts:
            hit = conflict.satisfied_by(available)
            if hit is None:
                continue
            other = findings.nodes.get(_norm(hit.mod_id))
            if other is not None and other.key != node.key:
                findings.conflicts.append((node, other, str(conflict)))

    findings.cycles = _find_cycles(findings.nodes)
    return findings


def _find_cycles(nodes: dict[str, Node]) -> list[list[str]]:
    """Depth-first cycle detection over hard dependencies.

    Not fatal in Steamodded, which orders by `priority`, but worth reporting.
    """
    cycles: list[list[str]] = []
    seen: set[str] = set()
    stack: list[str] = []
    on_stack: set[str] = set()

    def walk(key: str) -> None:
        if key in on_stack:
            start = stack.index(key)
            cycle = stack[start:] + [key]
            if cycle not in cycles:
                cycles.append(cycle)
            return
        if key in seen:
            return
        seen.add(key)
        stack.append(key)
        on_stack.add(key)
        node = nodes.get(key)
        if node is not None:
            for dep in node.dependencies:
                # Only single-alternative deps are real edges; an alternation
                # can always be satisfied another way.
                if len(dep.alternatives) == 1:
                    target = _norm(dep.alternatives[0].mod_id)
                    if target in nodes:
                        walk(target)
        stack.pop()
        on_stack.discard(key)

    for key in sorted(nodes):
        walk(key)
    return cycles


def _norm(mod_id: str) -> str:
    import re

    return re.sub(r"[\s_-]+", "", (mod_id or "")).lower()


#: Public alias so `resolve` normalises ids the same way.
norm_id = _norm

# Things a mod can depend on that are not mods: Lovely is the Rust injector
# living in the game directory as version.dll, and `Balatro` is the base game.
ENVIRONMENT = {"lovely", "balatro"}
_ENVIRONMENT = ENVIRONMENT


# -- Reporting -------------------------------------------------------------


def write_report(findings: Findings, log=print) -> str:
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    path = config.REPORTS / f"graph_{stamp}.md"
    lines: list[str] = []
    add = lines.append

    add(f"# Balatro mod dependency report — {datetime.now():%Y-%m-%d %H:%M}")
    add("")
    add(f"- Mods with a readable manifest: **{len(findings.nodes)}**")
    add(f"- Missing dependencies: **{len(findings.missing)}**")
    add(f"- Environment requirements (Lovely / base game): "
        f"**{len(findings.environment)}**")
    add(f"- Version constraints not satisfiable: **{len(findings.unsatisfiable)}**")
    add(f"- Declared conflicts between vaulted mods: **{len(findings.conflicts)}**")
    add(f"- Dependency cycles: **{len(findings.cycles)}**")
    add(f"- Unsorted (needs a human): **{len(findings.unsorted)}**")
    add(f"- Downloaded but no manifest: **{len(findings.no_manifest)}**")
    add(f"- Failed downloads: **{len(findings.failed)}**")
    add("")

    _section(add, "Conflicts", findings.conflicts,
             lambda c: f"`{c[0].mod_id}` conflicts with `{c[1].mod_id}` — declared `{c[2]}`",
             "Two mods that must not be enabled together.")

    _section(add, "Unsatisfiable version constraints", findings.unsatisfiable,
             lambda u: f"`{u[0].mod_id}` needs `{u[1]}` — vault has {u[2]}",
             "The dependency is present but at the wrong version.")

    _section(add, "Missing dependencies", findings.missing,
             lambda m: f"`{m[0].mod_id}` needs `{m[1]}` — not in the vault",
             "Nothing in the vault provides these.")

    _section(add, "Environment requirements", findings.environment,
             lambda m: f"`{m[0].mod_id}` needs `{m[1]}`",
             "Lovely (the version.dll injector) and the base game. Not mods, "
             "never in the vault -- listed so the count is visible without "
             "drowning the real missing dependencies above.")

    _section(add, "Dependency cycles", findings.cycles,
             lambda c: " → ".join(c),
             "Declared dependencies that loop.")

    _section(add, "Provides substitutions", findings.substitutions,
             lambda s: f"`{s[0].mod_id}`'s need for `{s[1]}` is met by `{s[2]}`",
             "A mod standing in for another via `provides`.")

    _section(add, "Version drift (registry vs. shipped manifest)",
             findings.version_drift,
             lambda d: f"`{d[0].mod_id}` — registry says `{d[1]}`, manifest says `{d[2]}`",
             "The registry's version disagrees with what the download contains. "
             "The manifest is authoritative; this is why we read it.")

    _section(add, "Unsorted — needs a category decision", findings.unsorted,
             lambda n: f"`{n.title}` ({n.mod_id}) — in `{config.UNSORTED}`",
             "The categoriser declined to guess. Move these by hand, or add "
             "`\"bmlCategory\"` to a `local-index/` entry to pin them.")

    _section(add, "Downloaded but no manifest", findings.no_manifest,
             lambda k: f"`{k}`",
             "Legitimate for texture packs and single-file mods; suspicious "
             "otherwise.")

    _section(add, "Failed downloads", findings.failed,
             lambda f: f"`{f[0]}` — {f[1]}",
             "Usually a retagged release or a dead repo.")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log(f"report: wrote {path}")
    return str(path)


def _section(add, title: str, items, render, blurb: str = "") -> None:
    add(f"## {title} ({len(items)})")
    add("")
    if blurb:
        add(f"*{blurb}*")
        add("")
    if not items:
        add("None.")
        add("")
        return
    for item in items:
        add(f"- {render(item)}")
    add("")
