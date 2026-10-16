"""The only module that knows Discord exists: commands, defer/edit, role check (Req 1, 3, 7).

Default intents only, and no message listener, so the bot needs no privileged intent and no Discord
verification (Req 1.2). Every handler is thin: the decisions live in `commands.py` and the rendering
in `render.py`, which is what lets the whole member-visible behaviour be tested without a gateway.
"""

from dataclasses import dataclass
from time import perf_counter

import discord
import structlog

from mcclub_rag.discordbot.client import RagClient
from mcclub_rag.discordbot.commands import (
    AskOutcome,
    AskRequest,
    precheck,
    run_ask,
    run_reset,
    run_status,
)
from mcclub_rag.discordbot.notices import (
    ANSWER_UNDELIVERED,
    NoticeLanguage,
    notice,
    notice_language,
)
from mcclub_rag.discordbot.render import EmbedSpec, RenderedReply
from mcclub_rag.discordbot.settings import BotSettings
from mcclub_rag.discordbot.state import ConversationDirectory, GuildSlots, UserCooldown
from mcclub_rag.discordbot.views import FeedbackView

log = structlog.get_logger(__name__)

ASK_COMMAND = "ask"
RESET_COMMAND = "reset"
STATUS_COMMAND = "status"


@dataclass
class BotRuntime:
    """What every handler needs; built once in `build_client`."""

    settings: BotSettings
    client: RagClient
    cooldown: UserCooldown
    slots: GuildSlots
    conversations: ConversationDirectory


def is_member(interaction: discord.Interaction, settings: BotSettings) -> bool:
    """Members access only in the configured guild (design decision 3; Req 3.1, 3.2).

    Duck-typed on `roles` rather than `isinstance(Member)`: a DM user has no roles at all, and a
    user in another guild must not inherit the club role.
    """
    if settings.guild_id is None or settings.member_role_id is None:
        return False
    if getattr(interaction, "guild_id", None) != settings.guild_id:
        return False
    roles = getattr(interaction.user, "roles", ())
    return any(getattr(role, "id", None) == settings.member_role_id for role in roles)


def to_embed(spec: EmbedSpec | None) -> discord.Embed | None:
    if spec is None:
        return None
    embed = discord.Embed(description=spec.description or None, colour=spec.colour)
    for field in spec.fields:
        embed.add_field(name=field.name, value=field.value, inline=False)
    if spec.footer:
        embed.set_footer(text=spec.footer)
    return embed


async def deliver(
    interaction: discord.Interaction,
    reply: RenderedReply,
    *,
    client: RagClient,
    language: NoticeLanguage,
) -> None:
    """Part 0 edits the deferred response, the rest follow up; the buttons ride the last part."""
    view = (
        FeedbackView(reply.feedback, client=client, language=language) if reply.feedback else None
    )
    last = len(reply.parts) - 1
    for index, part in enumerate(reply.parts):
        kwargs: dict[str, object] = {
            "content": part.content,
            "embed": to_embed(part.embed),
            # The public attribution line names the asker without pinging them (Req 2.5).
            "allowed_mentions": discord.AllowedMentions.none(),
        }
        if index == last and view is not None:
            kwargs["view"] = view
        if index == 0:
            await interaction.edit_original_response(**kwargs)
        else:
            await interaction.followup.send(ephemeral=part.ephemeral, **kwargs)


async def undelivered(
    interaction: discord.Interaction,
    reply: RenderedReply,
    *,
    language: NoticeLanguage,
) -> None:
    """The interaction expired before the answer arrived (design decision 2; Req 7.3).

    A private answer is never published: the member gets a short notice instead, and the answer is
    dropped. A public answer was headed for the channel anyway, so it is posted.
    """
    private = reply.parts[0].ephemeral
    log.warning("bot_reply_undelivered", private=private)
    try:
        if private:
            await interaction.channel.send(notice(ANSWER_UNDELIVERED, language))
        else:
            await interaction.channel.send(
                content=reply.parts[0].content,
                embed=to_embed(reply.parts[0].embed),
                allowed_mentions=discord.AllowedMentions.none(),
            )
    except discord.HTTPException:
        log.warning("bot_undelivered_notice_failed")


async def handle_ask(
    interaction: discord.Interaction,
    request: AskRequest,
    *,
    client: RagClient,
    cooldown: UserCooldown,
    slots: GuildSlots,
    conversations: ConversationDirectory,
) -> None:
    language = notice_language(request.question)
    started_at = perf_counter()
    refusal = precheck(request, cooldown=cooldown, slots=slots)
    if refusal is not None:
        log.info(
            "bot_ask_rejected",
            user_id=request.user_id,
            guild_id=request.guild_id,
            channel_id=request.channel_id,
        )
        await interaction.response.send_message(refusal.parts[0].content, ephemeral=True)
        return
    try:
        try:
            # Mandatory, not an optimisation: the chat route's p95 is above Discord's 3 s window.
            await interaction.response.defer(ephemeral=not request.public)
        except discord.NotFound:
            log.warning("bot_interaction_expired", channel_id=request.channel_id)
            return
        outcome = await run_ask(request, client=client, conversations=conversations)
        _log_outcome(request, outcome, started_at)
        try:
            await deliver(interaction, outcome.reply, client=client, language=language)
        except discord.NotFound:
            await undelivered(interaction, outcome.reply, language=language)
    except discord.HTTPException as error:
        log.warning(
            "bot_discord_error",
            status=getattr(error, "status", None),
            channel_id=request.channel_id,
        )
    finally:
        slots.release(request.guild_id)


