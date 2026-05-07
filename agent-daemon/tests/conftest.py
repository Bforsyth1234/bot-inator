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
