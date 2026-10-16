"""The only module that knows Discord exists: commands, defer/edit, role check (Req 1, 3, 7).

Default intents only, and no message listener, so the bot needs no privileged intent and no Discord
verification (Req 1.2). Every handler is thin: the decisions live in `commands.py` and the rendering
in `render.py`, which is what lets the whole member-visible behaviour be tested without a gateway.
"""

import asyncio
from dataclasses import dataclass
from time import perf_counter
from typing import Literal

import discord
import structlog

from mcclub_rag.discordbot.client import RagClient
from mcclub_rag.discordbot.commands import (
    AskOutcome,
    AskRequest,
    IngestRequest,
    forget_confirmation,
    ingest_precheck,
    ingest_started_reply,
    precheck,
    run_ask,
    run_documents,
    run_ingest,
    run_reset,
    run_status,
)
from mcclub_rag.discordbot.notices import (
    ADMIN_ONLY,
    ANSWER_UNDELIVERED,
    NoticeLanguage,
    notice,
    notice_language,
)
from mcclub_rag.discordbot.render import EmbedSpec, RenderedReply
from mcclub_rag.discordbot.settings import BotSettings
from mcclub_rag.discordbot.state import ConversationDirectory, GuildSlots, UserCooldown
from mcclub_rag.discordbot.views import DeleteView, FeedbackView

log = structlog.get_logger(__name__)

ASK_COMMAND = "ask"
RESET_COMMAND = "reset"
STATUS_COMMAND = "status"
DOCUMENTS_COMMAND = "documents"
INGEST_COMMAND = "ingest"
FORGET_COMMAND = "forget"

# A running ingest must not be garbage-collected mid-flight (Req 13.3).
_IN_FLIGHT_INGESTS: set[asyncio.Task[None]] = set()


@dataclass
class BotRuntime:
    """What every handler needs; built once in `build_client`."""

    settings: BotSettings
    client: RagClient
    cooldown: UserCooldown
    slots: GuildSlots
    conversations: ConversationDirectory


def _holds_role(interaction: discord.Interaction, role_id: int | None) -> bool:
    """Duck-typed on `roles` rather than `isinstance(Member)`: a DM user has no roles at all."""
    if role_id is None:
        return False
    roles = getattr(interaction.user, "roles", ())
    return any(getattr(role, "id", None) == role_id for role in roles)


def is_member(interaction: discord.Interaction, settings: BotSettings) -> bool:
    """Members access only in a guild that grants it (decision 3; Req 3.1, 3.2, 16.4).

    The role comes from the entry of the guild the interaction arrived in, so the same person in a
    server that does not grant the role is a public caller. The guild is resolved before the role,
    and `access_for` returns `None` for a DM, an unlisted guild and the no-guilds case.
    """
    access = settings.access_for(getattr(interaction, "guild_id", None))
    if access is None:
        return False
    return _holds_role(interaction, access.member_role_id)


def is_admin(interaction: discord.Interaction, settings: BotSettings) -> bool:
    """Admin commands only where a guild grants them, and never by default (Req 11.1, 11.2).

    Fails closed: a guild with no admin role has no admin, and nothing falls back to the member role
    or to Discord's own server permissions. Refusing here costs no API call (Req 11.3).
    """
    access = settings.access_for(getattr(interaction, "guild_id", None))
    if access is None:
        return False
    return _holds_role(interaction, access.admin_role_id)


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


def interaction_language(interaction: discord.Interaction) -> NoticeLanguage:
    """The operator's own Discord language: an admin command carries no question (Req 12.4)."""
    locale = str(getattr(interaction, "locale", "") or "")
    language = locale.split("-")[0].lower()
    return language if language in ("ar", "fr", "en") else "en"


async def refuse_non_admin(
    interaction: discord.Interaction, *, language: NoticeLanguage, command: str
) -> None:
    """One ephemeral notice, no API call, and no hint about the corpus (Req 11.3)."""
    log.info(
        "bot_admin_rejected",
        command=command,
        user_id=getattr(interaction.user, "id", 0),
        guild_id=getattr(interaction, "guild_id", None),
    )
    await interaction.response.send_message(notice(ADMIN_ONLY, language), ephemeral=True)


async def handle_documents(
    interaction: discord.Interaction, *, client: RagClient, settings: BotSettings
) -> None:
    """`/documents`: one synchronous GET, answered ephemerally (Req 12)."""
    language = interaction_language(interaction)
    if not is_admin(interaction, settings):
        await refuse_non_admin(interaction, language=language, command=DOCUMENTS_COMMAND)
        return
    reply = await run_documents(client=client, language=language)
    await interaction.response.send_message(
        content=reply.parts[0].content,
        embed=to_embed(reply.parts[0].embed),
        ephemeral=True,
    )


async def handle_ingest(
    interaction: discord.Interaction,
    request: IngestRequest,
    *,
    client: RagClient,
    settings: BotSettings,
) -> None:
    """`/ingest`: refuse or acknowledge inside the 3-second window, then work in the background."""
    language = interaction_language(interaction)
    if not is_admin(interaction, settings):
        await refuse_non_admin(interaction, language=language, command=INGEST_COMMAND)
        return
    refusal = ingest_precheck(request, language=language)
    if refusal is not None:
        await interaction.response.send_message(refusal.parts[0].content, ephemeral=True)
        return
    try:
        await interaction.response.defer(ephemeral=True)
        await interaction.edit_original_response(
            content=ingest_started_reply(request, language=language).parts[0].content
        )
    except discord.NotFound:  # the token died between the command and the defer
        log.warning("bot_interaction_expired", channel_id=request.channel_id)
        return
    task = asyncio.create_task(
        run_ingest_task(interaction, request, client=client, language=language)
    )
    _IN_FLIGHT_INGESTS.add(task)
    task.add_done_callback(_IN_FLIGHT_INGESTS.discard)


