import discord
from discord.ext import commands
from discord import ui
from discord.utils import escape_mentions
from datetime import datetime, timedelta
import json
import os
import asyncio
import re

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN environment variable is not set.")

DATA_FILE = "confessions.json"

CONF_ID_RE = re.compile(r"#(\d+)")
SUGG_ID_RE = re.compile(r"#(\d+)")
data_lock = asyncio.Lock()

PENDING_IMAGE = {}
PENDING_IMAGE_LOCK = asyncio.Lock()
PENDING_IMAGE_TTL_SECONDS = 60 * 30


def _default_data():
    return {
        "confession_count": 0,
        "confessions": {},
        "message_to_confession": {},
        "suggestion_count": 0,
        "suggestions": {},
        "message_to_suggestion": {},
        "guild_config": {}
    }


def load_data():
    if not os.path.exists(DATA_FILE):
        return _default_data()
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        for k, v in _default_data().items():
            data.setdefault(k, v)
        if not isinstance(data.get("guild_config"), dict):
            data["guild_config"] = {}
        return data
    except Exception:
        return _default_data()


def save_data_atomic(data):
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)
    os.replace(tmp, DATA_FILE)


DATA = load_data()


def get_guild_cfg(guild_id: int):
    return DATA["guild_config"].setdefault(str(guild_id), {})


def set_guild_cfg(guild_id: int, **kwargs):
    cfg = get_guild_cfg(guild_id)
    for k, v in kwargs.items():
        cfg[k] = v


def status_label(status: str):
    return {
        "pending": "🟨 Pending Review",
        "approved": "🟩 Approved",
        "denied": "🟥 Denied",
        "implemented": "✅ Implemented",
    }.get(status, "🟨 Pending Review")


def _rebuild_embed_from(embed: discord.Embed, *, fields, footer_text=None):
    new_embed = discord.Embed(
        title=embed.title,
        description=embed.description,
        color=embed.color.value if embed.color else 0xEB459E,
        timestamp=embed.timestamp
    )
    if embed.author and embed.author.name:
        try:
            new_embed.set_author(name=embed.author.name, icon_url=embed.author.icon_url)
        except Exception:
            new_embed.set_author(name=embed.author.name)
    if embed.thumbnail and embed.thumbnail.url:
        new_embed.set_thumbnail(url=embed.thumbnail.url)
    if embed.image and embed.image.url:
        new_embed.set_image(url=embed.image.url)
    if footer_text is None:
        if embed.footer and embed.footer.text:
            new_embed.set_footer(text=embed.footer.text)
    else:
        new_embed.set_footer(text=footer_text)
    for name, value, inline in fields[:25]:
        new_embed.add_field(name=name, value=value, inline=inline)
    return new_embed


async def _clean_expired_pending():
    async with PENDING_IMAGE_LOCK:
        now = datetime.utcnow().timestamp()
        expired = [k for k, v in PENDING_IMAGE.items() if v["expires_at"] <= now]
        for k in expired:
            PENDING_IMAGE.pop(k, None)


async def _set_pending_image(guild_id: int, suggestion_channel_id: int, suggestion_message_id: int, user_id: int):
    async with PENDING_IMAGE_LOCK:
        PENDING_IMAGE[(guild_id, suggestion_message_id)] = {
            "guild_id": guild_id,
            "channel_id": suggestion_channel_id,
            "message_id": suggestion_message_id,
            "user_id": user_id,
            "created_at": datetime.utcnow().timestamp(),
            "expires_at": (datetime.utcnow() + timedelta(seconds=PENDING_IMAGE_TTL_SECONDS)).timestamp()
        }


async def _clear_pending_image(guild_id: int, suggestion_message_id: int):
    async with PENDING_IMAGE_LOCK:
        PENDING_IMAGE.pop((guild_id, suggestion_message_id), None)


async def _find_pending_for_user(guild_id: int, channel_id: int, user_id: int):
    await _clean_expired_pending()
    async with PENDING_IMAGE_LOCK:
        matches = []
        for (g, mid), v in PENDING_IMAGE.items():
            if g == guild_id and v["channel_id"] == channel_id and v["user_id"] == user_id:
                matches.append(v)
        matches.sort(key=lambda x: x.get("created_at", 0), reverse=True)
        return matches


