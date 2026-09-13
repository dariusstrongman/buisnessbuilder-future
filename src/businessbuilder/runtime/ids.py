from __future__ import annotations

from collections import defaultdict
from uuid import uuid4


def random_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


class DeterministicIds:
    def __init__(self) -> None:
        self.counts: dict[str, int] = defaultdict(int)

    def __call__(self, prefix: str) -> str:
        self.counts[prefix] += 1
        return f"{prefix}_{self.counts[prefix]:06d}"
