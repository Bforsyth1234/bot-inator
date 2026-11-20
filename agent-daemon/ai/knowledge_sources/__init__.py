"""Registry of the knowledge-index sources added after #134 (#137–#141).

Each module here is a self-contained, read-only reader with the same
interface: ``NAME``, ``TITLE``, ``DESCRIPTION``, ``TAINT_LABEL``,
``DEFAULT_OPTIONS``, ``validate_options(options)``,
``listing(options, **overrides) -> Listing`` and (all but calendar)
``choices(options, **overrides)``. ``ai/knowledge_index.py`` registers them
(``SOURCES``, ``SOURCE_LABELS``, per-source options, readers), in this order.

Import order: the readers import ``Listing`` / ``SourceItem`` from
``ai.knowledge_index``, and ``ai.knowledge_index`` loads this registry at
the end of its own module. ``MODULE_NAMES`` is therefore defined before
anything is imported, and ``ai.knowledge_index`` is imported first, so
either module can be imported first without a circular-import failure.
"""
from __future__ import annotations

import importlib
from types import ModuleType
from typing import Optional

# File names, in Settings / SOURCES order (#137, #138, #139, #140, #141).
MODULE_NAMES: tuple[str, ...] = ("imessage", "calendar", "notes", "browser", "ai_exports")

from ai import knowledge_index as _ki  # noqa: E402,F401  - first (see module doc)

MODULES: tuple[ModuleType, ...] = tuple(
    importlib.import_module(f"{__name__}.{name}") for name in MODULE_NAMES)
BY_NAME: dict[str, ModuleType] = {m.NAME: m for m in MODULES}
NAMES: tuple[str, ...] = tuple(m.NAME for m in MODULES)


def get(name: str) -> Optional[ModuleType]:
    """The reader module whose ``NAME`` is ``name``, or None."""
    return BY_NAME.get(name)


def has_choices(name: str) -> bool:
    module = BY_NAME.get(name)
    return module is not None and callable(getattr(module, "choices", None))


__all__ = ["BY_NAME", "MODULES", "MODULE_NAMES", "NAMES", "get", "has_choices"]
