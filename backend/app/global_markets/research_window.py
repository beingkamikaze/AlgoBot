"""The line between research and proof, fixed on 7 Nov 2026 before any gold strategy ran. Of the 1,510 trading days from
4 Jan 2021 to 6 Nov 2026, the first 70 % (1,057) are for choosing settings. From HELD_BACK_FROM on, days are only for the one
Lab test (plan phase X7). It is a fixed date on purpose: a line worked out from the data would move as history downloads, and
days already seen could end up counted as unseen. Never move it after looking at results."""

from __future__ import annotations

import datetime as dt

RESEARCH_START = dt.date(2021, 1, 4)
HELD_BACK_FROM = dt.date(2025, 2, 6)