class ConfessionModal(ui.Modal, title="Submit an Anonymous Confession"):
    confession = ui.TextInput(
        label="Your Confession",
        style=discord.TextStyle.paragraph,
        placeholder="Type your confession here...",
        required=True,
        max_length=1200
    )

    async def on_submit(self, interaction: discord.Interaction):
        if not interaction.guild:
            return await interaction.response.send_message("❌ Use this inside a server.", ephemeral=True)

        guild = interaction.guild
        async with data_lock:
            cfg = get_guild_cfg(guild.id)
            confession_channel_id = cfg.get("confession_channel_id")
            log_channel_id = cfg.get("log_channel_id")

        if not confession_channel_id:
            return await interaction.response.send_message("❌ Confession panel not set. Run `!panel` in the channel you want.", ephemeral=True)

        confession_channel = guild.get_channel(int(confession_channel_id))
        log_channel = guild.get_channel(int(log_channel_id)) if log_channel_id else None

        if confession_channel is None:
            return await interaction.response.send_message("❌ Confession channel not found. Run `!panel` again.", ephemeral=True)

        text = escape_mentions(self.confession.value).strip()
        if not text:
            return await interaction.response.send_message("❌ Empty confession.", ephemeral=True)

        async with data_lock:
            DATA["confession_count"] += 1
            cid = int(DATA["confession_count"])

        confession_embed = discord.Embed(
            title=f"Anonymous Confession (#{cid})",
            description=f"“{text}”",
            color=0x5865F2,
            timestamp=datetime.utcnow()
        )
        confession_embed.set_footer(text="Reply anonymously using the button below.")

        msg = await confession_channel.send(embed=confession_embed, view=ConfessionPersistentView())

        async with data_lock:
            DATA["confessions"][str(cid)] = {
                "content": text,
                "user_id": interaction.user.id,
                "username": str(interaction.user),
                "account_created": interaction.user.created_at.strftime("%Y-%m-%d"),
                "timestamp": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                "message_id": msg.id,
                "channel_id": confession_channel.id,
                "guild_id": guild.id,
                "jump_url": msg.jump_url,
                "thread_id": None,
                "replies": []
            }
            DATA["message_to_confession"][str(msg.id)] = cid
            save_data_atomic(DATA)

        if log_channel:
            log_embed = discord.Embed(
                title=f"🔒 Confession #{cid} — Log",
                color=0xED4245,
                timestamp=datetime.utcnow()
            )
            log_embed.add_field(name="Server", value=f"{guild.name} (`{guild.id}`)", inline=False)
            log_embed.add_field(name="User", value=f"{interaction.user} (`{interaction.user.id}`)", inline=False)
            log_embed.add_field(name="Account Created", value=interaction.user.created_at.strftime("%Y-%m-%d"), inline=True)
            if isinstance(interaction.user, discord.Member) and interaction.user.joined_at:
                log_embed.add_field(name="Joined Server", value=interaction.user.joined_at.strftime("%Y-%m-%d"), inline=True)
            log_embed.add_field(name="Confession", value=text[:1024], inline=False)
            log_embed.add_field(name="Message Link", value=f"[Jump]({msg.jump_url})", inline=False)
            log_embed.set_thumbnail(url=interaction.user.display_avatar.url)
            try:
                await log_channel.send(embed=log_embed)
            except Exception:
                pass

        await interaction.response.send_message("✅ Confession submitted anonymously.", ephemeral=True)


