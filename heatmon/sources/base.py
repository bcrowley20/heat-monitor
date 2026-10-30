from typing import Protocol


class Source(Protocol):
    name: str
    interval_s: int

    async def poll(self) -> int | None:
        """Fetch new data and store it. Returns the number of rows stored, or None when
        the source is turned off."""
        ...
