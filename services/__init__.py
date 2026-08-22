"""Service package.

Heavy third-party imports (yt-dlp, faster-whisper, ollama) are done lazily
inside each module so that the application can still boot for basic health
checks even if a dependency is not yet installed.
"""

from __future__ import annotations