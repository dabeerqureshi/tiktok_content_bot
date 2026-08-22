"""Base class for pipeline workers."""

from __future__ import annotations

from abc import ABC, abstractmethod


class Worker(ABC):
    name: str = "worker"

    @abstractmethod
    def run_once(self) -> None:
        """Do a bounded amount of work in a single scheduler cycle."""
        raise NotImplementedError