"""The domain pack the models are given, and its version.

A literal for now. `apps.domains` will own the versioned pack (PRD §5
principle 4 keeps supplements-specific concepts out of the core schema), and
when it does this module is the single place that changes. The gate, the
extractor and signal detection all read it from here, so the three cannot
disagree about what the domain is.
"""
from __future__ import annotations

#: Recorded on every signal and output built from it (PRD §8). Bump it whenever
#: the text below changes, so a signal stays explainable against the pack it
#: was actually detected with.
DOMAIN_PACK_VERSION = "supplements@2026-09-24"


def domain_pack() -> str:
    return (
        "Dietary supplements and nutraceuticals: ingredients, formulations, "
        "dosing protocols, mechanisms of action, clinical evidence, safety and "
        "interactions, category and brand movements, and the consumer questions "
        "around them. Adjacent: sports nutrition, healthy ageing, gut health, "
        "cognition, sleep, micronutrients."
    )