async def run_ingest_task(
    interaction: discord.Interaction,
    request: IngestRequest,
    *,
    client: RagClient,
    language: NoticeLanguage,
) -> None:
    """Report the finished ingest: edit the acknowledgement, or post to the channel (Req 13.3).

    An ingest outlives the 15-minute interaction token on a large scan, so a result that could not
    be edited into the original reply goes to the channel instead: it is an operator confirmation,
    it carries no corpus text, and an operator who never sees it cannot tell whether the corpus
    changed.
    """
    outcome = await run_ingest(request, client=client, language=language)
    fields: dict[str, object] = {
        "name": outcome.name,
        "doc_id": outcome.doc_id,
        "guild_id": request.guild_id,
        "channel_id": request.channel_id,
    }
    if outcome.failure is not None:
        log.warning("bot_ingest_failed", code=outcome.failure.code, **fields)
    else:
        log.info("bot_ingest_done", **fields)
    content = outcome.reply.parts[0].content
    try:
        await interaction.edit_original_response(content=content, view=None)
    except discord.NotFound:
        channel = getattr(interaction, "channel", None)
        try:
            await channel.send(content=content, allowed_mentions=discord.AllowedMentions.none())
        except (AttributeError, discord.HTTPException):
            log.warning("bot_ingest_undelivered", **fields)


async def handle_forget(
    interaction: discord.Interaction,
    doc_id: str,
    *,
    client: RagClient,
    settings: BotSettings,
) -> None:
    """`/forget`: ask first, delete on the press (Req 14.1)."""
    language = interaction_language(interaction)
    if not is_admin(interaction, settings):
        await refuse_non_admin(interaction, language=language, command=FORGET_COMMAND)
        return
    reply = forget_confirmation(doc_id=doc_id, language=language)
    await interaction.response.send_message(
        content=reply.parts[0].content,
        view=DeleteView(doc_id, client=client, language=language),
        ephemeral=True,
    )


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
    """Guild commands in every configured guild, global when none are (Req 1.1, 16.1). Synced once.

    `copy_global_to` is not optional: `@tree.command` registers into the tree's *global* mapping,
    while discord.py's `sync(guild=...)` sends only the commands registered for that guild. Without
    the copy the PUT carries an empty list, Discord answers 200, and the guild sees no commands at
    all (observed on 2026-10-16; EDGE_CASES.md).

    With guilds configured the global list is emptied too: Discord keeps whatever an earlier run
    registered globally, and a guild shows its own commands *and* the global ones, so a bot that
    once ran without `DISCORD_GUILDS` listed every command twice (12 for 6, 2026-10-16).
    """
    if getattr(client, "_commands_synced", False):
        return
    if settings.guild_ids:
        for guild_id in settings.guild_ids:
            guild = discord.Object(id=guild_id)
            tree.copy_global_to(guild=guild)
            await tree.sync(guild=guild)
        tree.clear_commands(guild=None)
        await tree.sync()
    else:
        await tree.sync()
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
            admin_api_key=settings.admin_api_key,
            admin_timeout_s=settings.admin_timeout_s,
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

    @tree.command(name=DOCUMENTS_COMMAND, description="List the documents the bot knows (admins)")
    async def documents(interaction: discord.Interaction) -> None:
        await handle_documents(interaction, client=runtime.client, settings=runtime.settings)

    @tree.command(name=INGEST_COMMAND, description="Add a file or a page to the corpus (admins)")
    @discord.app_commands.describe(
        visibility="Who may see the document",
        file="A file to ingest",
        url="Or a page for the API to fetch",
        topic="Replaces the document already using this topic",
    )
    async def ingest(
        interaction: discord.Interaction,
        visibility: Literal["public", "members"],
        file: discord.Attachment | None = None,
        url: str | None = None,
        topic: str | None = None,
    ) -> None:
        request = IngestRequest(
            filename=file.filename if file is not None else None,
            data=await file.read() if file is not None else None,
            url=url,
            visibility=visibility,
            topic=topic,
            user_id=getattr(interaction.user, "id", 0),
            guild_id=getattr(interaction, "guild_id", None),
            channel_id=channel_id(interaction),
        )
        await handle_ingest(interaction, request, client=runtime.client, settings=runtime.settings)

    @tree.command(name=FORGET_COMMAND, description="Delete a document from the corpus (admins)")
    @discord.app_commands.describe(doc_id="The id from /documents or /ingest")
    async def forget(interaction: discord.Interaction, doc_id: str) -> None:
        await handle_forget(interaction, doc_id, client=runtime.client, settings=runtime.settings)

    @client.event
    async def on_ready() -> None:
        await sync_commands(client, tree, settings)
        log.info("bot_ready", user=str(client.user), guilds=list(settings.guild_ids))

    client.rag_client = runtime.client  # type: ignore[attr-defined] - closed by __main__
    # Same idea, because discord.py keeps no tree on the client: the wiring stays inspectable.
    client.tree = tree  # type: ignore[attr-defined]
    return client
