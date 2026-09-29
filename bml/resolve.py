"""Choosing a working set of mods from the graph.

Given some mods, what else is needed, and does the set hold together?
Problems only count within the chosen set. Dependencies are pulled in
transitively. Nothing is downloaded or written; `apply` acts on the result.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import graph
from .graph import Findings, Node
from .version import Version


@dataclass
class Resolution:
    """A proposed set of mods, plus everything wrong with it."""

    requested: list[str] = field(default_factory=list)
    include: dict[str, Node] = field(default_factory=dict)
    #: norm id -> why it is in the set ("requested", or "needed by X").
    reasons: dict[str, str] = field(default_factory=dict)

    missing: list[tuple[Node, str]] = field(default_factory=list)
    unsatisfiable: list[tuple[Node, str, str]] = field(default_factory=list)
    conflicts: list[tuple[Node, Node, str]] = field(default_factory=list)
    environment: list[tuple[Node, str]] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Whether this set can be applied without a known-broken outcome.

        Environment requirements like Lovely are not counted here; `status`
        reports Lovely separately.
        """
        return not (self.missing or self.unsatisfiable or self.conflicts
                    or self.unknown)

    @property
    def added(self) -> list[Node]:
        """Mods pulled in as dependencies rather than asked for by name."""
        wanted = set(self.requested)
        return [n for k, n in sorted(self.include.items()) if k not in wanted]


def resolve(findings: Findings, wanted: list[str]) -> Resolution:
    """Expand `wanted` (mod ids, titles or vault keys) into a full set."""
    out = Resolution()

    frontier: list[str] = []
    for name in wanted:
        node = find(findings, name)
        if node is None:
            out.unknown.append(name)
            continue
        key = graph.norm_id(node.mod_id)
        out.requested.append(key)
        if key not in out.include:
            out.include[key] = node
            out.reasons[key] = "requested"
            frontier.append(key)

    # Breadth-first with a queue, so a bad manifest can't blow the stack.
    seen: set[str] = set()
    while frontier:
        key = frontier.pop(0)
        if key in seen:
            continue
        seen.add(key)
        node = out.include[key]

        for dep in node.dependencies:
            names = [graph.norm_id(a.mod_id) for a in dep.alternatives]
            if all(n in graph.ENVIRONMENT for n in names):
                out.environment.append((node, str(dep)))
                continue

            # Already in the set and good enough.
            if dep.satisfied_by(_versions(out.include)) is not None:
                continue

            hit = dep.satisfied_by(findings.available)
            if hit is not None:
                provider = _provider(findings, graph.norm_id(hit.mod_id))
                if provider is not None:
                    pkey = graph.norm_id(provider.mod_id)
                    if pkey not in out.include:
                        out.include[pkey] = provider
                        out.reasons[pkey] = f"needed by {node.mod_id}"
                        frontier.append(pkey)
                    continue

            # Wrong version and missing are reported separately.
            present = [n for n in names if n in findings.available]
            if present and _is_steamodded_1_0_0_wording(dep, findings.available):
                continue
            if present:
                have = ", ".join(
                    f"{n}={findings.available.get(n) or '?'}" for n in present
                )
                out.unsatisfiable.append((node, str(dep), have))
            else:
                out.missing.append((node, str(dep)))

    _check_conflicts(out)
    return out


def _is_steamodded_1_0_0_wording(dep, available: dict[str, Version | None]) -> bool:
    """`Steamodded (>=1.0.0)` against a `1.0.0~BETA-*` build: not a real problem.

    Every Steamodded build is a 1.0.0 pre-release, which sorts below 1.0.0,
    yet mods mean "the 1.0.0 line". So for Steamodded only, a `>=` against a
    bare version accepts a pre-release of that exact version.
    """
    for alt in dep.alternatives:
        key = graph.norm_id(alt.mod_id)
        if key != "steamodded":
            continue
        have = available.get(key)
        if have is None or have.pre is None:
            continue
        if all(
            c.op == ">=" and c.version is not None and c.version.pre is None
            and c.version.parts == have.parts
            for c in alt.constraints
        ) and alt.constraints:
            return True
    return False


def _check_conflicts(out: Resolution) -> None:
    """Conflicts within the chosen set only.

    A conflict with a mod that isn't being enabled is not a problem.
    """
    versions = _versions(out.include)
    for key, node in sorted(out.include.items()):
        for conflict in node.conflicts:
            hit = conflict.satisfied_by(versions)
            if hit is None:
                continue
            other = out.include.get(graph.norm_id(hit.mod_id))
            if other is None or other.key == node.key:
                continue
            # Both mods usually declare the same conflict, so report it once.
            pair = {node.key, other.key}
            if any(pair == {a.key, b.key} for a, b, _ in out.conflicts):
                continue
            out.conflicts.append((node, other, str(conflict)))


def _versions(include: dict[str, Node]) -> dict[str, Version | None]:
    """The `available` map restricted to a chosen set, `provides` included."""
    versions: dict[str, Version | None] = {}
    for key, node in include.items():
        versions[key] = node.version
    for node in include.values():
        for provided in node.provides:
            for alt in provided.alternatives:
                versions.setdefault(graph.norm_id(alt.mod_id), node.version)
    return versions


def _provider(findings: Findings, key: str) -> Node | None:
    """The node answering to `key`, whether by its own id or via `provides`."""
    node = findings.nodes.get(key)
    if node is not None:
        return node
    owner = findings.provider_of.get(key)
    if owner:
        return findings.nodes.get(graph.norm_id(owner))
    return None


def find(findings: Findings, name: str) -> Node | None:
    """Look a mod up the way a person would type it.

    Exact id, then key, then title, then a unique substring of the title.
    Ambiguity returns nothing rather than guessing.
    """
    if not name:
        return None
    key = graph.norm_id(name)
    if key in findings.nodes:
        return findings.nodes[key]

    lowered = name.strip().lower()
    for node in findings.nodes.values():
        if node.key.lower() == lowered:
            return node
    for node in findings.nodes.values():
        if graph.norm_id(node.title) == key:
            return node

    hits = [
        node for node in findings.nodes.values()
        if lowered in node.title.lower() or lowered in node.key.lower()
    ]
    return hits[0] if len(hits) == 1 else None
