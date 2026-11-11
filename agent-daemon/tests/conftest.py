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
# Keep the developer's real ~/.bot-inator/token out of tests (#27): the
# daemon creates this temp token on first use and test clients send it.
os.environ.setdefault(
    "AGENT_TOKEN_PATH",
    os.path.join(_tempfile.mkdtemp(prefix="bot-inator-token-"), "token"),
)
# A developer's .env opt-in to sending personal text to the cloud (#31) must
# not change test expectations; tests that need it set it themselves.
os.environ.setdefault("AGENT_CLOUD_PERSONAL_TEXT", "0")
# Wave 13: keep the developer's real ~/.bot-inator stores out of tests:
# activity history (#75), MCP servers (#69), suggestion outcomes and the
# approval-decision log (#79).
_wave13_tmp = _tempfile.mkdtemp(prefix="bot-inator-w13-")
os.environ.setdefault("AGENT_ACTIVITY_DB", os.path.join(_wave13_tmp, "activity.db"))
os.environ.setdefault("AGENT_MCP_CONFIG_PATH", os.path.join(_wave13_tmp, "mcp_servers.json"))
os.environ.setdefault("AGENT_IDEA_OUTCOMES_PATH", os.path.join(_wave13_tmp, "idea_outcomes.json"))
os.environ.setdefault(
    "AGENT_APPROVAL_DECISIONS_PATH", os.path.join(_wave13_tmp, "approval_decisions.json")
)
