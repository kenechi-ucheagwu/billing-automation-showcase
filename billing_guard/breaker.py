"""Stop a run that keeps failing.

A few failures in a row usually mean the service is down or the tool's idea
of the system is wrong. Either way, carrying on only spreads the damage, so
the run stops and a person looks. The breaker never resets itself.
"""

from dataclasses import dataclass


@dataclass
class Breaker:
    consecutive_limit: int = 3
    total_limit: int = 5
    streak: int = 0
    total: int = 0

    def __post_init__(self) -> None:
        if self.consecutive_limit < 1 or self.total_limit < 1:
            raise ValueError("failure limits must be positive")

    def record(self, success: bool) -> str | None:
        """Count one attempt. Returns the reason to stop, or None to carry on."""
        if success:
            self.streak = 0
            return None
        self.streak += 1
        self.total += 1
        if self.streak >= self.consecutive_limit:
            return "consecutive failure limit"
        if self.total >= self.total_limit:
            return "total failure limit"
        return None