class ReplyModal(ui.Modal, title="Reply Anonymously"):
    reply = ui.TextInput(
        label="Your Reply",
        style=discord.TextStyle.paragraph,
        placeholder="Type your reply here...",
        required=True,
        max_length=800
    )

    def __init__(self, confession_id: int, confession_message: discord.Message):
        super().__init__()
        self.confession_id = confession_id
        self.confession_message = confession_message

    async def on_submit(self, interaction: discord.Interaction):
        if not interaction.guild:
            return await interaction.response.send_message("❌ Use this inside a server.", ephemeral=True)

        guild = interaction.guild
        async with data_lock:
            cfg = get_guild_cfg(guild.id)
            confession_channel_id = cfg.get("confession_channel_id")
            log_channel_id = cfg.get("log_channel_id")

        confession_channel = guild.get_channel(int(confession_channel_id)) if confession_channel_id else None
        log_channel = guild.get_channel(int(log_channel_id)) if log_channel_id else None

        text = escape_mentions(self.reply.value).strip()
        if not text:
            return await interaction.response.send_message("❌ Empty reply.", ephemeral=True)

        cid = int(self.confession_id)

        async with data_lock:
            rec = DATA["confessions"].get(str(cid))
            if not rec:
                return await interaction.response.send_message("❌ Confession not found.", ephemeral=True)

            reply_obj = {
                "content": text,
                "timestamp": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                "user_id": interaction.user.id,
                "username": str(interaction.user)
            }
            rec["replies"].append(reply_obj)
            save_data_atomic(DATA)

        posted_somewhere = False
        try:
            thread_id = rec.get("thread_id")
            thread = guild.get_thread(int(thread_id)) if thread_id else None

            if thread is None:
                thread = await self.confession_message.create_thread(
                    name=f"Replies #{cid}",
                    auto_archive_duration=1440
                )
                async with data_lock:
                    rec["thread_id"] = thread.id
                    save_data_atomic(DATA)

            reply_embed = discord.Embed(
                title=f"Anonymous Reply → Confession #{cid}",
                description=f"“{text}”",
                color=0x99AAB5,
                timestamp=datetime.utcnow()
            )
            await thread.send(embed=reply_embed, allowed_mentions=discord.AllowedMentions.none())
            posted_somewhere = True
        except Exception:
            if confession_channel:
                try:
                    reply_embed = discord.Embed(
                        title=f"Anonymous Reply → Confession #{cid}",
                        description=f"“{text}”",
                        color=0x99AAB5,
                        timestamp=datetime.utcnow()
                    )
                    await confession_channel.send(embed=reply_embed, allowed_mentions=discord.AllowedMentions.none())
                    posted_somewhere = True
                except Exception:
                    posted_somewhere = False

        if log_channel:
            log_embed = discord.Embed(
                title=f"🔒 Reply to Confession #{cid} — Log",
                color=0xFEE75C,
                timestamp=datetime.utcnow()
            )
            log_embed.add_field(name="Server", value=f"{guild.name} (`{guild.id}`)", inline=False)
            log_embed.add_field(name="User", value=f"{interaction.user} (`{interaction.user.id}`)", inline=False)
            log_embed.add_field(name="Reply", value=text[:1024], inline=False)
            log_embed.add_field(
                name="Confession Link",
                value=f"[Jump]({rec.get('jump_url', self.confession_message.jump_url)})",
                inline=False
            )
            log_embed.set_thumbnail(url=interaction.user.display_avatar.url)
            try:
                await log_channel.send(embed=log_embed)
            except Exception:
                pass

        if posted_somewhere:
            await interaction.response.send_message("💬 Reply sent anonymously.", ephemeral=True)
        else:
            await interaction.response.send_message("✅ Reply saved, but I couldn't post it.", ephemeral=True)


