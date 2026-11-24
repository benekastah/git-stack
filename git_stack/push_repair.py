"""Build lease-guarded recovery commands from confirmed push results."""

import shlex
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from git_client import PushDestination, PushStatus

_UPDATE_STATUSES = (PushStatus.FAST_FORWARD, PushStatus.FORCED, PushStatus.CREATED, PushStatus.DELETED)


@dataclass(frozen=True)
class PushRepair:
    url: str
    # Each entry is (full ref name, old object ID, new object ID). Empty
    # object IDs represent absent refs before a creation or after a deletion.
    refs: tuple[tuple[str, str, str], ...]

    @property
    def args(self) -> tuple[str, ...]:
        return (
            "-c",
            "push.pushOption=",
            "push",
            "--atomic",
            "--no-follow-tags",
            "--recurse-submodules=no",
            "--no-set-upstream",
            *(f"--force-with-lease={ref}:{new}" for ref, _, new in self.refs),
            "--",
            self.url,
            # A plain OID can select a local ref named after that OID.
            *(f"{old}^{{commit}}:{ref}" if old else f":{ref}" for ref, old, _ in self.refs),
        )

    def command(self, program: str) -> str:
        return shlex.join((program, *self.args))


def redacted_push_url(url: str) -> str | None:
    """Return a possible report label when Git strips user information."""
    scheme, sep, location = url.partition("://")
    if sep:
        authority, slash, path = location.partition("/")
        _, at, host = authority.partition("@")
        return f"{scheme}{sep}{host}{slash}{path}" if at else None
    prefix, at, suffix = url.partition("@")
    if at and ":" in suffix and "/" not in prefix.partition(":")[0] and not Path(url).is_absolute():
        return suffix
    return None


def repairable_url(url: str) -> bool:
    """Only direct, printable destinations that can be included in instructions."""
    if not url.isprintable() or any(c in url for c in "?#"):
        return False
    if Path(url).is_absolute():
        return True
    if "://" not in url:
        # A slashless SSH shorthand can select a legacy named remote.
        # A remote helper delimiter is also unsuitable for direct rollback.
        host, sep, path = url.partition(":")
        return bool(sep and host and "/" in path and not path.startswith(":") and "/" not in host)
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    return parsed.scheme in ("https", "http", "ssh", "git", "file") and parsed.username is None


def destination_status(destination: PushDestination, planned: dict[str, tuple[str, str]]) -> str:
    """Describe confirmed status rows and identify refs without a reliable result."""
    targets = Counter(ref.target for ref in destination.refs)
    groups: dict[str, list[str]] = {"updated": [], "rejected": [], "up to date": []}
    uncertain = set(planned)
    for ref in destination.refs:
        tips = planned.get(ref.target)
        if targets[ref.target] != 1 or tips is None or tips[1] != ref.source:
            continue
        if ref.status in _UPDATE_STATUSES:
            status = "updated"
        elif ref.status is PushStatus.REJECTED:
            status = "rejected"
        elif ref.status is PushStatus.UP_TO_DATE:
            status = "up to date"
        else:
            continue
        groups[status].append(ref.target)
        uncertain.discard(ref.target)
    details = [f"{status} {', '.join(sorted(refs))}" for status, refs in groups.items() if refs]
    uncertain.update(targets.keys() - planned.keys())
    if uncertain:
        details.append(f"outcome uncertain for {', '.join(sorted(uncertain))}")
    return ". ".join(details) or "outcome uncertain"


def rollback_for_destination(destination: PushDestination, planned: dict[str, tuple[str, str]]) -> PushRepair | None:
    """Build a command only when the entire destination has known outcomes."""
    if Counter(ref.target for ref in destination.refs) != Counter(planned.keys()):
        return None
    refs: list[tuple[str, str, str]] = []
    for ref in destination.refs:
        old, source = planned[ref.target]
        if source != ref.source:
            return None
        if ref.status is PushStatus.UP_TO_DATE:
            continue
        if ref.status in (PushStatus.FAST_FORWARD, PushStatus.FORCED) and ref.old_oid == old and ref.new_oid:
            refs.append((ref.target, old, ref.new_oid))
        elif ref.status is PushStatus.DELETED and old:
            refs.append((ref.target, old, ""))
        elif ref.status is PushStatus.CREATED and not old and ref.new_oid:
            refs.append((ref.target, "", ref.new_oid))
        else:
            return None
    return PushRepair(destination.url, tuple(refs)) if refs else None
