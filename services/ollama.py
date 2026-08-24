"""Ollama client for transcript analysis + clip metadata.

Uses Olive's structured-output support: a Pydantic JSON schema is passed in
the ``format`` field, then the response is validated again with a
:class:`pydantic.TypeAdapter` before it is trusted.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from pydantic import TypeAdapter, ValidationError

from config import load_settings
from database.models import ClipList

log = logging.getLogger(__name__)


def _client():
    import ollama  # noqa: PLC0415

    # Bounded timeout so a hung Ollama server can't freeze the scheduler
    # thread forever (large models can take minutes; 15 min is generous).
    return ollama.Client(host=load_settings().ollama_host, timeout=900.0)


def available() -> bool:
    try:
        _client().list()
        return True
    except Exception:  # pragma: no cover - depends on local Ollama
        return False


def analyze_transcript(
    transcript: str,
    video_title: str,
    video_duration: float,
) -> list[dict[str, Any]]:
    """Ask Ollama for the strongest segments and return validated clip dicts.

    Invalid/unbounded JSON is retried with a corrective follow-up message so
    one malformed LLM response does not waste a whole downloaded video.
    """
    settings = load_settings()
    min_s, max_s = settings.clip_duration_bounds
    base_prompt = (
        "You analyze YouTube video transcripts to find the best short-form "
        "TikTok clips.\n"
        "Return ONLY valid JSON matching this shape: "
        "{\"clips\": [{\"start\": float, \"end\": float, \"score\": int 1-10, "
        "\"reason\": str, \"title\": str, \"caption\": str, "
        "\"hashtags\": [str, ...]}]}\n"
        f"Each clip must be between {min_s:.0f}s and {max_s:.0f}s long.\n"
        f"Video title: {video_title}\n"
        f"Video duration: {video_duration:.1f}s\n"
        f"Transcript:\n{transcript[-12000:]}"
    )

    messages = [{"role": "user", "content": base_prompt}]
    adapter = TypeAdapter(ClipList)

    for attempt in range(1, settings.ollama_max_retries + 2):
        raw = _client().chat(
            model=settings.ollama_model,
            messages=messages,
            format=ClipList.model_json_schema(),
            options={"temperature": 0.2},
        )
        content = raw.message.content if not isinstance(raw, dict) else raw["message"]["content"]
        try:
            data = _extract_json(content)
            parsed = adapter.validate_python(data)
            return [c.model_dump() for c in parsed.clips]
        except (json.JSONDecodeError, ValidationError, KeyError) as exc:
            log.warning("Ollama attempt %d returned invalid output: %s", attempt, exc)
            if attempt > settings.ollama_max_retries:
                return []
            messages.append({"role": "assistant", "content": content[:2000]})
            messages.append({
                "role": "user",
                "content": (
                    "That was not valid for the required schema "
                    f"({exc}). Respond again with ONLY the JSON object."
                ),
            })
    return []


def _extract_json(content: str) -> Any:
    content = content.strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content)
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        start, end = content.find("{"), content.rfind("}")
        if start != -1 and end != -1:
            return json.loads(content[start : end + 1])
        raise