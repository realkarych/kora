from dataclasses import dataclass


@dataclass
class Meter:
    steps: int = 0

    def tick(self, count: int = 1) -> None:
        self.steps += count