class ConfessionPersistentView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @ui.button(label="Submit a confession!", emoji="📝", style=discord.ButtonStyle.primary, custom_id="confession:submit")
    async def submit(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(ConfessionModal())

    @ui.button(label="Reply", emoji="💬", style=discord.ButtonStyle.secondary, custom_id="confession:reply")
    async def reply(self, interaction: discord.Interaction, button: ui.Button):
        message = interaction.message
        if message is None:
            return await interaction.response.send_message("❌ No message context.", ephemeral=True)

        async with data_lock:
            cid = DATA["message_to_confession"].get(str(message.id))

        if cid is None and message.embeds:
            title = message.embeds[0].title or ""
            m = CONF_ID_RE.search(title)
            if m:
                cid = int(m.group(1))

        if cid is None:
            return await interaction.response.send_message("❌ I can't detect which confession this is.", ephemeral=True)

        await interaction.response.send_modal(ReplyModal(int(cid), message))


class SuggestionModal(ui.Modal, title="Submit a Suggestion"):
    title_in = ui.TextInput(
        label="Title",
        placeholder="Short title (e.g., Add a music channel)",
        required=True,
        max_length=80
    )
    details = ui.TextInput(
        label="Details",
        style=discord.TextStyle.paragraph,
        placeholder="Explain your idea clearly. Include why it helps.",
        required=True,
        max_length=1200
    )

    async def on_submit(self, interaction: discord.Interaction):
        if not interaction.guild:
            return await interaction.response.send_message("❌ Use this inside a server.", ephemeral=True)

        guild = interaction.guild
        async with data_lock:
            cfg = get_guild_cfg(guild.id)
            suggestion_channel_id = cfg.get("suggestion_channel_id")
            log_channel_id = cfg.get("log_channel_id")

        if not suggestion_channel_id:
            return await interaction.response.send_message("❌ Suggestion panel not set. Run `!suggestionpanel` in the channel you want.", ephemeral=True)

        suggestion_channel = guild.get_channel(int(suggestion_channel_id))
        log_channel = guild.get_channel(int(log_channel_id)) if log_channel_id else None

        if suggestion_channel is None:
            return await interaction.response.send_message("❌ Suggestion channel not found. Run `!suggestionpanel` again.", ephemeral=True)

        title = escape_mentions(self.title_in.value).strip()
        text = escape_mentions(self.details.value).strip()
        if not title or not text:
            return await interaction.response.send_message("❌ Empty suggestion.", ephemeral=True)

        async with data_lock:
            DATA["suggestion_count"] += 1
            sid = int(DATA["suggestion_count"])

        embed = discord.Embed(
            title=f"✨ Suggestion #{sid}",
            description=f"**{title}**\n\n{text}",
            color=0xEB459E,
            timestamp=datetime.utcnow()
        )
        embed.set_author(name=str(interaction.user), icon_url=interaction.user.display_avatar.url)
        embed.add_field(name="Status", value=f"**{status_label('pending')}**", inline=True)
        embed.add_field(name="Votes", value="👍 0  |  👎 0", inline=True)
        embed.add_field(name="Image", value="Use **Attach Image** or **No Image** below.", inline=False)
        embed.set_footer(text="Vote below • Mods can update status")

        msg = await suggestion_channel.send(embed=embed, view=SuggestionView())

        async with data_lock:
            DATA["suggestions"][str(sid)] = {
                "guild_id": guild.id,
                "title": title,
                "content": text,
                "status": "pending",
                "user_id": interaction.user.id,
                "username": str(interaction.user),
                "timestamp": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                "message_id": msg.id,
                "channel_id": suggestion_channel.id,
                "jump_url": msg.jump_url,
                "image_url": None,
                "upvotes": [],
                "downvotes": []
            }
            DATA["message_to_suggestion"][str(msg.id)] = sid
            save_data_atomic(DATA)

        if log_channel:
            log_embed = discord.Embed(
                title=f"📥 Suggestion #{sid} — Log",
                color=0x57F287,
                timestamp=datetime.utcnow()
            )
            log_embed.add_field(name="Server", value=f"{guild.name} (`{guild.id}`)", inline=False)
            log_embed.add_field(name="User", value=f"{interaction.user} (`{interaction.user.id}`)", inline=False)
            log_embed.add_field(name="Title", value=title, inline=False)
            log_embed.add_field(name="Suggestion", value=text[:1024], inline=False)
            log_embed.add_field(name="Message Link", value=f"[Jump]({msg.jump_url})", inline=False)
            log_embed.set_thumbnail(url=interaction.user.display_avatar.url)
            try:
                await log_channel.send(embed=log_embed)
            except Exception:
                pass

        await interaction.response.send_message("✅ Posted! On your suggestion, press **Attach Image** or **No Image**.", ephemeral=True)


class SuggestionStatusSelect(ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Pending Review", value="pending", emoji="🟨"),
            discord.SelectOption(label="Approved", value="approved", emoji="🟩"),
            discord.SelectOption(label="Denied", value="denied", emoji="🟥"),
            discord.SelectOption(label="Implemented", value="implemented", emoji="✅"),
        ]
        super().__init__(
            placeholder="🛠️ Moderator: Update status…",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="suggestion:status_select"
        )

    async def callback(self, interaction: discord.Interaction):
        if not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message("❌ Mods only (Manage Server required).", ephemeral=True)

        message = interaction.message
        if message is None or not message.embeds:
            return await interaction.response.send_message("❌ Missing embed.", ephemeral=True)

        async with data_lock:
            sid = DATA["message_to_suggestion"].get(str(message.id))

        if sid is None:
            t = message.embeds[0].title or ""
            m = SUGG_ID_RE.search(t)
            if m:
                sid = int(m.group(1))

        if sid is None:
            return await interaction.response.send_message("❌ Can't detect suggestion ID.", ephemeral=True)

        new_status = self.values[0]
        label = status_label(new_status)

        e = message.embeds[0]
        fields = [(f.name, f.value, f.inline) for f in e.fields]
        updated = False
        for i, (name, value, inline) in enumerate(fields):
            if name.lower() == "status":
                fields[i] = ("Status", f"**{label}**", True)
                updated = True
                break
        if not updated:
            fields.insert(0, ("Status", f"**{label}**", True))

        new_embed = _rebuild_embed_from(e, fields=fields)

        async with data_lock:
            rec = DATA["suggestions"].get(str(sid))
            if rec:
                rec["status"] = new_status
                save_data_atomic(DATA)

        await message.edit(embed=new_embed, view=SuggestionView())
        await interaction.response.send_message(f"✅ Status updated to **{label}**.", ephemeral=True)


class SuggestionView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(SuggestionStatusSelect())

    @ui.button(label="Upvote", emoji="👍", style=discord.ButtonStyle.success, custom_id="suggestion:upvote")
    async def upvote(self, interaction: discord.Interaction, button: ui.Button):
        await self._vote(interaction, up=True)

    @ui.button(label="Downvote", emoji="👎", style=discord.ButtonStyle.danger, custom_id="suggestion:downvote")
    async def downvote(self, interaction: discord.Interaction, button: ui.Button):
        await self._vote(interaction, up=False)

    @ui.button(label="Attach Image", emoji="🖼️", style=discord.ButtonStyle.primary, custom_id="suggestion:attach_image")
    async def attach_image(self, interaction: discord.Interaction, button: ui.Button):
        msg = interaction.message
        if not msg or not interaction.guild:
            return await interaction.response.send_message("❌ Missing context.", ephemeral=True)

        guild = interaction.guild
        async with data_lock:
            sid = DATA["message_to_suggestion"].get(str(msg.id))
            rec = DATA["suggestions"].get(str(sid)) if sid else None
            cfg = get_guild_cfg(guild.id)
            suggestion_channel_id = cfg.get("suggestion_channel_id")

        if not sid or not rec:
            return await interaction.response.send_message("❌ Can't detect this suggestion.", ephemeral=True)

        is_owner = rec.get("user_id") == interaction.user.id
        is_mod = isinstance(interaction.user, discord.Member) and interaction.user.guild_permissions.manage_guild
        if not (is_owner or is_mod):
            return await interaction.response.send_message("❌ Only the suggester (or a mod) can attach an image.", ephemeral=True)

        if not suggestion_channel_id or int(suggestion_channel_id) != msg.channel.id:
            return await interaction.response.send_message("❌ This suggestion is not in the configured suggestion channel.", ephemeral=True)

        await _set_pending_image(guild.id, msg.channel.id, msg.id, rec.get("user_id"))
        await interaction.response.send_message("🖼️ Send an image in this channel now — I’ll attach it to your latest suggestion.", ephemeral=True)

    @ui.button(label="No Image", emoji="🚫", style=discord.ButtonStyle.secondary, custom_id="suggestion:no_image")
    async def no_image(self, interaction: discord.Interaction, button: ui.Button):
        msg = interaction.message
        if not msg or not interaction.guild:
            return await interaction.response.send_message("❌ Missing context.", ephemeral=True)

        async with data_lock:
            sid = DATA["message_to_suggestion"].get(str(msg.id))
            rec = DATA["suggestions"].get(str(sid)) if sid else None

        if not sid or not rec:
            return await interaction.response.send_message("❌ Can't detect this suggestion.", ephemeral=True)

        is_owner = rec.get("user_id") == interaction.user.id
        is_mod = isinstance(interaction.user, discord.Member) and interaction.user.guild_permissions.manage_guild
        if not (is_owner or is_mod):
            return await interaction.response.send_message("❌ Only the suggester (or a mod) can choose this.", ephemeral=True)

        await _clear_pending_image(interaction.guild.id, msg.id)
        await interaction.response.send_message("✅ Got it — no image will be attached.", ephemeral=True)

    @ui.button(label="Open", emoji="🔗", style=discord.ButtonStyle.secondary, custom_id="suggestion:link")
    async def link(self, interaction: discord.Interaction, button: ui.Button):
        msg = interaction.message
        if not msg:
            return await interaction.response.send_message("❌ No message.", ephemeral=True)
        await interaction.response.send_message(msg.jump_url, ephemeral=True)

    async def _vote(self, interaction: discord.Interaction, up: bool):
        msg = interaction.message
        if msg is None or not msg.embeds:
            return await interaction.response.send_message("❌ Missing embed.", ephemeral=True)

        async with data_lock:
            sid = DATA["message_to_suggestion"].get(str(msg.id))
            if sid is None:
                t = msg.embeds[0].title or ""
                m = SUGG_ID_RE.search(t)
                if m:
                    sid = int(m.group(1))

            if sid is None:
                return await interaction.response.send_message("❌ Can't detect suggestion ID.", ephemeral=True)

            rec = DATA["suggestions"].get(str(sid))
            if not rec:
                return await interaction.response.send_message("❌ Suggestion not found.", ephemeral=True)

            uid = interaction.user.id
            upvotes = set(rec.get("upvotes", []))
            downvotes = set(rec.get("downvotes", []))

            if up:
                if uid in upvotes:
                    upvotes.remove(uid)
                else:
                    upvotes.add(uid)
                    downvotes.discard(uid)
            else:
                if uid in downvotes:
                    downvotes.remove(uid)
                else:
                    downvotes.add(uid)
                    upvotes.discard(uid)

            rec["upvotes"] = list(upvotes)
            rec["downvotes"] = list(downvotes)
            save_data_atomic(DATA)

            up_count = len(rec["upvotes"])
            down_count = len(rec["downvotes"])

        e = msg.embeds[0]
        fields = [(f.name, f.value, f.inline) for f in e.fields]
        votes_value = f"👍 {up_count}  |  👎 {down_count}"

        updated = False
        for i, (name, value, inline) in enumerate(fields):
            if name.lower() == "votes":
                fields[i] = ("Votes", votes_value, True)
                updated = True
                break
        if not updated:
            fields.append(("Votes", votes_value, True))

        new_embed = _rebuild_embed_from(e, fields=fields)
        await msg.edit(embed=new_embed, view=SuggestionView())
        await interaction.response.send_message("✅ Vote updated.", ephemeral=True)


class SuggestionPanelView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @ui.button(label="Submit Suggestion", emoji="💡", style=discord.ButtonStyle.primary, custom_id="suggestion:open_modal")
    async def open_modal(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(SuggestionModal())

    @ui.button(label="How it works", emoji="📌", style=discord.ButtonStyle.secondary, custom_id="suggestion:how")
    async def how(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_message(
            "1) Click **Submit Suggestion**\n"
            "2) Fill title + details\n"
            "3) On your suggestion: press **Attach Image** or **No Image**\n"
            "4) Send an image normally (no reply needed)\n"
            "5) Vote with 👍/👎\n"
            "6) Mods update status using the dropdown",
            ephemeral=True
        )


class HiraBot(commands.Bot):
    async def setup_hook(self):
        self.add_view(ConfessionPersistentView())
        self.add_view(SuggestionView())
        self.add_view(SuggestionPanelView())


bot = HiraBot(command_prefix="!", intents=discord.Intents.all())


@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} ({bot.user.id})")


@bot.event
async def on_message(message: discord.Message):
    await bot.process_commands(message)

    if message.author.bot:
        return
    if not message.guild:
        return
    if not message.attachments:
        return

    async with data_lock:
        cfg = get_guild_cfg(message.guild.id)
        suggestion_channel_id = cfg.get("suggestion_channel_id")

    if not suggestion_channel_id:
        return
    if message.channel.id != int(suggestion_channel_id):
        return

    await _clean_expired_pending()

    target_suggestion_message_id = None

    if message.reference and message.reference.message_id:
        ref_id = int(message.reference.message_id)
        async with PENDING_IMAGE_LOCK:
            key = (message.guild.id, ref_id)
            pending = PENDING_IMAGE.get(key)
            if pending and pending["user_id"] == message.author.id and pending["channel_id"] == message.channel.id:
                target_suggestion_message_id = ref_id

    if target_suggestion_message_id is None:
        pending_list = await _find_pending_for_user(message.guild.id, message.channel.id, message.author.id)
        if not pending_list:
            return
        target_suggestion_message_id = int(pending_list[0]["message_id"])

    attachment = message.attachments[0]

    async with data_lock:
        sid = DATA["message_to_suggestion"].get(str(target_suggestion_message_id))
        rec = DATA["suggestions"].get(str(sid)) if sid else None
        if not sid or not rec:
            await _clear_pending_image(message.guild.id, target_suggestion_message_id)
            return
        rec["image_url"] = attachment.url
        save_data_atomic(DATA)

    try:
        suggestion_msg = await message.channel.fetch_message(target_suggestion_message_id)
        if suggestion_msg and suggestion_msg.embeds:
            e = suggestion_msg.embeds[0]
            fields = [(f.name, f.value, f.inline) for f in e.fields]
            new_embed = _rebuild_embed_from(e, fields=fields)
            new_embed.set_image(url=attachment.url)
            await suggestion_msg.edit(embed=new_embed, view=SuggestionView())
    except Exception:
        pass

    await _clear_pending_image(message.guild.id, target_suggestion_message_id)

    try:
        await message.delete()
    except Exception:
        pass


@bot.command(name="panel")
@commands.has_permissions(administrator=True)
async def panel(ctx: commands.Context):
    if not ctx.guild:
        return
    async with data_lock:
        set_guild_cfg(ctx.guild.id, confession_channel_id=ctx.channel.id)
        save_data_atomic(DATA)
    embed = discord.Embed(
        title="💌 Anonymous Confessions",
        description="Click **Submit a confession!** to post anonymously.\nUse **Reply** under a confession to reply anonymously.",
        color=0x57F287
    )
    embed.set_footer(text="This channel is now the confession channel for this server.")
    await ctx.send(embed=embed, view=ConfessionPersistentView())


@bot.command(name="suggestionpanel")
@commands.has_permissions(administrator=True)
async def suggestionpanel(ctx: commands.Context):
    if not ctx.guild:
        return
    async with data_lock:
        set_guild_cfg(ctx.guild.id, suggestion_channel_id=ctx.channel.id)
        save_data_atomic(DATA)
    embed = discord.Embed(
        title="✨ Suggestions Box",
        description="Drop ideas to improve the server.\n\n💡 Submit an idea\n👍 Community votes\n🛠️ Mods set status\n🖼️ Attach Image / No Image buttons on your post",
        color=0xEB459E
    )
    embed.set_footer(text="This channel is now the suggestion channel for this server.")
    await ctx.send(embed=embed, view=SuggestionPanelView())


@bot.command(name="panel2")
@commands.has_permissions(administrator=True)
async def panel2(ctx: commands.Context):
    if not ctx.guild:
        return
    async with data_lock:
        set_guild_cfg(ctx.guild.id, log_channel_id=ctx.channel.id)
        save_data_atomic(DATA)
    embed = discord.Embed(
        title="🧾 Logs Enabled",
        description="This channel is now the log channel for confessions + suggestions.",
        color=0x5865F2
    )
    await ctx.send(embed=embed)


@bot.command(name="rebuildmap")
@commands.has_permissions(administrator=True)
async def rebuildmap(ctx: commands.Context):
    rebuilt = 0
    async with data_lock:
        DATA["message_to_confession"] = {}
        for cid_str, rec in DATA["confessions"].items():
            mid = rec.get("message_id")
            if mid:
                DATA["message_to_confession"][str(mid)] = int(cid_str)
                rebuilt += 1
        save_data_atomic(DATA)
    await ctx.send(f"✅ Rebuilt mapping for `{rebuilt}` confessions.")


@bot.command(name="rebuildsuggestmap")
@commands.has_permissions(administrator=True)
async def rebuildsuggestmap(ctx: commands.Context):
    rebuilt = 0
    async with data_lock:
        DATA["message_to_suggestion"] = {}
        for sid_str, rec in DATA["suggestions"].items():
            mid = rec.get("message_id")
            if mid:
                DATA["message_to_suggestion"][str(mid)] = int(sid_str)
                rebuilt += 1
        save_data_atomic(DATA)
    await ctx.send(f"✅ Rebuilt mapping for `{rebuilt}` suggestions.")


bot.run(TOKEN)
