"""Pytest configuration for agent-daemon tests.

Pins the inference provider to ``local`` before any test module imports
``config`` or ``main``. Without this, ``config.py``'s ``load_dotenv`` call
would pick up the developer's real ``agent-daemon/.env`` (e.g. with
``AGENT_PROVIDER=groq``) and tests that patch ``MLXEngine.evaluate_event``
would silently no-op because ``_build_engines`` would have instantiated
``GroqEngine`` instead. ``setdefault`` keeps an explicit shell override
authoritative (e.g. ``AGENT_PROVIDER=groq pytest`` for an integration run
against the real Groq backend).
"""

from __future__ import annotations

import os

os.environ.setdefault("AGENT_PROVIDER", "local")
# Keep the developer's real ~/.bot-inator/settings.json out of tests.
os.environ.setdefault(
    "AGENT_SETTINGS_PATH",
    os.path.join(os.path.dirname(__file__), ".no-user-settings.json"),
)
# Keep the developer's real ~/.bot-inator/memory.db out of tests: the
# app lifespan in tests/test_integration.py opens the default Memory.
import tempfile as _tempfile

os.environ.setdefault(
    "AGENT_MEMORY_DB",
    os.path.join(_tempfile.mkdtemp(prefix="bot-inator-test-"), "memory.db"),
)
# Per-engine overrides from a developer's .env must not leak into tests.
for _role in ("MAIN", "ANALYSIS", "DRAFTING"):
    os.environ.setdefault(f"AGENT_PROVIDER_{_role}", "")
