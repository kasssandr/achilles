"""ARCHILLES Execution Plan — *how* a recipe runs on this machine.

``plan(capabilities, recipe, mode)`` is a **pure function**: given detected
hardware (``HardwareProfile``), the hardware-independent ``IndexRecipe`` and a
mode, it derives the throughput/quality knobs that may vary per machine —
batch size, embedding device, local vs. external embedding, and reranking
on/off + reranker device. It performs no I/O and touches no real GPU, so the
whole multi-tier decision logic is unit-testable with synthetic specs (§9).

Modes (the single user-facing variable, Hardware-Tiers-V2 §7):

- ``auto`` (default): derive the sensible path from the detected class —
  capable hardware (apple-mps/gpu-mid/gpu-large) → ``full-local``; weak
  hardware (cpu-only/gpu-small) → ``light`` (flat, local, free; auto never
  silently requires external embedding or cost).
- ``light``: flat + local (degraded-compatible, zero-cost).
- ``full-local``: hierarchical (per recipe) + local embedding.
- ``full-external``: hierarchical (per recipe) + external embedding
  (prepare locally, embed elsewhere — the §8 sweet spot).

The plan's ``mode`` field holds the *resolved* mode (``auto`` is collapsed to
``light``/``full-local``). Wiring the mode from config.json/CLI and consuming
the plan is a later stage (§10.3/§10.4).

See docs/internal/CONCEPT_2026-06-23_HARDWARE_TIERS_V2.md (§3, §4, §6, §9).
"""
import logging
from dataclasses import dataclass
from typing import Literal

from src.archilles.hardware import HardwareClass, HardwareProfile, classify_hardware
from src.archilles.recipe import IndexRecipe

logger = logging.getLogger(__name__)

Mode = Literal["auto", "light", "full-local", "full-external"]

VALID_MODES: frozenset[str] = frozenset(
    {"auto", "light", "full-local", "full-external"}
)

# Classes that can run the full (hierarchical, local) path comfortably (§4).
_CAPABLE_CLASSES: frozenset[str] = frozenset({"apple-mps", "gpu-mid", "gpu-large"})

# Batch size per class. cpu-only/gpu-small stay small; apple-mps moderate;
# gpu-mid/gpu-large match the old balanced/maximal staffel.
_BATCH_BY_CLASS: dict[HardwareClass, int] = {
    "cpu-only": 8,
    "gpu-small": 8,
    "apple-mps": 16,
    "gpu-mid": 32,
    "gpu-large": 64,
}

# Local embedding device per class.
_DEVICE_BY_CLASS: dict[HardwareClass, str] = {
    "cpu-only": "cpu",
    "gpu-small": "cuda",
    "apple-mps": "mps",
    "gpu-mid": "cuda",
    "gpu-large": "cuda",
}

# Reranking runs at search time. On weak hardware the reranker shares VRAM with
# the embedder (the observed "meta tensor" conflict), so it runs on CPU there;
# from gpu-mid up it runs on the GPU (§6).
_RERANK_GPU_CLASSES: frozenset[str] = frozenset({"gpu-mid", "gpu-large"})


@dataclass(frozen=True)
class ExecutionPlan:
    """How an IndexRecipe is executed on a concrete machine.

    Identity/chunk-schema decisions live in the recipe; this captures only the
    machine-/mode-dependent knobs. DB-neutral fields (reranking) and
    quality-neutral fields (batch/device/local-vs-external) never change the
    vectors, so databases stay cross-machine compatible.
    """

    hardware_class: HardwareClass
    mode: Mode  # resolved mode (auto collapsed to light/full-local)
    embedding_device: str  # "cuda" | "mps" | "cpu" (local embedding device)
    batch_size: int
    embed_local: bool  # False → embed externally (prepare local, embed elsewhere)
    hierarchical: bool
    rerank_enabled: bool
    rerank_device: str  # "cuda" | "cpu"


def _resolve_mode(requested: str, hw_class: HardwareClass) -> Mode:
    if requested not in VALID_MODES:
        raise ValueError(
            f"Unknown mode: {requested!r}. Valid modes: {sorted(VALID_MODES)}"
        )
    if requested != "auto":
        return requested  # type: ignore[return-value]
    return "full-local" if hw_class in _CAPABLE_CLASSES else "light"


def plan(
    capabilities: HardwareProfile,
    recipe: IndexRecipe,
    mode: str = "auto",
) -> ExecutionPlan:
    """Derive the ExecutionPlan for ``recipe`` on ``capabilities`` under ``mode``.

    Raises:
        ValueError: if ``mode`` is not one of VALID_MODES.
    """
    hw_class = classify_hardware(capabilities)
    resolved = _resolve_mode(mode, hw_class)

    # light forces the flat (degraded-compatible) schema; the full modes honour
    # the recipe's declared schema.
    hierarchical = False if resolved == "light" else recipe.hierarchical
    embed_local = resolved != "full-external"

    return ExecutionPlan(
        hardware_class=hw_class,
        mode=resolved,
        embedding_device=_DEVICE_BY_CLASS[hw_class],
        batch_size=_BATCH_BY_CLASS[hw_class],
        embed_local=embed_local,
        hierarchical=hierarchical,
        rerank_enabled=True,
        rerank_device="cuda" if hw_class in _RERANK_GPU_CLASSES else "cpu",
    )


def warn_if_light_plan_hides_hierarchy(execution_plan: "ExecutionPlan", store) -> bool:
    """Operational hint (finding 1.1): warn when the resolved plan is ``light``
    but the existing index already holds hierarchical (parent) chunks.

    That combination means the index was embedded hierarchically (full-local or
    externally) elsewhere, yet on this machine ``auto`` resolved to ``light`` —
    so new titles will be indexed FLAT and unmarked, silently diverging in
    quality with no ``pending_external`` upgrade queue. Setting
    ``"mode": "full-external"`` restores the marked-and-upgradable path.

    Logged, not printed (review 1.3): this fires on *every* scan in this
    library, including scans driven by the MCP server, where stdout carries
    JSON-RPC. A library-layer function has no business writing to stdout — the
    CLI sees this through logging like every other warning.

    Returns True if it warned. Never raises — a store hiccup just skips the hint.
    """
    if execution_plan is None or execution_plan.mode != "light":
        return False
    try:
        if not store.has_parent_chunks():
            return False
    except Exception:
        return False
    logger.warning(
        "This index already contains hierarchical (parent) chunks, but the "
        "resolved mode is 'light'. New titles will be indexed FLAT and "
        "unmarked — searchable, but silently lower quality, with no "
        "pending_external upgrade queue. If your corpus was embedded "
        'externally, set "mode": "full-external" in this library\'s '
        ".archilles/config.json."
    )
    return True
