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
# #93: the settings path above usually does not exist, which would make
# every app start "first run pending" (no engines). tests/test_first_run.py
# clears this to exercise the screen.
os.environ.setdefault("AGENT_SKIP_FIRST_RUN", "1")
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
# Wave 15: keep the developer's real ~/.bot-inator stores out of tests:
# the action journal (#83), the privacy ledger (#82, REQUIRED: the egress
# hooks write a row on every cloud call, URL open and model download), tool
# health (#90), delivery settings + held offers (#87) and watches (#88).
_wave15_tmp = _tempfile.mkdtemp(prefix="bot-inator-w15-")
os.environ.setdefault("AGENT_ACTIONS_DB", os.path.join(_wave15_tmp, "actions.db"))
os.environ.setdefault("AGENT_EGRESS_DB", os.path.join(_wave15_tmp, "egress.db"))
os.environ.setdefault("AGENT_TOOL_HEALTH_PATH", os.path.join(_wave15_tmp, "tool_health.json"))
os.environ.setdefault(
    "AGENT_DELIVERY_SETTINGS_PATH", os.path.join(_wave15_tmp, "delivery_settings.json")
)
os.environ.setdefault("AGENT_HELD_OFFERS_PATH", os.path.join(_wave15_tmp, "held_offers.json"))
os.environ.setdefault("AGENT_WATCHES_PATH", os.path.join(_wave15_tmp, "watches.json"))
# #87: the developer's real calendar, running apps and key presses must not
# hold test events (Pause still works).
os.environ.setdefault("AGENT_DELIVERY_BUSY_CHECKS", "0")
# Wave 16: keep the developer's real ~/.bot-inator stores out of tests:
# standing rules (#78).
_wave16_tmp = _tempfile.mkdtemp(prefix="bot-inator-w16-")
os.environ.setdefault("AGENT_STANDING_RULES_PATH", os.path.join(_wave16_tmp, "standing_rules.json"))
# Wave 18: keep the developer's real ~/.bot-inator/tool_trust.json (#104)
# out of tests: the loader drops a trust entry whose hash no longer matches.
os.environ.setdefault(
    "AGENT_TOOL_TRUST_PATH",
    os.path.join(_tempfile.mkdtemp(prefix="bot-inator-w18-"), "tool_trust.json"),
)
