"""Energy insights: which projects and tags drain or lift you.

A single deterministic analyzer over the frame/check-in join.  For every
energy-bearing check-in (``energy is not None``) we find the frame it fell
inside (``pairing.frame_at``); a *rated block* is a frame holding at least one
such check-in, and its energy is the mean of those check-ins.  Each project
and each tag is then scored by how far the mean energy of its rated blocks
sits from your overall mean -- a drain (below) or a lift (above).

Honesty over count (see docs/designs/energy-aware-klok.md): a project or tag
is reported only when it has at least ``MIN_BLOCKS`` rated blocks *and* an
effect of at least ``MIN_EFFECT`` energy points, so sparse data stays silent.
This is the first-slice analyzer; the per-hour, per-weekday and fade analyzers
are deferred behind the go/no-go gate.
"""

from dataclasses import dataclass
from datetime import datetime
from statistics import mean
from typing import Dict, List, Tuple

from klok import pairing
from klok.mindful import Checkin
from klok.model import Frame

MIN_BLOCKS = 5
MIN_EFFECT = 0.5
NO_PROJECT = "(no project)"


@dataclass
class Finding:
    """One drain-or-lift result, ready to print in the digest."""

    headline: str
    detail: str
    effect_size: float
    n: int


def _rated_blocks(frames: List[Frame], checkins: List[Checkin],
                  now: datetime) -> List[Tuple[Frame, float]]:
    """Join once.  Every frame carrying at least one energy-bearing check-in,
    paired with the mean energy of those check-ins.  Mood/stress-only
    check-ins (``energy is None``) and check-ins that match no frame are
    ignored, so neither creates or inflates a block.
    """
    energies: Dict[int, List[int]] = {}   # id(frame) -> raw energies
    frame_of: Dict[int, Frame] = {}       # id(frame) -> the frame itself
    for checkin in checkins:
        if checkin.energy is None:
            continue
        frame = pairing.frame_at(frames, checkin.at, now)
        if frame is None:
            continue
        energies.setdefault(id(frame), []).append(checkin.energy)
        frame_of[id(frame)] = frame
    return [(frame_of[key], float(mean(values)))
            for key, values in energies.items()]


def _findings_for(groups: Dict[str, List[float]], overall: float,
                  prefix: str = "") -> List[Finding]:
    """Turn one grouping (by project or by tag) into surviving findings."""
    findings = []
    for name, block_energies in groups.items():
        n = len(block_energies)
        if n < MIN_BLOCKS:
            continue
        effect = round(float(mean(block_energies)) - overall, 2)
        if abs(effect) < MIN_EFFECT:
            continue
        drains = effect < 0
        findings.append(Finding(
            headline="%s%s %s" % (prefix, name,
                                  "drains you" if drains else "lifts you"),
            detail="averages %.1f points %s your overall energy over %d blocks"
                   % (abs(effect), "below" if drains else "above", n),
            effect_size=effect,
            n=n,
        ))
    return findings


def drain_findings(frames: List[Frame], checkins: List[Checkin],
                   now: datetime) -> List[Finding]:
    """Projects and tags that drain or lift you, most draining first.

    Returns ``[]`` when there is nothing honest to say: no rated blocks, or
    none that clear the ``MIN_BLOCKS``/``MIN_EFFECT`` suppression rule.
    """
    rated = _rated_blocks(frames, checkins, now)
    if not rated:
        return []
    overall = float(mean(energy for _, energy in rated))

    by_project: Dict[str, List[float]] = {}
    by_tag: Dict[str, List[float]] = {}
    for frame, energy in rated:
        by_project.setdefault(frame.project or NO_PROJECT, []).append(energy)
        for tag in frame.tags:
            by_tag.setdefault(tag, []).append(energy)

    findings = _findings_for(by_project, overall)
    findings += _findings_for(by_tag, overall, prefix="+")
    findings.sort(key=lambda finding: (finding.effect_size, finding.headline))
    return findings
