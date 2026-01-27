import discord
from discord.ext import commands
from discord import ui
from discord.utils import escape_mentions
from datetime import datetime
import json
import os
import asyncio
import re

# =======================
# CONFIG (EDIT THESE)
# =======================
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN environment variable is not set.")

GUILD_ID = 417323686018940928          # your server ID
CONFESSION_CHANNEL_ID = 1461951872364449984  # where confessions are posted
LOG_CHANNEL_ID = 1461951962965868680         # staff-only logs channel

DATA_FILE = "confessions.json"
# =======================

CONF_ID_RE = re.compile(r"#(\d+)")
data_lock = asyncio.Lock()


# -----------------------
# JSON HELPERS (SAFE)
# -----------------------
def _default_data():
    return {
        "confession_count": 0,
        "confessions": {},              # str(confession_id) -> data
        "message_to_confession": {}     # str(message_id) -> confession_id (int)
    }

def load_data():
    if not os.path.exists(DATA_FILE):
        return _default_data()
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        # ensure keys exist
        for k, v in _default_data().items():
            data.setdefault(k, v)
        return data
    except Exception:
        # if file corrupted, do not crash bot
        return _default_data()

def save_data_atomic(data):
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)
    os.replace(tmp, DATA_FILE)


DATA = load_data()


# -----------------------
# MODALS
# -----------------------
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
            return await interaction.response.send_message(
                "❌ Channels not found. Check channel IDs in config.",
                ephemeral=True
            )

        text = escape_mentions(self.confession.value).strip()
        if not text:
            return await interaction.response.send_message("❌ Empty confession.", ephemeral=True)

        # reserve confession id + save
        async with data_lock:
            DATA["confession_count"] += 1
            cid = int(DATA["confession_count"])

        # PUBLIC EMBED
        confession_embed = discord.Embed(
            title=f"Anonymous Confession (#{cid})",
            description=f"“{text}”",
            color=0x5865F2,
            timestamp=datetime.utcnow()
        )
        confession_embed.set_footer(text="Use the buttons below to submit or reply anonymously.")

        # Send with persistent buttons
        msg = await confession_channel.send(embed=confession_embed, view=ConfessionPersistentView())

        # Save full record
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

        # LOG EMBED (ADVANCED)
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
            return await interaction.response.send_message(
                "❌ Channels not found. Check IDs.",
                ephemeral=True
            )

        text = escape_mentions(self.reply.value).strip()
        if not text:
            return await interaction.response.send_message("❌ Empty reply.", ephemeral=True)

        cid = int(self.confession_id)

        # Save reply to JSON first
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

        # Try to post reply in a thread under the confession (advanced)
        posted_somewhere = False
        try:
            thread_id = rec.get("thread_id")
            thread = None

            if thread_id:
                thread = guild.get_thread(int(thread_id))

            if thread is None:
                # create thread from the confession message
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
            # fallback: send reply in confession channel (no crash)
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

        # Log reply (advanced)
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
            await interaction.response.send_message(
                "✅ Reply saved, but I couldn't post it (missing thread/channel permissions).",
                ephemeral=True
            )


# -----------------------
# PERSISTENT VIEW (NO ERRORS)
# -----------------------
class ConfessionPersistentView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @ui.button(
        label="Submit a confession!",
        emoji="📝",
        style=discord.ButtonStyle.primary,
        custom_id="confession:submit"
    )
    async def submit(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(ConfessionModal())

    @ui.button(
        label="Reply",
        emoji="💬",
        style=discord.ButtonStyle.secondary,
        custom_id="confession:reply"
    )
    async def reply(self, interaction: discord.Interaction, button: ui.Button):
        # Identify confession by message_id mapping (works after restarts)
        message = interaction.message
        if message is None:
            return await interaction.response.send_message("❌ No message context.", ephemeral=True)

        cid = None
        async with data_lock:
            cid = DATA["message_to_confession"].get(str(message.id))

        # fallback: parse embed title
        if cid is None and message.embeds:
            title = message.embeds[0].title or ""
            m = CONF_ID_RE.search(title)
            if m:
                cid = int(m.group(1))

        if cid is None:
            return await interaction.response.send_message(
                "❌ I can't detect which confession this is.",
                ephemeral=True
            )

        await interaction.response.send_modal(ReplyModal(int(cid), message))


# -----------------------
# BOT
# -----------------------
class HiraBot(commands.Bot):
    async def setup_hook(self):
        # Register persistent view correctly (NO ValueError)
        self.add_view(ConfessionPersistentView())

bot = HiraBot(command_prefix="!", intents=discord.Intents.all())


@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} ({bot.user.id})")


# One-time panel message like your screenshot (run !panel in the channel you want)
@bot.command(name="panel")
@commands.has_permissions(administrator=True)
async def panel(ctx: commands.Context):
    embed = discord.Embed(
        title="💌 Anonymous Confessions",
        description="Click **Submit a confession!** to post anonymously.\n"
                    "Use **Reply** under a confession to reply anonymously.",
        color=0x57F287
    )
    await ctx.send(embed=embed, view=ConfessionPersistentView())


# Optional: rebuild mapping if you deleted JSON keys or changed files
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


bot.run(TOKEN)
