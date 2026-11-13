"""Canonical step identity for the evaluator.

The runtime action-prefetch engine can leave "holes" in the committed
screenshot file indices: a case dir may hold only ``step001, step002,
step004, step006, step007`` because indices 3/5 were reserved by *cancelled*
prefetch candidates (debris under ``_prefetch_staging/step00N_<uuid>/``).
Meanwhile ``actions_flat[].global_step`` in ``_trajectory_for_evaluate.json``
is a **contiguous ordinal** over the committed actions (1..5 above), while the
screenshot file names keep the **raw runtime capture index**.

So ordinal != file index whenever prefetch candidates were cancelled between
two executed actions. Consumers that mix the two produce evidence / report
references a human can't map back to a real screenshot (e.g. ``step 3`` vs the
actual file ``step004_vla_done.jpg``) and phantom steps (``step 0``,
``global_steps=[6]`` when ordinals only run 1..5).

This module is the single source of truth for resolving one into the other and
for labelling steps in a human-auditable way::

    step#3 (file step004)     # ordinal 3, its after-screenshot is step004

``global_step`` keeps its ordinal semantics (``tools/dtr_reconstruct.py`` zips
ordinals 1:1 against executed steps), so nothing here renumbers it.  ``file_step``
is always *derived* from the action's stored screenshot paths — never added to
``actions_flat``, never written back to the runtime layer.

Pure stdlib (os/re); no evaluator-internal imports so every tool module can use
it without import cycles, and offline tests can exercise it with no LLM.
"""
from __future__ import annotations

import os
import re

_STEP_RE = re.compile(r"step(\d+)")


def _to_int(value) -> int | None:
    """Best-effort int of an LLM-supplied handle ('6', 6, 'final' -> None)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_file_step(path) -> int | None:
    """Physical runtime index embedded in an artifact path, or None.

    Checks the file's basename first (``step001_vla_input.jpg``,
    ``step001_vla_done_ui.xml``), then the preceding path segment so a staged-dir
    prefix also matches (``_prefetch_staging/step003_<uuid>/prefetch_cancelled.json``
    -> 3). Does NOT match ``turn006_orch.jpg`` or an orchestrator path whose parent
    is the plain case dir (no literal ``step`` in either segment).
    """
    if not path:
        return None
    parts = str(path).replace("\\", "/").rstrip("/").split("/")
    segments = [parts[-1]]
    if len(parts) >= 2:
        segments.append(parts[-2])
    for seg in segments:
        m = _STEP_RE.search(seg)
        if m:
            return int(m.group(1))
    return None


def ordinal_set(flat) -> set[int]:
    """Valid ``global_step`` ordinals (ints > 0) over the committed actions."""
    out: set[int] = set()
    for a in flat or []:
        gs = a.get("global_step")
        if isinstance(gs, int) and gs > 0:
            out.add(gs)
    return out


def file_step_index(flat) -> dict[int, int]:
    """Physical file index -> ordinal of its committed owner.

    Maps every screenshot index referenced by a committed action
    (``screenshot_before`` / ``screenshot_after``) to that action's ``global_step``.
    The map is NOT injective (e.g. r04: ordinal 3 and 4 both own ``step004``);
    when several ordinals share one file index the **last** committed owner wins
    (that is the action which carries the real after-screenshot / overlay).
    """
    idx: dict[int, int] = {}
    for a in flat or []:
        gs = a.get("global_step")
        if not (isinstance(gs, int) and gs > 0):
            continue
        for path in (a.get("screenshot_before"), a.get("screenshot_after")):
            fs = parse_file_step(path)
            if fs is not None:
                idx[fs] = gs  # later committed action wins on collisions
    return idx


def resolve_ordinal(flat, *, gs=None, path=None) -> int | None:
    """Canonicalize any model-supplied handle to a committed ordinal, or None.

    ``global_step`` contract = ordinal, so a numeric ``gs`` that IS a valid
    ordinal is returned as-is. But the model can also leak a *physical* file
    index into ``global_step`` (e.g. after ``list_all_screenshots``), or pass an
    ``image_path``; both are resolved to the ordinal that owns that on-disk file.

    Resolution priority:
      1. numeric ``gs`` that is a known ordinal -> itself;
      2. numeric ``gs`` that is NOT a known ordinal but is a known physical
         file index (or ``path``'s file index) -> owning ordinal;
      3. ``path``'s file index is a known physical index -> owning ordinal;
      4. otherwise None.

    r02 example (ordinals 1..5, committed files 1,2,4,6,7):
      ``resolve_ordinal(flat, gs=6)  -> 4``   (file step006 owned by ordinal 4)
      ``resolve_ordinal(flat, gs=5)  -> 5``   (ordinal 5)
      ``resolve_ordinal(flat, gs=99) -> None``
      ``resolve_ordinal(flat, path='...step004_vla_done.jpg') -> 3``

    The final ``f in idx`` branch only fires when no numeric ``gs`` was usable,
    and only maps files that a committed action really owns — a stray on-disk
    file (cancelled-prefetch debris) never enriches from a coincidental ordinal.
    """
    ords = ordinal_set(flat)
    idx = file_step_index(flat)
    g = _to_int(gs)
    f = parse_file_step(path)

    if g is not None:
        if g in ords:
            return g          # global_step contract = ordinal
        if g in idx:
            return idx[g]     # physical index leaked into global_step -> owner
    if f is not None and f in idx:
        return idx[f]         # on-disk file index -> its committed owner
    return None


def describe_step(gs=None, file_step=None) -> str:
    """Canonical human-auditable label.

    ``step#3`` when ordinal == file index (the common, gap-free case);
    ``step#3 (file step004)`` when they differ; ``file step004`` when only the
    physical file is known (cancelled-prefetch debris, uncommitted); ``step?``
    when neither.
    """
    g = _to_int(gs)
    f = _to_int(file_step)
    if g is not None and f is not None:
        if g == f:
            return f"step#{g}"
        return f"step#{g} (file step{f:03d})"
    if g is not None:
        return f"step#{g}"
    if f is not None:
        return f"file step{f:03d}"
    return "step?"


def describe_action(action) -> str:
    """Canonical label for one committed action record."""
    if not isinstance(action, dict):
        return "step?"
    fs = parse_file_step(action.get("screenshot_after") or action.get("screenshot_before"))
    return describe_step(action.get("global_step"), fs)
