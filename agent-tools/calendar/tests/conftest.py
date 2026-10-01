"""Suite-wide test configuration.

`obs.STRICT = True` for the whole suite (plan UC02a, spec rev 3.2 section 10): a
non-OK outcome logged without a diagnostic raises, so a missed path fails a test
instead of quietly logging `unclassified`.
"""

from __future__ import annotations

from calendar_tools import obs

obs.STRICT = True
