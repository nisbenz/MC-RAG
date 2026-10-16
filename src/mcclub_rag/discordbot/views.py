"""The thumbs-up / thumbs-down buttons on an answer (Req 5).

The conversation id rides inside the `custom_id`, so nothing has to be remembered to record a rating
and one bot process holds buttons for every channel at once. A press replaces the view with the
pressed button alone, disabled, so a rating cannot be changed or counted twice (Req 5.2).

Known limitation: discord.py re-dispatches a component press only to a view registered with
`add_view`, and that registration is keyed by the exact `custom_id`. Ours carries the conversation
id, so the set of ids is unbounded and cannot be pre-registered; buttons sent before a bot restart
therefore stop working after it. Such a press gets Discord's "interaction failed" and records
nothing.
"""

from dataclasses import dataclass
from typing import cast

import discord
import structlog

from mcclub_rag.discordbot.client import FeedbackRating, RagClient
from mcclub_rag.discordbot.notices import (
    FEEDBACK_FAILED,
    FEEDBACK_RECORDED,
    NoticeLanguage,
    notice,
)

log = structlog.get_logger(__name__)

FEEDBACK_PREFIX = "mcclub:fb"
UP_LABEL = "\U0001f44d"
DOWN_LABEL = "\U0001f44e"
_RATINGS: tuple[FeedbackRating, ...] = ("up", "down")


@dataclass(frozen=True)
class RatingPress:
    conversation_id: str
    rating: FeedbackRating


def custom_id(conversation_id: str, rating: FeedbackRating) -> str:
    """Stays well inside Discord's 100-character `custom_id` limit (a conversation id is 38)."""
    return f"{FEEDBACK_PREFIX}:{rating}:{conversation_id}"


def parse_custom_id(value: str) -> RatingPress | None:
    """`None` for anything that is not one of ours, so a malformed id never records a rating."""
    prefix, _, remainder = value.partition(":")
    name, _, rest = remainder.partition(":")
    if f"{prefix}:{name}" != FEEDBACK_PREFIX:
        return None
    rating, _, conversation_id = rest.partition(":")
    if rating not in _RATINGS or not conversation_id:
        return None
    return RatingPress(conversation_id=conversation_id, rating=cast(FeedbackRating, rating))


async def submit_rating(client: RagClient, press: RatingPress) -> bool:
    """One feedback call; False when it could not be stored (Req 5.3)."""
    return await client.feedback(press.conversation_id, press.rating)


class FeedbackView(discord.ui.View):
    """Two buttons bound to one answer's conversation id."""

    def __init__(
        self,
        conversation_id: str,
        *,
        client: RagClient | None = None,
        language: NoticeLanguage = "en",
    ) -> None:
        super().__init__(timeout=None)
        self.conversation_id = conversation_id
        self._client = client
        self._language = language
        for rating, label, style in (
            ("up", UP_LABEL, discord.ButtonStyle.success),
            ("down", DOWN_LABEL, discord.ButtonStyle.secondary),
        ):
            self.add_item(self._button(cast(FeedbackRating, rating), label, style))

    def _button(
        self,
        rating: FeedbackRating,
        label: str,
        style: discord.ButtonStyle,
    ) -> discord.ui.Button:
        button = discord.ui.Button(
            label=label, style=style, custom_id=custom_id(self.conversation_id, rating)
        )

        async def callback(interaction: discord.Interaction) -> None:
            await self.record(interaction, button)

        button.callback = callback
        return button

    async def record(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        """Record the press, then leave only the pressed button, disabled (Req 5.2)."""
        press = parse_custom_id(button.custom_id or "")
        if press is None:  # unreachable for a button we built: refuse rather than guess
            log.warning("bot_feedback_bad_custom_id")
            return
        ok = True
        if self._client is not None:
            ok = await submit_rating(self._client, press)
        if not ok:  # Req 5.3: logged, and never laid on the answer already delivered
            log.warning("bot_feedback_failed", rating=press.rating)
        button.disabled = True
        for item in list(self.children):
            if item is not button:
                self.remove_item(item)
        text = notice(FEEDBACK_RECORDED if ok else FEEDBACK_FAILED, self._language)
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.edit_message(view=self)
            await interaction.followup.send(text, ephemeral=True)
