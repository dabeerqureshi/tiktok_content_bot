"""Domain models for the content bot.

Status enums drive the state machines that let SQLite recover from crashes.
The Pydantic models below are also used as the JSON schema for Ollama's
structured output, so the LLM is forced to return validated clip suggestions.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class VideoStatus(str, Enum):
    DISCOVERED = "DISCOVERED"
    DOWNLOADING = "DOWNLOADING"
    DOWNLOADED = "DOWNLOADED"
    TRANSCRIBING = "TRANSCRIBING"
    ANALYZING = "ANALYZING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class ClipStatus(str, Enum):
    CREATED = "CREATED"
    READY = "READY"
    SCHEDULED = "SCHEDULED"
    UPLOADING = "UPLOADING"
    PUBLISHED = "PUBLISHED"
    FAILED = "FAILED"


class PostStatus(str, Enum):
    PENDING = "PENDING"
    UPLOADING = "UPLOADING"
    PROCESSING = "PROCESSING"
    PUBLISHED = "PUBLISHED"
    FAILED = "FAILED"
    RETRY = "RETRY"


class ClipSuggestion(BaseModel):
    """A single short-form segment returned by Ollama."""

    start: float = Field(gt=0, description="Start time in seconds")
    end: float = Field(gt=0, description="End time in seconds")
    score: int = Field(ge=1, le=10, description="Quality score 1-10")
    reason: str = Field(description="Why this segment is a good clip")
    title: str = Field(description="Short hook title")
    caption: str = Field(description="TikTok caption text")
    hashtags: list[str] = Field(description="3-8 hashtags without '#'")


class ClipList(BaseModel):
    """The wrapper Ollama is asked to return for transcript analysis."""

    clips: list[ClipSuggestion]
