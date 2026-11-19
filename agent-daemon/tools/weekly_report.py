"""#128: the ``weekly_report`` tool, kept with the other built-ins.

The report itself (gathering, text, scheduler) lives in
``events/weekly_report.py``; this module only re-exports the tool, so the
import line a generated tool copies is ``from tools.weekly_report import
weekly_report`` like every other built-in.
"""
from __future__ import annotations

from events.weekly_report import weekly_report

__all__ = ["weekly_report"]
