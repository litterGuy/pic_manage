"""Duplicate resolution.

The rule the user asked for is "keep the file with the earliest time".  The
surrounding tiebreakers exist only to make that choice *deterministic*: running
the scan twice must quarantine the same file both times.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .timeextract import REAL_SOURCES


@dataclass
class DedupeItem:
    """The subset of a probed file that the comparison needs."""

    file_id: int
    source_path: str
    size: int
    sha256: str | None
    phash: str | None
    taken_at: datetime | None
    time_source: str
    media_type: str
    #: ``(width, height)`` when known.  Two images of different dimensions are
    #: never near-duplicates, which keeps perceptual clustering from pairing
    #: unrelated photos that merely share a similar tone histogram.
    dims: tuple[int, int] | None = None
    #: True for a row already sitting in the library.  Such an item always
    #: wins, which is what makes re-scanning an overlapping source tree a
    #: no-op instead of quarantining the library's own copy.
    in_library: bool = False


def winner_key(item: DedupeItem):
    """Sort key: smaller wins.

    1. A file already organized into the library beats a new candidate.
    2. A real embedded time beats a fallback or missing one.
    3. Then the earliest ``taken_at`` -- the user's rule.
    4. Then the larger file (a full-size original over a thumbnail).
    5. Then the shorter path, then lexicographic.
    6. Finally ``file_id``, which can only tie when two rows genuinely share a
       source path -- impossible given the unique index, but including it makes
       the key a strict total order so ``min()`` never depends on input order.
    """
    real_time = 0 if item.time_source in REAL_SOURCES else 1
    taken = item.taken_at or datetime.max
    return (
        0 if item.in_library else 1,
        real_time, taken,
        -item.size, len(item.source_path), item.source_path,
        item.file_id,
    )


def pick_winner(items: list[DedupeItem]) -> DedupeItem | None:
    if not items:
        return None
    return min(items, key=winner_key)


def group_exact(items: list[DedupeItem]) -> dict[str, list[DedupeItem]]:
    """Group by content hash; files without a hash form singleton groups."""
    groups: dict[str, list[DedupeItem]] = {}
    for item in items:
        key = item.sha256 or f"__nohash__{item.file_id}"
        groups.setdefault(key, []).append(item)
    return groups


def losers_of(groups: dict[str, list[DedupeItem]]) -> list[tuple[DedupeItem, DedupeItem, str]]:
    """``(loser, winner, dup_kind)`` for every non-winner in every group."""
    out: list[tuple[DedupeItem, DedupeItem, str]] = []
    for key, members in groups.items():
        if len(members) < 2:
            continue
        winner = pick_winner(members)
        if winner is None:
            continue
        kind = "phash" if key.startswith("__phash__") else "exact"
        for member in members:
            if member is not winner:
                out.append((member, winner, kind))
    return out


def resolve(
    items: list[DedupeItem],
    *,
    phash_threshold: int = 6,
    use_phash: bool = False,
) -> tuple[list[DedupeItem], list[tuple[DedupeItem, DedupeItem, str]]]:
    """Split ``items`` into keepers and ``(loser, winner, kind)`` triples.

    Exact duplicates are resolved first.  When ``use_phash`` is set, the exact
    winners are then clustered by perceptual hash so a re-encoded copy of a
    photo is quarantined too.
    """
    groups = group_exact(items)
    keepers: list[DedupeItem] = []
    losers: list[tuple[DedupeItem, DedupeItem, str]] = []

    for key, members in groups.items():
        if len(members) < 2:
            keepers.extend(members)
            continue
        winner = pick_winner(members)
        keepers.append(winner)
        for member in members:
            if member is not winner:
                losers.append((member, winner, "exact"))

    if use_phash:
        from .hashing import phash_clusters

        by_id = {k.file_id: k for k in keepers}
        already_lost = {loser.file_id for loser, _, _ in losers}
        extra: list[tuple[DedupeItem, DedupeItem, str]] = []

        # Only compare images that share pixel dimensions.  A near-duplicate is
        # a re-encode or a crop of the same frame, so its dimensions match;
        # unrelated photos that merely share a tone histogram do not.  Bucketing
        # by dimensions first is what keeps this affordable: the pairwise scan is
        # quadratic within a bucket, and a library is mostly one-offs of many
        # different sizes rather than one enormous same-size pile.
        by_dims: dict[tuple[int, int], dict[int, str]] = {}
        for k in keepers:
            if k.phash and k.media_type == "image" and k.dims:
                by_dims.setdefault(k.dims, {})[k.file_id] = k.phash

        for hashed in by_dims.values():
            for cluster in phash_clusters(hashed, phash_threshold):
                members = [by_id[i] for i in cluster if i not in already_lost]
                if len(members) < 2:
                    continue
                winner = pick_winner(members)
                for member in members:
                    if member is not winner:
                        already_lost.add(member.file_id)
                        extra.append((member, winner, "phash"))

        keepers = [k for k in keepers if k.file_id not in already_lost]
        losers.extend(extra)

    return keepers, losers


__all__ = ["DedupeItem", "winner_key", "pick_winner", "group_exact", "resolve"]
