"""Steamodded version numbers and dependency constraints.

This is deliberately NOT semver. Steamodded's format is

    (major).(minor).(patch)(rev)

where `rev` is a free-form trailer, and a `~` in it marks a pre-release. Real
versions in the wild look like:

    1.6.4                 CardSleeves
    0.5.4                 Cryptid
    2.1.1~dev             Talisman
    1.0.0~ALPHA-1206c     Steamodded

A `~` trailer sorts before the plain release, so `1.0.0~ALPHA-1206c < 1.0.0`.
Pre-releases compare digit-aware, so `999c < 1206c`.

Dependency strings arrive in two syntaxes depending on which manifest format
the mod shipped, and both are handled by parse_dependency():

    JSON     "Steamodded (>=1.0.0~ALPHA-1206c)"
    legacy   "Talisman>=2.0.9"

Operators, per the Steamodded spec: >= <= (inclusive), >> << (strict),
== (exact, `*` wildcards allowed), and `|` between whole alternatives.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Longest first so `>=` is never mistaken for the start of `>>`, and so a
# two-character operator always wins over a one-character one.
_OPERATORS = (">=", "<=", ">>", "<<", "==", ">", "<", "=")

_VERSION_RE = re.compile(
    r"^\s*v?"
    r"(\d+)"  # major
    r"(?:\.(\d+))?"  # minor
    r"(?:\.(\d+))?"  # patch
    r"(.*)$"  # rev / pre-release trailer
)

# Split a trailer into digit and non-digit runs so "ALPHA-1206c" compares
# numerically on the 1206 rather than lexically on the "1".
_NATURAL_RE = re.compile(r"(\d+)")

# Thunderstore's `owner-package-version` triplet, matched strictly so a
# hyphenated id like `Some-Mod-2.0` isn't mistaken for one.
_TS_TRIPLET_RE = re.compile(
    r"^(?P<owner>[A-Za-z0-9_]+)-(?P<package>[A-Za-z0-9_]+)-(?P<version>\d+\.\d+\.\d+)$"
)


@dataclass(frozen=True, order=False)
class Version:
    """A parsed Steamodded version.

    `parts` is (major, minor, patch); missing components read as 0 so that
    "1" and "1.0.0" are the same version. `pre` is the `~...` pre-release
    trailer without its tilde, or None for a normal release. `build` is any
    non-tilde trailer (rare, but `1.0.0b` exists).
    """

    raw: str
    parts: tuple[int, int, int]
    pre: str | None = None
    build: str = ""

    def __str__(self) -> str:
        return self.raw

    # -- ordering ----------------------------------------------------------
    #
    # Written out by hand rather than via @dataclass(order=True) because the
    # pre-release rule inverts the natural field ordering.

    def _key(self) -> tuple:
        # A release outranks its own pre-releases, so releases get 1 and
        # pre-releases get 0 in the second slot.
        if self.pre is None:
            return (self.parts, 1, ())
        return (self.parts, 0, _natural_key(self.pre))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Version):
            return NotImplemented
        return self._key() == other._key()

    def __hash__(self) -> int:
        return hash(self._key())

    def __lt__(self, other: "Version") -> bool:
        return self._key() < other._key()

    def __le__(self, other: "Version") -> bool:
        return self._key() <= other._key()

    def __gt__(self, other: "Version") -> bool:
        return self._key() > other._key()

    def __ge__(self, other: "Version") -> bool:
        return self._key() >= other._key()


def _natural_key(text: str) -> tuple:
    """Digit-aware key: "ALPHA-1206c" -> ("alpha-", 1206, "c")."""
    out: list = []
    for chunk in _NATURAL_RE.split(text.lower()):
        if not chunk:
            continue
        # Tag each element so int and str never compare against each other.
        out.append((1, int(chunk), "") if chunk.isdigit() else (0, 0, chunk))
    return tuple(out)


def parse_version(text: str | None) -> Version | None:
    """Parse a version string. Returns None if it isn't a version at all.

    A git SHA in the `version` field comes back None (unknown).
    """
    if not text:
        return None
    raw = str(text).strip()
    if not raw:
        return None
    match = _VERSION_RE.match(raw)
    if not match:
        return None

    major, minor, patch, trailer = match.groups()
    # A bare git SHA like "0cf7086" matches the regex as major=0 with trailer
    # "cf7086". Reject anything whose leading number is immediately followed
    # by hex-looking junk and that has no dotted component at all.
    if minor is None and patch is None and trailer and not trailer.startswith("~"):
        if re.fullmatch(r"[0-9a-f]{5,}", raw, re.IGNORECASE):
            return None

    parts = (int(major), int(minor or 0), int(patch or 0))
    trailer = (trailer or "").strip()
    pre: str | None = None
    build = ""
    if "~" in trailer:
        before, _, after = trailer.partition("~")
        build = before
        pre = after
    else:
        build = trailer
    return Version(raw=raw, parts=parts, pre=pre, build=build)


# -- Constraints -----------------------------------------------------------


@dataclass(frozen=True)
class Constraint:
    """One operator + version pair, e.g. `>=2.0.9`.

    `wildcard` holds the pattern for `==1.0.*` style constraints, where the
    right-hand side isn't a single version but a family of them.
    """

    op: str
    version: Version | None
    wildcard: tuple[str, ...] | None = None
    raw: str = ""

    def __str__(self) -> str:
        return self.raw or f"{self.op}{self.version}"

    def matches(self, candidate: Version | None) -> bool:
        """Does `candidate` satisfy this constraint?

        An unknown (None) candidate satisfies nothing except an absent
        constraint -- callers distinguish "unsatisfied" from "unknown" via
        `candidate is None` so the report can say which it was.
        """
        if candidate is None:
            return False
        if self.wildcard is not None:
            return _wildcard_matches(self.wildcard, candidate)
        if self.version is None:
            return True
        if self.op in (">=",):
            return candidate >= self.version
        if self.op in ("<=",):
            return candidate <= self.version
        if self.op in (">>", ">"):
            return candidate > self.version
        if self.op in ("<<", "<"):
            return candidate < self.version
        if self.op in ("==", "="):
            return candidate == self.version
        return False


def _wildcard_matches(pattern: tuple[str, ...], candidate: Version) -> bool:
    """`==1.0.*` matches 1.0.7 but not 1.1.0."""
    for index, want in enumerate(pattern):
        if want == "*":
            # A `*` swallows this component and everything after it.
            return True
        if index >= 3:
            break
        try:
            if candidate.parts[index] != int(want):
                return False
        except ValueError:
            return False
    return True


def parse_constraint(text: str) -> Constraint | None:
    """Parse `>=2.0.9`, `==1.0.*`, or a bare `2.0.9` (treated as `>=`)."""
    text = (text or "").strip()
    if not text:
        return None
    op = ""
    for candidate_op in _OPERATORS:
        if text.startswith(candidate_op):
            op = candidate_op
            text = text[len(candidate_op) :].strip()
            break
    if not op:
        # Steamodded writes a bare version to mean "at least this", which is
        # also the safer reading: it never rejects a newer dependency.
        op = ">="
    raw = f"{op}{text}"

    if "*" in text:
        # `1.*~` means "any 1.x, pre-releases included"; drop the `~`.
        return Constraint(op="==", version=None,
                          wildcard=tuple(text.rstrip("~").split(".")), raw=raw)
    return Constraint(op=op, version=parse_version(text), raw=raw)


# -- Dependency strings ----------------------------------------------------


@dataclass
class Alternative:
    """One acceptable mod within a dependency, with its constraints."""

    mod_id: str
    constraints: list[Constraint] = field(default_factory=list)

    def __str__(self) -> str:
        if not self.constraints:
            return self.mod_id
        return f"{self.mod_id} ({', '.join(str(c) for c in self.constraints)})"


@dataclass
class Dependency:
    """A whole dependency entry: one or more `|`-separated alternatives."""

    raw: str
    alternatives: list[Alternative] = field(default_factory=list)

    @property
    def is_alternation(self) -> bool:
        return len(self.alternatives) > 1

    def __str__(self) -> str:
        return " | ".join(str(a) for a in self.alternatives)

    def satisfied_by(self, available: dict[str, Version | None]) -> Alternative | None:
        """Return the first alternative satisfied by `available`, else None.

        `available` maps mod id -> version (None when the version is present
        but unparseable). A mod that is installed with an unknown version
        satisfies a bare dependency but not a version-constrained one.
        """
        for alt in self.alternatives:
            key = _norm_id(alt.mod_id)
            if key not in available:
                continue
            have = available[key]
            if all(c.matches(have) for c in alt.constraints):
                return alt
        return None


def _norm_id(mod_id: str) -> str:
    """Mod ids are compared case-insensitively and ignoring spaces/dashes.

    Authors are inconsistent about this -- the same mod appears as
    "Steamodded", "steamodded", and "SMODS" across manifests.
    """
    return re.sub(r"[\s_-]+", "", (mod_id or "")).lower()


def parse_dependency(text: str) -> Dependency | None:
    """Parse one dependency string in either manifest syntax.

    Accepts all of:
        "Steamodded (>=1.0.0~ALPHA-1206c)"   JSON form
        "Talisman>=2.0.9"                    legacy form
        "Talisman (>=2.0.9, <<3.0.0)"        multiple constraints
        "ModA | ModB (>=1.0)"                alternatives
        "Cryptid"                            no constraint at all
    """
    raw = (text or "").strip()
    if not raw:
        return None

    # A Thunderstore `owner-package-version` string in a Steamodded manifest,
    # read as a floor like `manifest.thunderstore_dependency` does.
    triplet = _TS_TRIPLET_RE.match(raw)
    if triplet:
        return Dependency(
            raw=raw,
            alternatives=[Alternative(
                mod_id=triplet.group("package"),
                constraints=[parse_constraint(f">={triplet.group('version')}")],
            )],
        )

    alternatives: list[Alternative] = []
    for chunk in raw.split("|"):
        alt = _parse_alternative(chunk)
        if alt is not None:
            alternatives.append(alt)
    if not alternatives:
        return None
    return Dependency(raw=raw, alternatives=alternatives)


def _parse_alternative(text: str) -> Alternative | None:
    # Some manifests leave a trailing comma inside the string.
    text = text.strip().strip(",;").strip()
    if not text:
        return None

    constraint_text = ""
    if "(" in text and text.rstrip().endswith(")"):
        # JSON form: name, then everything inside the trailing parentheses.
        name, _, rest = text.partition("(")
        constraint_text = rest.rstrip().rstrip(")")
        text = name.strip()
    else:
        # Legacy form: the name runs up to the first operator character.
        cut = _first_operator_index(text)
        if cut is not None:
            constraint_text = text[cut:]
            text = text[:cut].strip()

    # Strip stray brackets off the id too.
    mod_id = text.strip().strip("()[]").strip()
    if not mod_id:
        return None

    constraints: list[Constraint] = []
    for piece in re.split(r"[,;]", constraint_text):
        # And off the constraint.
        parsed = parse_constraint(piece.strip().strip("()[]").strip())
        if parsed is not None:
            constraints.append(parsed)
    return Alternative(mod_id=mod_id, constraints=constraints)


def _first_operator_index(text: str) -> int | None:
    for index, char in enumerate(text):
        if char in "<>=":
            return index
    return None


def parse_dependencies(items) -> list[Dependency]:
    """Parse a manifest's whole dependencies/conflicts/provides list."""
    out: list[Dependency] = []
    if not items:
        return out
    if isinstance(items, str) or isinstance(items, dict):
        items = [items]
    for item in items:
        if isinstance(item, dict):
            item = _dependency_from_object(item)
        parsed = parse_dependency(str(item))
        if parsed is not None:
            out.append(parsed)
    return out


def _dependency_from_object(item: dict) -> str:
    """Render the object form of a dependency as the string form.

    Not in the Steamodded spec, but some manifests use
    `{"id": "Steamodded", "min_version": "1.0.0~BETA-0224a"}`.
    """
    mod_id = str(item.get("id") or item.get("name") or item.get("mod_id") or "").strip()
    if not mod_id:
        return ""
    bounds = [
        (">=", item.get("min_version") or item.get("minimum_version")),
        ("<=", item.get("max_version") or item.get("maximum_version")),
        ("==", item.get("version") if not item.get("min_version") else None),
    ]
    constraints = [f"{op}{value}" for op, value in bounds if value]
    return f"{mod_id} ({', '.join(constraints)})" if constraints else mod_id
