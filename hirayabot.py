import discord
from discord.ext import commands
from discord import ui
from discord.utils import escape_mentions
from datetime import datetime
import json
import os
import asyncio
import re

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN environment variable is not set.")

GUILD_ID = 417323686018940928
CONFESSION_CHANNEL_ID = 1461951872364449984
LOG_CHANNEL_ID = 1461951962965868680
SUGGESTION_CHANNEL_ID = 1469255656513998941

DATA_FILE = "confessions.json"

CONF_ID_RE = re.compile(r"#(\d+)")
SUGG_ID_RE = re.compile(r"#(\d+)")
data_lock = asyncio.Lock()


def _default_data():
    return {
        "confession_count": 0,
        "confessions": {},
        "message_to_confession": {},
        "suggestion_count": 0,
        "suggestions": {},
        "message_to_suggestion": {}
    }


def load_data():
    if not os.path.exists(DATA_FILE):
        return _default_data()
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        for k, v in _default_data().items():
            data.setdefault(k, v)
        return data
    except Exception:
        return _default_data()


def save_data_atomic(data):
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)
    os.replace(tmp, DATA_FILE)


DATA = load_data()


class ConfessionModal(ui.Modal, title="Submit an Anonymous Confession"):
    confession = ui.TextInput(
        label="Your Confession",
        style=discord.TextStyle.paragraph,
        placeholder="Type your confession here...",
        required=True,
        max_length=1200
    )

    async def on_submit(self, interaction: discord.Interaction):
        if not interaction.guild or interaction.guild.id != GUILD_ID:
            return await interaction.response.send_message("❌ Wrong server.", ephemeral=True)

        guild = interaction.guild
        confession_channel = guild.get_channel(CONFESSION_CHANNEL_ID)
        log_channel = guild.get_channel(LOG_CHANNEL_ID)

        if confession_channel is None or log_channel is None:
            return await interaction.response.send_message("❌ Channels not found. Check IDs.", ephemeral=True)

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
        confession_embed.set_footer(text="Use the buttons below to submit or reply anonymously.")

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
                "jump_url": msg.jump_url,
                "thread_id": None,
                "replies": []
            }
            DATA["message_to_confession"][str(msg.id)] = cid
            save_data_atomic(DATA)

        log_embed = discord.Embed(
            title=f"🔒 Confession #{cid} — Log",
            color=0xED4245,
            timestamp=datetime.utcnow()
        )
        log_embed.add_field(name="User", value=f"{interaction.user} (`{interaction.user.id}`)", inline=False)
        log_embed.add_field(name="Account Created", value=interaction.user.created_at.strftime("%Y-%m-%d"), inline=True)
        if isinstance(interaction.user, discord.Member) and interaction.user.joined_at:
            log_embed.add_field(name="Joined Server", value=interaction.user.joined_at.strftime("%Y-%m-%d"), inline=True)
        log_embed.add_field(name="Confession", value=text[:1024], inline=False)
        log_embed.add_field(name="Message Link", value=f"[Jump to confession]({msg.jump_url})", inline=False)
        log_embed.set_thumbnail(url=interaction.user.display_avatar.url)

        await log_channel.send(embed=log_embed)
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
        if not interaction.guild or interaction.guild.id != GUILD_ID:
            return await interaction.response.send_message("❌ Wrong server.", ephemeral=True)

        guild = interaction.guild
        confession_channel = guild.get_channel(CONFESSION_CHANNEL_ID)
        log_channel = guild.get_channel(LOG_CHANNEL_ID)

        if confession_channel is None or log_channel is None:
            return await interaction.response.send_message("❌ Channels not found. Check IDs.", ephemeral=True)

        text = escape_mentions(self.reply.value).strip()
        if not text:
            return await interaction.response.send_message("❌ Empty reply.", ephemeral=True)

        cid = int(self.confession_id)

        async with data_lock:
            rec = DATA["confessions"].get(str(cid))
            if not rec:
                return await interaction.response.send_message("❌ Confession not found in data.", ephemeral=True)

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
            thread = None
            if thread_id:
                thread = guild.get_thread(int(thread_id))

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

        log_embed = discord.Embed(
            title=f"🔒 Reply to Confession #{cid} — Log",
            color=0xFEE75C,
            timestamp=datetime.utcnow()
        )
        log_embed.add_field(name="User", value=f"{interaction.user} (`{interaction.user.id}`)", inline=False)
        log_embed.add_field(name="Reply", value=text[:1024], inline=False)
        log_embed.add_field(
            name="Confession Link",
            value=f"[Jump]({rec.get('jump_url', self.confession_message.jump_url)})",
            inline=False
        )
        log_embed.set_thumbnail(url=interaction.user.display_avatar.url)
        await log_channel.send(embed=log_embed)

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
        label="Suggestion Title",
        placeholder="Short title (e.g., Add a music channel)",
        required=True,
        max_length=80
    )
    details = ui.TextInput(
        label="Suggestion Details",
        style=discord.TextStyle.paragraph,
        placeholder="Explain your suggestion clearly and why it helps.",
        required=True,
        max_length=1200
    )

    async def on_submit(self, interaction: discord.Interaction):
        if not interaction.guild or interaction.guild.id != GUILD_ID:
            return await interaction.response.send_message("❌ Wrong server.", ephemeral=True)

        guild = interaction.guild
        suggestion_channel = guild.get_channel(SUGGESTION_CHANNEL_ID)
        log_channel = guild.get_channel(LOG_CHANNEL_ID)

        if suggestion_channel is None or log_channel is None:
            return await interaction.response.send_message("❌ Channels not found. Check IDs.", ephemeral=True)

        title = escape_mentions(self.title_in.value).strip()
        text = escape_mentions(self.details.value).strip()
        if not title or not text:
            return await interaction.response.send_message("❌ Empty suggestion.", ephemeral=True)

        async with data_lock:
            DATA["suggestion_count"] += 1
            sid = int(DATA["suggestion_count"])

        embed = discord.Embed(
            title=f"💡 Suggestion (#{sid}) — {title}",
            description=text,
            color=0xEB459E,
            timestamp=datetime.utcnow()
        )
        embed.add_field(name="Status", value="🟨 **Pending Review**", inline=True)
        embed.add_field(name="Votes", value="👍 0  |  👎 0", inline=True)
        embed.set_footer(text="Vote below • Mods can update status • Reply to this suggestion with 1 image to attach")

        msg = await suggestion_channel.send(embed=embed, view=SuggestionView())

        async with data_lock:
            DATA["suggestions"][str(sid)] = {
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

        log_embed = discord.Embed(
            title=f"📥 Suggestion #{sid} — Log",
            color=0x57F287,
            timestamp=datetime.utcnow()
        )
        log_embed.add_field(name="User", value=f"{interaction.user} (`{interaction.user.id}`)", inline=False)
        log_embed.add_field(name="Title", value=title, inline=False)
        log_embed.add_field(name="Suggestion", value=text[:1024], inline=False)
        log_embed.add_field(name="Message Link", value=f"[Jump]({msg.jump_url})", inline=False)
        log_embed.set_thumbnail(url=interaction.user.display_avatar.url)
        await log_channel.send(embed=log_embed)

        await interaction.response.send_message(
            "✅ Suggestion posted! Optional image: go to the suggestion channel and **reply to your suggestion** with an image within **60s**.",
            ephemeral=True
        )

        try:
            def check(m: discord.Message):
                if m.author.id != interaction.user.id:
                    return False
                if m.channel.id != suggestion_channel.id:
                    return False
                if not m.attachments:
                    return False
                if m.reference is None or m.reference.message_id is None:
                    return False
                return int(m.reference.message_id) == int(msg.id)

            img_msg = await bot.wait_for("message", timeout=60.0, check=check)
            attachment = img_msg.attachments[0]

            async with data_lock:
                rec = DATA["suggestions"].get(str(sid))
                if rec:
                    rec["image_url"] = attachment.url
                    save_data_atomic(DATA)

            try:
                original = await suggestion_channel.fetch_message(msg.id)
                if original and original.embeds:
                    new_embed = original.embeds[0]
                    new_embed.set_image(url=attachment.url)
                    await original.edit(embed=new_embed, view=SuggestionView())
            except Exception:
                pass

            try:
                await img_msg.reply("✅ Image attached to the suggestion!", mention_author=False)
            except Exception:
                pass

        except asyncio.TimeoutError:
            pass
        except Exception:
            pass


class SuggestionStatusSelect(ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Pending Review", value="pending", emoji="🟨", description="Default status"),
            discord.SelectOption(label="Approved", value="approved", emoji="🟩", description="Accepted / will do"),
            discord.SelectOption(label="Denied", value="denied", emoji="🟥", description="Not moving forward"),
            discord.SelectOption(label="Implemented", value="implemented", emoji="✅", description="Already done"),
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
        if message is None:
            return await interaction.response.send_message("❌ No message context.", ephemeral=True)

        async with data_lock:
            sid = DATA["message_to_suggestion"].get(str(message.id))

        if sid is None and message.embeds:
            title = message.embeds[0].title or ""
            m = SUGG_ID_RE.search(title)
            if m:
                sid = int(m.group(1))

        if sid is None:
            return await interaction.response.send_message("❌ Can't detect suggestion ID.", ephemeral=True)

        new_status = self.values[0]
        status_label = {
            "pending": "🟨 **Pending Review**",
            "approved": "🟩 **Approved**",
            "denied": "🟥 **Denied**",
            "implemented": "✅ **Implemented**",
        }.get(new_status, "🟨 **Pending Review**")

        if not message.embeds:
            return await interaction.response.send_message("❌ Missing embed.", ephemeral=True)

        embed = message.embeds[0]
        fields = list(embed.fields)

        replaced = False
        for i, f in enumerate(fields):
            if f.name.lower() == "status":
                fields[i] = discord.EmbedField(name="Status", value=status_label, inline=True)
                replaced = True
                break
        if not replaced:
            fields.insert(0, discord.EmbedField(name="Status", value=status_label, inline=True))

        new_embed = discord.Embed(
            title=embed.title,
            description=embed.description,
            color=embed.color.value if embed.color else 0xEB459E,
            timestamp=embed.timestamp
        )
        if embed.image and embed.image.url:
            new_embed.set_image(url=embed.image.url)
        new_embed.set_footer(text=embed.footer.text if embed.footer else "Vote below • Mods can update status")
        for f in fields[:25]:
            new_embed.add_field(name=f.name, value=f.value, inline=f.inline)

        async with data_lock:
            rec = DATA["suggestions"].get(str(sid))
            if rec:
                rec["status"] = new_status
                save_data_atomic(DATA)

        await message.edit(embed=new_embed, view=SuggestionView())
        await interaction.response.send_message(f"✅ Status updated to **{new_status}**.", ephemeral=True)


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

    @ui.button(label="Link", emoji="🔗", style=discord.ButtonStyle.secondary, custom_id="suggestion:link")
    async def link(self, interaction: discord.Interaction, button: ui.Button):
        msg = interaction.message
        if not msg:
            return await interaction.response.send_message("❌ No message.", ephemeral=True)
        await interaction.response.send_message(f"🔗 {msg.jump_url}", ephemeral=True)

    async def _vote(self, interaction: discord.Interaction, up: bool):
        msg = interaction.message
        if msg is None:
            return await interaction.response.send_message("❌ No message context.", ephemeral=True)

        async with data_lock:
            sid = DATA["message_to_suggestion"].get(str(msg.id))
            if sid is None and msg.embeds:
                title = msg.embeds[0].title or ""
                m = SUGG_ID_RE.search(title)
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

        if not msg.embeds:
            return await interaction.response.send_message("❌ Missing embed.", ephemeral=True)

        embed = msg.embeds[0]
        votes_value = f"👍 {up_count}  |  👎 {down_count}"

        fields = list(embed.fields)
        replaced = False
        for i, f in enumerate(fields):
            if f.name.lower() == "votes":
                fields[i] = discord.EmbedField(name="Votes", value=votes_value, inline=True)
                replaced = True
                break
        if not replaced:
            fields.append(discord.EmbedField(name="Votes", value=votes_value, inline=True))

        new_embed = discord.Embed(
            title=embed.title,
            description=embed.description,
            color=embed.color.value if embed.color else 0xEB459E,
            timestamp=embed.timestamp
        )
        if embed.image and embed.image.url:
            new_embed.set_image(url=embed.image.url)
        new_embed.set_footer(text=embed.footer.text if embed.footer else "Vote below • Mods can update status")
        for f in fields[:25]:
            new_embed.add_field(name=f.name, value=f.value, inline=f.inline)

        await msg.edit(embed=new_embed, view=SuggestionView())
        await interaction.response.send_message("✅ Vote updated.", ephemeral=True)


class SuggestionPanelView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @ui.button(
        label="Submit Suggestion",
        emoji="💡",
        style=discord.ButtonStyle.primary,
        custom_id="suggestion:open_modal"
    )
    async def open_modal(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(SuggestionModal())


class HiraBot(commands.Bot):
    async def setup_hook(self):
        self.add_view(ConfessionPersistentView())
        self.add_view(SuggestionView())
        self.add_view(SuggestionPanelView())


bot = HiraBot(command_prefix="!", intents=discord.Intents.all())


@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} ({bot.user.id})")


@bot.command(name="panel")
@commands.has_permissions(administrator=True)
async def panel(ctx: commands.Context):
    embed = discord.Embed(
        title="💌 Anonymous Confessions",
        description="Click **Submit a confession!** to post anonymously.\nUse **Reply** under a confession to reply anonymously.",
        color=0x57F287
    )
    await ctx.send(embed=embed, view=ConfessionPersistentView())


@bot.command(name="suggestpanel")
@commands.has_permissions(administrator=True)
async def suggestpanel(ctx: commands.Context):
    embed = discord.Embed(
        title="🌈 Server Suggestions",
        description="Click **Submit Suggestion** to send an idea.\n✅ Community can vote • 🛠️ Mods can change status • 📎 Reply with an image to attach",
        color=0xEB459E
    )
    await ctx.send(embed=embed, view=SuggestionPanelView())


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
