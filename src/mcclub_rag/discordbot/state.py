"""In-memory, disposable state: conversation ids, per-user cooldown, per-guild slots (Req 4, 6.1).

Nothing here is persisted. Losing all of it costs at most one lost follow-up per channel, because an
unknown ``conversation_id`` is a valid first turn for the API, not an error.
"""

from collections.abc import Callable
from time import monotonic


class ConversationDirectory:
    """Remembers the ``conversation_id`` the API returned, per Discord location (Req 4.1).

    The API mints a fresh id for any id it does not recognise (``conversation.py::create``), so the
    bot cannot choose one: it sends nothing on the first turn and echoes back whatever came back.
    """

    def __init__(self) -> None:
        self._ids: dict[tuple[int | None, int], str] = {}

    @staticmethod
    def key(guild_id: int | None, channel_id: int) -> str:
        """The namespaced cache key (Req 4.3): two guilds cannot collide on one entry."""
        return f"mc-{guild_id if guild_id is not None else 'dm'}-{channel_id}"

    def get(self, guild_id: int | None, channel_id: int) -> str | None:
        return self._ids.get((guild_id, channel_id))

    def remember(self, guild_id: int | None, channel_id: int, conversation_id: str) -> None:
        self._ids[(guild_id, channel_id)] = conversation_id

    def forget(self, guild_id: int | None, channel_id: int) -> None:
        """Drop the entry so the next ask omits ``conversation_id`` (Req 4.4)."""
        self._ids.pop((guild_id, channel_id), None)


class UserCooldown:
    """A per-user window, checked before the API call so one member cannot spend the day's quota."""

    def __init__(self, seconds: float, *, clock: Callable[[], float] = monotonic) -> None:
        self.seconds = seconds
        self._clock = clock
        self._last: dict[int, float] = {}

    def remaining(self, user_id: int) -> float:
        """Seconds left in the window; 0.0 when the user may ask. Records nothing."""
        last = self._last.get(user_id)
        if last is None:
            return 0.0
        return max(0.0, self.seconds - (self._clock() - last))

    def acquire(self, user_id: int) -> float:
        """Seconds left when refused, or 0.0 when allowed and recorded."""
        remaining = self.remaining(user_id)
        if remaining > 0:
            return remaining
        self._last[user_id] = self._clock()
        return 0.0


class GuildSlots:
    """A per-guild concurrency cap; refuses rather than queues, so no request waits on another."""

    def __init__(self, max_concurrency: int) -> None:
        self.max_concurrency = max_concurrency
        self._in_flight: dict[int | None, int] = {}

    def in_flight(self, guild_id: int | None) -> int:
        return self._in_flight.get(guild_id, 0)

    def acquire(self, guild_id: int | None) -> bool:
        if self.in_flight(guild_id) >= self.max_concurrency:
            return False
        self._in_flight[guild_id] = self.in_flight(guild_id) + 1
        return True

    def release(self, guild_id: int | None) -> None:
        current = self.in_flight(guild_id)
        if current <= 1:
            self._in_flight.pop(guild_id, None)
        else:
            self._in_flight[guild_id] = current - 1
