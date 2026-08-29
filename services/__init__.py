"""Service package.

Services are imported lazily where they need heavy third-party libraries, so the
application can still boot with only the lightweight runtime dependencies in
``requirements.txt``.
"""

from __future__ import annotations