async def handle_reset(
    interaction: discord.Interaction, *, conversations: ConversationDirectory
) -> None:
    reply = run_reset(
        getattr(interaction, "guild_id", None),
        channel_id(interaction),
        conversations,
    )
    await interaction.response.send_message(reply.parts[0].content, ephemeral=True)


async def handle_status(
    interaction: discord.Interaction,
    *,
    client: RagClient,
    settings: BotSettings,
) -> None:
    reply = await run_status(client=client, settings=settings)
    await interaction.response.send_message(reply.parts[0].content, ephemeral=True)


def channel_id(interaction: discord.Interaction) -> int:
    return getattr(interaction, "channel_id", None) or 0


def _log_outcome(request: AskRequest, outcome: AskOutcome, started_at: float) -> None:
    """One event per interaction, and never the question text, the answer or the token (Req 7.5)."""
    fields: dict[str, object] = {
        "user_id": request.user_id,
        "guild_id": request.guild_id,
        "channel_id": request.channel_id,
        "public": request.public,
        "latency_ms": int((perf_counter() - started_at) * 1000),
    }
    if outcome.failure is not None:
        log.warning(
            "bot_ask_failed",
            code=outcome.failure.code,
            status=outcome.failure.status,
            **fields,
        )
    else:
        log.info(
            "bot_ask_done",
            grounded=outcome.grounded,
            llm_calls=outcome.llm_calls,
            **fields,
        )


async def sync_commands(
    client: discord.Client,
    tree: discord.app_commands.CommandTree,
    settings: BotSettings,
) -> None:
    """Guild commands when a guild is configured, global otherwise (Req 1.1). Synced once.

    `copy_global_to` is not optional: `@tree.command` registers into the tree's *global* mapping,
    while discord.py's `sync(guild=...)` sends only the commands registered for that guild. Without
    the copy the PUT carries an empty list, Discord answers 200, and the guild sees no commands at
    all (observed on 2026-10-16; EDGE_CASES.md).
    """
    if getattr(client, "_commands_synced", False):
        return
    guild = discord.Object(id=settings.guild_id) if settings.guild_id else None
    if guild is not None:
        tree.copy_global_to(guild=guild)
    await tree.sync(guild=guild)
    client._commands_synced = True  # noqa: SLF001 - a one-shot guard for on_ready reconnects


def build_client(settings: BotSettings) -> discord.Client:
    client = discord.Client(intents=discord.Intents.default())
    tree = discord.app_commands.CommandTree(client)
    runtime = BotRuntime(
        settings=settings,
        client=RagClient(
            settings.rag_base_url,
            timeout_s=settings.api_timeout_s,
            member_token=settings.member_token,
        ),
        cooldown=UserCooldown(settings.user_cooldown_s),
        slots=GuildSlots(settings.max_concurrency),
        conversations=ConversationDirectory(),
    )

    @tree.command(name=ASK_COMMAND, description="Ask the club knowledge base")
    @discord.app_commands.describe(
        question="Your question",
        public="Post the answer in the channel instead of only to you",
    )
    async def ask(
        interaction: discord.Interaction,
        question: str,
        public: bool = False,
    ) -> None:
        request = AskRequest(
            question=question,
            public=public,
            user_id=getattr(interaction.user, "id", 0),
            asker=getattr(interaction.user, "display_name", "someone"),
            guild_id=getattr(interaction, "guild_id", None),
            channel_id=channel_id(interaction),
            as_member=is_member(interaction, settings),
        )
        await handle_ask(
            interaction,
            request,
            client=runtime.client,
            cooldown=runtime.cooldown,
            slots=runtime.slots,
            conversations=runtime.conversations,
        )

    @tree.command(name=RESET_COMMAND, description="Start a new conversation in this channel")
    async def reset(interaction: discord.Interaction) -> None:
        await handle_reset(interaction, conversations=runtime.conversations)

    @tree.command(name=STATUS_COMMAND, description="Show the API status and the bot's limits")
    async def status(interaction: discord.Interaction) -> None:
        await handle_status(interaction, client=runtime.client, settings=runtime.settings)

    @client.event
    async def on_ready() -> None:
        await sync_commands(client, tree, settings)
        log.info("bot_ready", user=str(client.user), guild_id=settings.guild_id)

    client.rag_client = runtime.client  # type: ignore[attr-defined] - closed by __main__
    # Same idea, because discord.py keeps no tree on the client: the wiring stays inspectable.
    client.tree = tree  # type: ignore[attr-defined]
    return client
