import discord
from discord.ext import commands, tasks
from discord.ui import Button, View, Select
import sqlite3
import random
import asyncio
from datetime import datetime, timedelta
import yt_dlp as youtube_dl
from collections import deque
import aiohttp
import json
import os
from dotenv import load_dotenv

# ============================================================
#  🔐 LOAD SECRETS FROM .env
# ============================================================
load_dotenv()
TOKEN        = os.getenv("DISCORD_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
RENDI_API_KEY = os.getenv("RENDI_API_KEY")   # optional
DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID")

if not TOKEN:
    raise ValueError("Missing DISCORD_TOKEN in .env file")

# ============================================================
#  BOT SETUP
# ============================================================
intents = discord.Intents.all()
bot = commands.Bot(command_prefix="R", intents=intents, help_command=None)

OWNER_IDS = [1170738402241548399, 1406682041478676511]
def is_owner(ctx): return ctx.author.id in OWNER_IDS

# ============================================================
#  DATABASE SETUP
# ============================================================
conn = sqlite3.connect("casino.db")
c = conn.cursor()

c.execute("""CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY, balance INTEGER DEFAULT 100,
    daily_last TEXT, bio TEXT DEFAULT 'No bio yet 💫',
    color TEXT DEFAULT 'FFB6C1', rep INTEGER DEFAULT 0,
    xp INTEGER DEFAULT 0, level INTEGER DEFAULT 1,
    work_last TEXT, rob_last TEXT, streak INTEGER DEFAULT 0
)""")
c.execute("""CREATE TABLE IF NOT EXISTS marriages (
    user1 INTEGER, user2 INTEGER, since TEXT, PRIMARY KEY (user1, user2)
)""")
c.execute("""CREATE TABLE IF NOT EXISTS jackpot (
    id INTEGER PRIMARY KEY CHECK (id=1), amount INTEGER DEFAULT 0
)""")
c.execute("INSERT OR IGNORE INTO jackpot VALUES (1,0)")
c.execute("""CREATE TABLE IF NOT EXISTS entries (
    user_id INTEGER PRIMARY KEY, count INTEGER DEFAULT 0
)""")
c.execute("""CREATE TABLE IF NOT EXISTS free_spins (
    user_id INTEGER PRIMARY KEY, last_spin TEXT
)""")
c.execute("""CREATE TABLE IF NOT EXISTS inventory (
    user_id INTEGER, item TEXT, qty INTEGER DEFAULT 1,
    PRIMARY KEY (user_id, item)
)""")
c.execute("""CREATE TABLE IF NOT EXISTS shop (
    item TEXT PRIMARY KEY, price INTEGER, description TEXT, emoji TEXT
)""")
shop_defaults = [
    ("Lucky Charm",  5000,  "Boosts daily reward by 50%",           "🍀"),
    ("VIP Pass",    20000,  "Unlock VIP profile badge",              "💎"),
    ("Shield",       8000,  "Protect coins from being robbed once",  "🛡️"),
    ("Bomb",        12000,  "Rob 2× more next time",                 "💣"),
    ("Lotto Boost",  3000,  "Buy 3 lottery tickets for price of 1",  "🎟️"),
    ("XP Boost",    10000,  "Double XP gain for 1 hour",             "⚡"),
]
for item, price, desc, emoji in shop_defaults:
    c.execute("INSERT OR IGNORE INTO shop VALUES (?,?,?,?)", (item, price, desc, emoji))

c.execute("""CREATE TABLE IF NOT EXISTS warns (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
    guild_id INTEGER, reason TEXT, timestamp TEXT
)""")
c.execute("""CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
    channel_id INTEGER, message TEXT, remind_at TEXT
)""")
c.execute("""CREATE TABLE IF NOT EXISTS giveaways (
    msg_id INTEGER PRIMARY KEY, channel_id INTEGER, guild_id INTEGER,
    prize TEXT, winners INTEGER DEFAULT 1, ends_at TEXT,
    host_id INTEGER, ended INTEGER DEFAULT 0
)""")
c.execute("""CREATE TABLE IF NOT EXISTS afk (
    user_id INTEGER PRIMARY KEY, reason TEXT, since TEXT
)""")
conn.commit()

# Trading tables
c.execute("""CREATE TABLE IF NOT EXISTS trading_portfolio (
    user_id INTEGER,
    stock_symbol TEXT,
    shares INTEGER DEFAULT 0,
    avg_price INTEGER DEFAULT 0,
    PRIMARY KEY (user_id, stock_symbol)
)""")
c.execute("""CREATE TABLE IF NOT EXISTS trading_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    stock_symbol TEXT,
    action TEXT,
    shares INTEGER,
    price INTEGER,
    timestamp TEXT
)""")
conn.commit()

# Migration helper
def _col_exists(table, col):
    c.execute(f"PRAGMA table_info({table})")
    return any(r[1] == col for r in c.fetchall())

for col, dflt in [("xp","0"),("level","1"),("work_last","NULL"),
                  ("rob_last","NULL"),("streak","0"),
                  ("bio","'No bio yet 💫'"),("color","'FFB6C1'"),
                  ("rep","0"),("daily_last","NULL")]:
    if not _col_exists("users", col):
        c.execute(f"ALTER TABLE users ADD COLUMN {col} {'INTEGER' if dflt.isdigit() else 'TEXT'} DEFAULT {dflt}")
conn.commit()

# ============================================================
#  HELPER FUNCTIONS
# ============================================================
def get_user(uid):
    c.execute("SELECT * FROM users WHERE user_id=?", (uid,))
    return c.fetchone()

def create_user(uid):
    try:
        c.execute("INSERT INTO users (user_id) VALUES (?)", (uid,))
        conn.commit()
    except sqlite3.IntegrityError:
        pass

def get_balance(uid):
    c.execute("SELECT balance FROM users WHERE user_id=?", (uid,))
    r = c.fetchone(); return r[0] if r else 0

def update_balance(uid, amt):
    c.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (amt, uid))
    conn.commit()

def get_jackpot():
    c.execute("SELECT amount FROM jackpot WHERE id=1"); return c.fetchone()[0]

def add_to_jackpot(a):
    c.execute("UPDATE jackpot SET amount=amount+? WHERE id=1", (a,)); conn.commit()

def reset_jackpot():
    c.execute("UPDATE jackpot SET amount=0 WHERE id=1"); conn.commit()

def reset_all_entries():
    c.execute("DELETE FROM entries"); conn.commit()

def update_entries(uid, d):
    c.execute("INSERT INTO entries VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET count=count+?",
              (uid, d, d)); conn.commit()

def get_top_balances(n=10):
    c.execute("SELECT user_id, balance FROM users ORDER BY balance DESC LIMIT ?", (n,))
    return c.fetchall()

def parse_amount(s):
    s = s.lower().strip()
    mul = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}
    if s == "all":
        return None
    if s[-1] in mul:
        try: return int(float(s[:-1]) * mul[s[-1]])
        except: return None
    try: return int(float(s))
    except: return None

def parse_bet_amount(s, user_id):
    if s.lower() == "all":
        return get_balance(user_id)
    return parse_amount(s)

def can_daily(uid):
    c.execute("SELECT daily_last FROM users WHERE user_id=?", (uid,))
    r = c.fetchone()
    if not r or not r[0]: return True
    try: return datetime.now() - datetime.fromisoformat(r[0]) >= timedelta(days=1)
    except: return True

def time_until_daily(uid):
    c.execute("SELECT daily_last FROM users WHERE user_id=?", (uid,))
    r = c.fetchone()
    if not r or not r[0]: return None
    try:
        next_time = datetime.fromisoformat(r[0]) + timedelta(days=1)
        remaining = next_time - datetime.now()
        h, m = divmod(int(remaining.total_seconds()) // 60, 60)
        return f"{h}h {m}m"
    except: return None

def update_daily(uid):
    c.execute("UPDATE users SET daily_last=? WHERE user_id=?", (datetime.now().isoformat(), uid))
    conn.commit()

def can_work(uid):
    c.execute("SELECT work_last FROM users WHERE user_id=?", (uid,))
    r = c.fetchone()
    if not r or not r[0]: return True
    try: return datetime.now() - datetime.fromisoformat(r[0]) >= timedelta(minutes=30)
    except: return True

def time_until_work(uid):
    c.execute("SELECT work_last FROM users WHERE user_id=?", (uid,))
    r = c.fetchone()
    if not r or not r[0]: return None
    try:
        next_time = datetime.fromisoformat(r[0]) + timedelta(minutes=30)
        remaining = next_time - datetime.now()
        m = max(0, int(remaining.total_seconds()) // 60)
        return f"{m}m"
    except: return None

def update_work(uid):
    c.execute("UPDATE users SET work_last=? WHERE user_id=?", (datetime.now().isoformat(), uid))
    conn.commit()

def can_rob(uid):
    c.execute("SELECT rob_last FROM users WHERE user_id=?", (uid,))
    r = c.fetchone()
    if not r or not r[0]: return True
    try: return datetime.now() - datetime.fromisoformat(r[0]) >= timedelta(minutes=45)
    except: return True

def update_rob(uid):
    c.execute("UPDATE users SET rob_last=? WHERE user_id=?", (datetime.now().isoformat(), uid))
    conn.commit()

def can_spin(uid):
    c.execute("SELECT last_spin FROM free_spins WHERE user_id=?", (uid,))
    r = c.fetchone()
    if not r or not r[0]: return True
    try: return datetime.now() - datetime.fromisoformat(r[0]) >= timedelta(minutes=60)
    except: return True

def update_spin(uid):
    c.execute("INSERT OR REPLACE INTO free_spins VALUES(?,?)", (uid, datetime.now().isoformat()))
    conn.commit()

def add_xp(uid, amount):
    c.execute("UPDATE users SET xp=xp+? WHERE user_id=?", (amount, uid))
    conn.commit()
    c.execute("SELECT xp, level FROM users WHERE user_id=?", (uid,))
    xp, lv = c.fetchone()
    needed = lv * 100
    if xp >= needed:
        c.execute("UPDATE users SET level=level+1, xp=xp-? WHERE user_id=?", (needed, uid))
        conn.commit()
        return lv + 1
    return None

def has_item(uid, item):
    c.execute("SELECT qty FROM inventory WHERE user_id=? AND item=?", (uid, item))
    r = c.fetchone(); return r and r[0] > 0

def use_item(uid, item):
    c.execute("UPDATE inventory SET qty=qty-1 WHERE user_id=? AND item=?", (uid, item))
    c.execute("DELETE FROM inventory WHERE user_id=? AND item=? AND qty<=0", (uid, item))
    conn.commit()

def give_item(uid, item):
    c.execute("INSERT INTO inventory VALUES(?,?,1) ON CONFLICT(user_id,item) DO UPDATE SET qty=qty+1",
              (uid, item)); conn.commit()

def get_generated_avatar(uid):
    return f"https://api.dicebear.com/7.x/adventurer/svg?seed={uid}"

def xp_bar(xp, needed, length=12):
    filled = int((xp / needed) * length) if needed else 0
    return "█" * min(filled, length) + "░" * (length - min(filled, length))

def coin_bar(balance, length=10):
    filled = min(int((balance / 1_000_000) * length), length)
    return "▰" * filled + "▱" * (length - filled)

RANK_TIERS = [
    (50, "👑 Legend",   0xFFD700),
    (30, "💎 Diamond",  0x00BFFF),
    (20, "🏅 Gold",     0xFFA500),
    (10, "🥈 Silver",   0xC0C0C0),
    (5,  "🥉 Bronze",   0xCD7F32),
    (0,  "🌱 Newcomer", 0x7CFC00),
]

def get_rank_tier(level):
    for min_lv, label, color in RANK_TIERS:
        if level >= min_lv:
            return label, color
    return "🌱 Newcomer", 0x7CFC00

def get_badges(uid):
    badges = []
    c.execute("SELECT item FROM inventory WHERE user_id=? AND qty>0", (uid,))
    items = {row[0] for row in c.fetchall()}
    badge_map = [
        ("VIP Pass",    "💎 VIP"),
        ("Lucky Charm", "🍀 Lucky"),
        ("Shield",      "🛡️ Shielded"),
        ("Bomb",        "💣 Armed"),
        ("XP Boost",    "⚡ Boosted"),
        ("Lotto Boost", "🎟️ Lotto"),
    ]
    for item, badge in badge_map:
        if item in items: badges.append(badge)
    return badges

FLAVOR_LINES = [
    "Still grinding. Respect. 💪",
    "Bags secured. 💼",
    "The casino fears this one. 🎰",
    "Running the economy. 💸",
    "Vibes immaculate, wallet fuller. ✨",
    "Built different. 🏗️",
    "Quietly becoming the richest. 🤫",
    "On their third comeback arc. 🔄",
    "The leaderboard knows the name. 🏆",
    "Luck? No, just skill. 🎯",
]

# ============================================================
#  RAA AI (using GROQ)
# ============================================================
async def ask_raa_ai(prompt, system_message=None):
    if not GROQ_API_KEY:
        return "❌ RAA AI is not configured."
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    msgs = []
    if system_message: msgs.append({"role": "system", "content": system_message})
    msgs.append({"role": "user", "content": prompt})
    payload = {
        "model": "llama-3.3-70b-versatile",
        "messages": msgs,
        "temperature": 0.7,
        "max_tokens": 700,
    }
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post("https://api.groq.com/openai/v1/chat/completions",
                              headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status == 200:
                    d = await r.json()
                    return d["choices"][0]["message"]["content"].strip()
                return f"❌ API error {r.status}. Try again later."
    except asyncio.TimeoutError:
        return "❌ RAA AI timed out. Try again."
    except Exception as e:
        return f"❌ Request failed: {e}"

# ============================================================
#  MUSIC (yt-dlp + FFmpeg)
# ============================================================
queues        = {}
current_songs = {}

ytdl_opts = {
    'format': 'bestaudio/best',
    'restrictfilenames': True,
    'noplaylist': True,
    'nocheckcertificate': True,
    'ignoreerrors': False,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'ytsearch',
    'source_address': '0.0.0.0',
    'cookiefile': None,
    'extractor_retries': 3,
    'socket_timeout': 10,
}

ffmpeg_opts = {
    'options': '-vn -bufsize 64k',
    'before_options': (
        '-reconnect 1 '
        '-reconnect_streamed 1 '
        '-reconnect_delay_max 5 '
        '-nostdin '
        '-loglevel warning'
    ),
}

ytdl = youtube_dl.YoutubeDL(ytdl_opts)

class YTDLSource(discord.PCMVolumeTransformer):
    def __init__(self, source, *, data, volume=0.5):
        super().__init__(source, volume)
        self.data        = data
        self.title       = data.get('title', 'Unknown')
        self.url         = data.get('url', '')
        self.duration    = data.get('duration', 0)
        self.thumbnail   = data.get('thumbnail', '')
        self.webpage_url = data.get('webpage_url', '')

    @classmethod
    async def from_url(cls, url, *, loop=None, stream=True):
        loop = loop or asyncio.get_event_loop()
        try:
            data = await loop.run_in_executor(
                None,
                lambda: ytdl.extract_info(url, download=False)
            )
        except Exception as e:
            raise Exception(f"yt-dlp error: {e}")

        if data is None:
            raise Exception("Could not retrieve audio data.")

        if 'entries' in data:
            data = data['entries'][0]

        stream_url = data.get('url')
        if not stream_url:
            raise Exception("No stream URL found.")

        source = discord.FFmpegPCMAudio(stream_url, **ffmpeg_opts)
        return cls(source, data=data)

async def play_next(ctx, guild_id):
    if guild_id in queues and queues[guild_id]:
        nxt = queues[guild_id].popleft()
        current_songs[guild_id] = nxt
        vc = discord.utils.get(bot.voice_clients, guild=ctx.guild)
        if vc:
            def after_play(e):
                if e:
                    print(f"Player error: {e}")
                bot.loop.create_task(play_next(ctx, guild_id))
            vc.play(nxt, after=after_play)
            embed = discord.Embed(
                title="🎶 Now Playing",
                description=f"**{nxt.title}**",
                color=discord.Color.blue())
            if nxt.thumbnail:
                embed.set_thumbnail(url=nxt.thumbnail)
            await ctx.send(embed=embed)
    else:
        current_songs.pop(guild_id, None)

# ============================================================
#  MUSIC COMMANDS
# ============================================================
@bot.command(name="play", aliases=["pl", "ply"])
async def play_cmd(ctx, *, query: str):
    """Play music from YouTube. Usage: Rplay <song name or URL>"""
    if not ctx.author.voice:
        await ctx.send("❌ Join a voice channel first!")
        return

    vc = discord.utils.get(bot.voice_clients, guild=ctx.guild)
    if not vc:
        try:
            vc = await ctx.author.voice.channel.connect()
        except Exception as e:
            await ctx.send(f"❌ Could not join voice channel: `{e}`")
            return
    elif vc.channel != ctx.author.voice.channel:
        await vc.move_to(ctx.author.voice.channel)

    loading = await ctx.send("🔍 **Searching...**")

    try:
        player = await YTDLSource.from_url(query, loop=bot.loop, stream=True)
    except Exception as e:
        await loading.edit(content=f"❌ Error: `{e}`")
        return

    gid = ctx.guild.id
    queues.setdefault(gid, deque())

    if vc.is_playing() or vc.is_paused():
        queues[gid].append(player)
        embed = discord.Embed(
            title="⏩ Added to Queue",
            description=f"**{player.title}**",
            color=discord.Color.blue())
        embed.add_field(name="Position", value=f"#{len(queues[gid])}", inline=True)
        if player.thumbnail:
            embed.set_thumbnail(url=player.thumbnail)
        await loading.edit(content=None, embed=embed)
    else:
        current_songs[gid] = player

        def after_play(e):
            if e:
                print(f"Player error: {e}")
            bot.loop.create_task(play_next(ctx, gid))

        vc.play(player, after=after_play)
        embed = discord.Embed(
            title="🎶 Now Playing",
            description=f"**{player.title}**",
            color=discord.Color.blue())
        if player.thumbnail:
            embed.set_thumbnail(url=player.thumbnail)
        if player.duration:
            mins, secs = divmod(player.duration, 60)
            embed.add_field(name="⏱️ Duration", value=f"{mins}:{secs:02d}", inline=True)
        if player.webpage_url:
            embed.add_field(name="🔗 Link", value=f"[YouTube]({player.webpage_url})", inline=True)
        await loading.edit(content=None, embed=embed)

@bot.command(name="pause")
async def pause_cmd(ctx):
    vc = discord.utils.get(bot.voice_clients, guild=ctx.guild)
    if vc and vc.is_playing():
        vc.pause()
        await ctx.send("⏸️ Paused.")
    else:
        await ctx.send("❌ Nothing is playing.")

@bot.command(name="resume")
async def resume_cmd(ctx):
    vc = discord.utils.get(bot.voice_clients, guild=ctx.guild)
    if vc and vc.is_paused():
        vc.resume()
        await ctx.send("▶️ Resumed.")
    else:
        await ctx.send("❌ Not paused.")

@bot.command(name="volume", aliases=["vol"])
async def volume_cmd(ctx, vol: int):
    if not 1 <= vol <= 100:
        await ctx.send("❌ Volume must be between 1 and 100.")
        return
    vc = discord.utils.get(bot.voice_clients, guild=ctx.guild)
    if vc and isinstance(vc.source, discord.PCMVolumeTransformer):
        vc.source.volume = vol / 100
        bar = "🔊" * (vol // 20) + "🔈" * (5 - vol // 20)
        await ctx.send(f"{bar} Volume set to **{vol}%**")
    else:
        await ctx.send("❌ Nothing playing.")

@bot.command(name="stop")
async def stop_cmd(ctx):
    gid = ctx.guild.id
    vc = discord.utils.get(bot.voice_clients, guild=ctx.guild)
    if vc and (vc.is_playing() or vc.is_paused()):
        vc.stop()
        queues[gid] = deque()
        current_songs.pop(gid, None)
        await ctx.send("⏹️ Stopped and queue cleared.")
    else:
        await ctx.send("❌ Nothing playing.")

@bot.command(name="skip")
async def skip_cmd(ctx):
    vc = discord.utils.get(bot.voice_clients, guild=ctx.guild)
    if vc and vc.is_playing():
        vc.stop()
        await ctx.send("⏭️ Skipped!")
    else:
        await ctx.send("❌ Nothing to skip.")

@bot.command(name="queue", aliases=["q"])
async def queue_cmd(ctx):
    gid = ctx.guild.id
    current = current_songs.get(gid)
    q = queues.get(gid, deque())
    if not current and not q:
        await ctx.send("📭 Nothing in queue.")
        return
    embed = discord.Embed(title="🎵 Music Queue", color=discord.Color.blue())
    if current:
        embed.add_field(name="▶️ Now Playing", value=f"**{current.title}**", inline=False)
    if q:
        up_next = "\n".join(f"`{i}.` {s.title}" for i, s in enumerate(list(q)[:10], 1))
        embed.add_field(name="⏩ Up Next", value=up_next, inline=False)
        if len(q) > 10:
            embed.set_footer(text=f"+{len(q)-10} more in queue")
    await ctx.send(embed=embed)

@bot.command(name="nowplaying", aliases=["np"])
async def nowplaying_cmd(ctx):
    gid = ctx.guild.id
    if gid in current_songs:
        s = current_songs[gid]
        embed = discord.Embed(title="🎶 Now Playing", description=f"**{s.title}**",
                              color=discord.Color.blue())
        if s.thumbnail:
            embed.set_thumbnail(url=s.thumbnail)
        if s.webpage_url:
            embed.add_field(name="Link", value=f"[YouTube]({s.webpage_url})")
        await ctx.send(embed=embed)
    else:
        await ctx.send("❌ Nothing is playing.")

@bot.command(name="leave")
async def leave_cmd(ctx):
    vc = discord.utils.get(bot.voice_clients, guild=ctx.guild)
    if vc:
        gid = ctx.guild.id
        await vc.disconnect()
        queues.pop(gid, None)
        current_songs.pop(gid, None)
        await ctx.send("👋 Left the voice channel.")
    else:
        await ctx.send("❌ Not in a voice channel.")

# ============================================================
#  UI COMPONENTS (Buttons, Views, etc.)
# ============================================================
class CreateAccountButton(Button):
    def __init__(self):
        super().__init__(label="✨ Create Account", style=discord.ButtonStyle.green)

    async def callback(self, interaction: discord.Interaction):
        uid = interaction.user.id
        if get_user(uid):
            await interaction.response.send_message("⚠️ You already have an account!", ephemeral=True)
            return
        create_user(uid)
        embed = discord.Embed(
            title="✨ Account Created!",
            description=(
                f"{interaction.user.mention}, welcome to BOBxRAA Casino!\n"
                f"You start with **100 coins**. Use `Rdaily` every day for bonuses. 🎁"
            ),
            color=discord.Color.green(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

class CreateAccountView(View):
    def __init__(self): super().__init__(timeout=None); self.add_item(CreateAccountButton())

class ConfirmView(View):
    def __init__(self, author_id: int, timeout: float = 30):
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.value     = None

    @discord.ui.button(label="✅ Confirm", style=discord.ButtonStyle.green)
    async def confirm(self, interaction: discord.Interaction, button: Button):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ This isn't your button!", ephemeral=True)
            return
        self.value = True
        self.stop()
        await interaction.response.defer()

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.red)
    async def cancel(self, interaction: discord.Interaction, button: Button):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ This isn't your button!", ephemeral=True)
            return
        self.value = False
        self.stop()
        await interaction.response.defer()

class BlackjackView(View):
    def __init__(self, author_id: int):
        super().__init__(timeout=30)
        self.author_id = author_id
        self.action    = None

    @discord.ui.button(label="👊 Hit", style=discord.ButtonStyle.green)
    async def hit(self, interaction: discord.Interaction, button: Button):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ Not your game!", ephemeral=True); return
        self.action = "hit"; self.stop(); await interaction.response.defer()

    @discord.ui.button(label="🛑 Stand", style=discord.ButtonStyle.red)
    async def stand(self, interaction: discord.Interaction, button: Button):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ Not your game!", ephemeral=True); return
        self.action = "stand"; self.stop(); await interaction.response.defer()

    @discord.ui.button(label="💰 Double Down", style=discord.ButtonStyle.blurple)
    async def double(self, interaction: discord.Interaction, button: Button):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ Not your game!", ephemeral=True); return
        self.action = "double"; self.stop(); await interaction.response.defer()

class TriviaView(View):
    def __init__(self, author_id: int, answer_letter: str):
        super().__init__(timeout=20)
        self.author_id     = author_id
        self.answer_letter = answer_letter
        self.chosen        = None

    async def _handle(self, interaction: discord.Interaction, letter: str):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ Not your trivia!", ephemeral=True); return
        self.chosen = letter; self.stop()
        await interaction.response.defer()

    @discord.ui.button(label="A", style=discord.ButtonStyle.blurple)
    async def btn_a(self, i, b): await self._handle(i, "A")
    @discord.ui.button(label="B", style=discord.ButtonStyle.blurple)
    async def btn_b(self, i, b): await self._handle(i, "B")
    @discord.ui.button(label="C", style=discord.ButtonStyle.blurple)
    async def btn_c(self, i, b): await self._handle(i, "C")
    @discord.ui.button(label="D", style=discord.ButtonStyle.blurple)
    async def btn_d(self, i, b): await self._handle(i, "D")

class HeistJoinView(View):
    def __init__(self, host_id: int, bet: int):
        super().__init__(timeout=60)
        self.host_id      = host_id
        self.bet          = bet
        self.participants = {host_id}

    @discord.ui.button(label="🏦 Join Heist", style=discord.ButtonStyle.green)
    async def join(self, interaction: discord.Interaction, button: Button):
        uid = interaction.user.id
        if uid in self.participants:
            await interaction.response.send_message("⚠️ Already joined!", ephemeral=True); return
        if not get_user(uid) or get_balance(uid) < self.bet:
            await interaction.response.send_message(
                f"❌ You need **{self.bet:,}** coins to join!", ephemeral=True); return
        update_balance(uid, -self.bet)
        self.participants.add(uid)
        await interaction.response.send_message(
            f"✅ {interaction.user.mention} joined! ({len(self.participants)} members)", ephemeral=False)

class PollView(View):
    def __init__(self, options: list[str]):
        super().__init__(timeout=None)
        self.votes   = {i: set() for i in range(len(options))}
        self.options = options
        for i, opt in enumerate(options):
            btn = Button(label=f"{['1️⃣','2️⃣','3️⃣','4️⃣','5️⃣','6️⃣','7️⃣','8️⃣','9️⃣'][i]} {opt[:40]}",
                         style=discord.ButtonStyle.blurple, custom_id=f"poll_{i}")
            btn.callback = self._make_callback(i)
            self.add_item(btn)

    def _make_callback(self, idx: int):
        async def callback(interaction: discord.Interaction):
            uid = interaction.user.id
            for i, voters in self.votes.items():
                voters.discard(uid)
            self.votes[idx].add(uid)
            total = sum(len(v) for v in self.votes.values())
            lines = []
            for i, opt in enumerate(self.options):
                count = len(self.votes[i])
                pct   = int((count / total) * 100) if total else 0
                bar   = "█" * (pct // 10) + "░" * (10 - pct // 10)
                lines.append(f"**{opt}** — {count} vote{'s' if count!=1 else ''}\n`{bar}` {pct}%")
            embed = discord.Embed(title="📊 Live Poll Results",
                                  description="\n\n".join(lines), color=discord.Color.blue())
            embed.set_footer(text=f"Total votes: {total}")
            await interaction.response.edit_message(embed=embed, view=self)
        return callback

class GiveawayView(View):
    def __init__(self, msg_id: int):
        super().__init__(timeout=None)
        self.msg_id   = msg_id
        self.entrants = set()

    @discord.ui.button(label="🎉 Enter Giveaway", style=discord.ButtonStyle.green, custom_id="giveaway_enter")
    async def enter(self, interaction: discord.Interaction, button: Button):
        uid = interaction.user.id
        if uid in self.entrants:
            await interaction.response.send_message("✅ Already entered!", ephemeral=True); return
        self.entrants.add(uid)
        await interaction.response.send_message(
            f"🎉 You're in! **{len(self.entrants)}** total entries.", ephemeral=True)

class ShopSelect(Select):
    def __init__(self, items):
        options = [
            discord.SelectOption(label=f"{emoji} {item}", description=f"{price:,} coins — {desc[:50]}",
                                 value=item)
            for item, price, desc, emoji in items
        ]
        super().__init__(placeholder="Browse items…", options=options[:25])
        self.items_data = {item: (price, desc, emoji) for item, price, desc, emoji in items}

    async def callback(self, interaction: discord.Interaction):
        item   = self.values[0]
        price, desc, emoji = self.items_data[item]
        uid    = interaction.user.id
        if not get_user(uid):
            await interaction.response.send_message("❌ Use `Rstart` first!", ephemeral=True); return
        bal = get_balance(uid)
        can_afford = bal >= price
        embed = discord.Embed(
            title=f"{emoji} {item}",
            description=f"{desc}\n\n**Price:** {price:,} coins\n**Your balance:** {bal:,} coins",
            color=discord.Color.green() if can_afford else discord.Color.red(),
        )
        view = BuyConfirmView(uid, item, price, emoji) if can_afford else None
        if not can_afford:
            embed.set_footer(text=f"❌ You need {price - bal:,} more coins.")
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

class BuyConfirmView(View):
    def __init__(self, uid, item, price, emoji):
        super().__init__(timeout=30)
        self.uid = uid; self.item = item; self.price = price; self.emoji = emoji

    @discord.ui.button(label="Buy Now", style=discord.ButtonStyle.green)
    async def buy(self, interaction: discord.Interaction, button: Button):
        if interaction.user.id != self.uid:
            await interaction.response.send_message("❌ Not your shop!", ephemeral=True); return
        if get_balance(self.uid) < self.price:
            await interaction.response.send_message("❌ Not enough coins!", ephemeral=True); return
        update_balance(self.uid, -self.price)
        give_item(self.uid, self.item)
        self.stop()
        await interaction.response.edit_message(
            content=f"{self.emoji} Purchased **{self.item}** for **{self.price:,}** coins! ✅",
            embed=None, view=None)

class ShopView(View):
    def __init__(self, items):
        super().__init__(timeout=60)
        self.add_item(ShopSelect(items))

# ============================================================
#  STOCK MARKET CLASS
# ============================================================
class StockMarket:
    def __init__(self):
        self.stocks = {
            "BOBAI": {"name": "🤖 BOBAI Tech", "price": 100, "volatility": 0.15, "trend": 0, "sector": "Technology", "icon": "🤖"},
            "RAACOIN": {"name": "🪙 RAA Coin", "price": 50, "volatility": 0.25, "trend": 0, "sector": "Crypto", "icon": "🪙"},
            "CASINO": {"name": "🎰 Casino Corp", "price": 200, "volatility": 0.08, "trend": 0, "sector": "Gambling", "icon": "🎰"},
            "ENERGY": {"name": "⚡ Power Corp", "price": 75, "volatility": 0.12, "trend": 0, "sector": "Energy", "icon": "⚡"},
            "GOLD": {"name": "⛏️ Gold Mining", "price": 150, "volatility": 0.18, "trend": 0, "sector": "Mining", "icon": "⛏️"},
            "BANK": {"name": "🏦 Royal Bank", "price": 300, "volatility": 0.06, "trend": 0, "sector": "Finance", "icon": "🏦"},
        }
        
    def update_prices(self):
        changes = []
        for symbol in self.stocks:
            stock = self.stocks[symbol]
            change = random.uniform(-stock["volatility"], stock["volatility"])
            stock["trend"] = stock["trend"] * 0.7 + random.uniform(-0.05, 0.05)
            change += stock["trend"]
            old_price = stock["price"]
            new_price = stock["price"] * (1 + change)
            stock["price"] = max(5, round(new_price))
            changes.append((symbol, old_price, stock["price"]))
        return changes
    
    def get_stock(self, symbol):
        return self.stocks.get(symbol)
    
    def get_portfolio_value(self, user_id):
        c.execute("SELECT stock_symbol, shares FROM trading_portfolio WHERE user_id=? AND shares>0", (user_id,))
        portfolio = c.fetchall()
        total = 0
        for symbol, shares in portfolio:
            if symbol in self.stocks:
                total += shares * self.stocks[symbol]["price"]
        return total

market = StockMarket()

# ============================================================
#  TRADING GUI - MODAL
# ============================================================
class TradeModal(discord.ui.Modal):
    def __init__(self, user_id, symbol, action):
        super().__init__(title=f"{action.upper()} {symbol}")
        self.user_id = user_id
        self.symbol = symbol
        self.action = action
        
        self.shares = discord.ui.TextInput(
            label=f"Number of shares to {action}",
            placeholder="Enter number or 'all'",
            required=True,
            style=discord.TextStyle.short
        )
        self.add_item(self.shares)
    
    async def on_submit(self, interaction: discord.Interaction):
        shares_input = self.shares.value.lower()
        stock = market.get_stock(self.symbol)
        
        if not stock:
            await interaction.response.send_message("❌ Stock not found!", ephemeral=True)
            return
        
        price = stock["price"]
        
        if shares_input == "all":
            if self.action == "buy":
                shares = get_balance(self.user_id) // price
            else:
                c.execute("SELECT shares FROM trading_portfolio WHERE user_id=? AND stock_symbol=?", (self.user_id, self.symbol))
                result = c.fetchone()
                shares = result[0] if result else 0
        else:
            try:
                shares = int(shares_input)
            except ValueError:
                await interaction.response.send_message("❌ Invalid number!", ephemeral=True)
                return
        
        if shares <= 0:
            await interaction.response.send_message("❌ Must buy/sell at least 1 share!", ephemeral=True)
            return
        
        cost = shares * price
        
        if self.action == "buy":
            if get_balance(self.user_id) < cost:
                await interaction.response.send_message(f"❌ Need {cost:,} coins!", ephemeral=True)
                return
            
            update_balance(self.user_id, -cost)
            
            c.execute("""INSERT INTO trading_portfolio (user_id, stock_symbol, shares, avg_price) 
                        VALUES (?, ?, ?, ?) 
                        ON CONFLICT(user_id, stock_symbol) 
                        DO UPDATE SET shares = shares + ?, avg_price = (avg_price * shares + ?) / (shares + ?)""",
                      (self.user_id, self.symbol, shares, price, shares, cost, shares))
            conn.commit()
            
            c.execute("INSERT INTO trading_history (user_id, stock_symbol, action, shares, price, timestamp) VALUES (?,?,?,?,?,?)",
                      (self.user_id, self.symbol, "BUY", shares, price, datetime.now().isoformat()))
            conn.commit()
            
            embed = discord.Embed(title=f"✅ Bought {shares} shares of {self.symbol}!", 
                                  description=f"Cost: {cost:,} coins @ ${price}/share", color=discord.Color.green())
            
        else:
            c.execute("SELECT shares, avg_price FROM trading_portfolio WHERE user_id=? AND stock_symbol=?", (self.user_id, self.symbol))
            result = c.fetchone()
            
            if not result or result[0] < shares:
                await interaction.response.send_message(f"❌ You only own {result[0] if result else 0} shares!", ephemeral=True)
                return
            
            avg_price = result[1]
            proceeds = shares * price
            profit = proceeds - (shares * avg_price)
            
            update_balance(self.user_id, proceeds)
            
            new_shares = result[0] - shares
            if new_shares > 0:
                c.execute("UPDATE trading_portfolio SET shares=? WHERE user_id=? AND stock_symbol=?", (new_shares, self.user_id, self.symbol))
            else:
                c.execute("DELETE FROM trading_portfolio WHERE user_id=? AND stock_symbol=?", (self.user_id, self.symbol))
            conn.commit()
            
            c.execute("INSERT INTO trading_history (user_id, stock_symbol, action, shares, price, timestamp) VALUES (?,?,?,?,?,?)",
                      (self.user_id, self.symbol, "SELL", shares, price, datetime.now().isoformat()))
            conn.commit()
            
            profit_emoji = "🟢" if profit > 0 else "🔴"
            embed = discord.Embed(title=f"✅ Sold {shares} shares of {self.symbol}!",
                                  description=f"Proceeds: {proceeds:,} coins\n{profit_emoji} P/L: {profit:+,}", 
                                  color=discord.Color.green() if profit > 0 else discord.Color.red())
        
        embed.add_field(name="New Balance", value=f"{get_balance(self.user_id):,} coins")
        await interaction.response.send_message(embed=embed, ephemeral=False)

class TradingStockView(View):
    def __init__(self, user_id, symbol):
        super().__init__(timeout=60)
        self.user_id = user_id
        self.symbol = symbol
        
        buy = Button(label="🟢 BUY", style=discord.ButtonStyle.green)
        buy.callback = self.buy_callback
        self.add_item(buy)
        
        sell = Button(label="🔴 SELL", style=discord.ButtonStyle.red)
        sell.callback = self.sell_callback
        self.add_item(sell)
        
        back = Button(label="◀️ Back", style=discord.ButtonStyle.grey)
        back.callback = self.back_callback
        self.add_item(back)
    
    async def buy_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ Not yours!", ephemeral=True)
            return
        await interaction.response.send_modal(TradeModal(self.user_id, self.symbol, "buy"))
    
    async def sell_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ Not yours!", ephemeral=True)
            return
        await interaction.response.send_modal(TradeModal(self.user_id, self.symbol, "sell"))
    
    async def back_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ Not yours!", ephemeral=True)
            return
        view = TradingPortfolioView(self.user_id, interaction.user.display_name)
        embed = await view.get_portfolio_embed()
        await interaction.response.edit_message(embed=embed, view=view)

class TradingPortfolioView(View):
    def __init__(self, user_id, user_name):
        super().__init__(timeout=60)
        self.user_id = user_id
        self.user_name = user_name
        
        options = []
        for symbol, data in market.stocks.items():
            options.append(discord.SelectOption(
                label=f"{data['icon']} {symbol} - {data['name']}",
                description=f"${data['price']:,} | {data['sector']}",
                value=symbol
            ))
        
        select = Select(placeholder="📊 Select a stock...", options=options)
        select.callback = self.stock_select_callback
        self.add_item(select)
        
        refresh = Button(label="🔄 Refresh", style=discord.ButtonStyle.green)
        refresh.callback = self.refresh_callback
        self.add_item(refresh)
    
    async def stock_select_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ Not yours!", ephemeral=True)
            return
        symbol = interaction.data["values"][0]
        stock = market.get_stock(symbol)
        embed = discord.Embed(title=f"📈 {symbol} - {stock['name']}", color=discord.Color.blue())
        embed.add_field(name="Price", value=f"${stock['price']:,}", inline=True)
        embed.add_field(name="Sector", value=stock['sector'], inline=True)
        embed.add_field(name="Volatility", value=f"{stock['volatility']*100:.0f}%", inline=True)
        embed.set_footer(text="Use BUY/SELL buttons below")
        view = TradingStockView(self.user_id, symbol)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=False)
    
    async def refresh_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ Not yours!", ephemeral=True)
            return
        embed = await self.get_portfolio_embed()
        await interaction.response.edit_message(embed=embed, view=self)
    
    async def get_portfolio_embed(self):
        c.execute("SELECT stock_symbol, shares, avg_price FROM trading_portfolio WHERE user_id=? AND shares>0", (self.user_id,))
        portfolio = c.fetchall()
        
        total_value = market.get_portfolio_value(self.user_id)
        cash = get_balance(self.user_id)
        net_worth = cash + total_value
        
        embed = discord.Embed(title=f"📈 {self.user_name}'s Portfolio", color=discord.Color.gold())
        embed.add_field(name="💰 Cash", value=f"{cash:,}", inline=True)
        embed.add_field(name="📊 Stocks", value=f"{total_value:,}", inline=True)
        embed.add_field(name="💎 Net Worth", value=f"{net_worth:,}", inline=True)
        
        if portfolio:
            text = ""
            for symbol, shares, avg_price in portfolio:
                if symbol in market.stocks:
                    current = market.stocks[symbol]["price"]
                    pl = (current - avg_price) * shares
                    pl_emoji = "🟢" if pl > 0 else "🔴"
                    text += f"**{symbol}** ×{shares} @ ${avg_price}\n  → ${current} {pl_emoji} {pl:+,}\n"
            embed.add_field(name="📋 Holdings", value=text, inline=False)
        else:
            embed.add_field(name="📋 Holdings", value="*No stocks owned*", inline=False)
        
        return embed

# ============================================================
#  TRADING COMMANDS
# ============================================================
@bot.command(name="market")
async def market_cmd(ctx):
    """View stock prices"""
    embed = discord.Embed(title="📈 Stock Market", color=discord.Color.blue())
    for symbol, data in market.stocks.items():
        embed.add_field(name=f"{data['icon']} {symbol}", value=f"💰 ${data['price']:,}\n📊 {data['sector']}", inline=True)
    await ctx.send(embed=embed)

@bot.command(name="trade")
async def trade_cmd(ctx):
    """Open trading interface"""
    if not get_user(ctx.author.id):
        await ctx.send("❌ Use `Rstart` first!")
        return
    view = TradingPortfolioView(ctx.author.id, ctx.author.display_name)
    embed = await view.get_portfolio_embed()
    await ctx.send(embed=embed, view=view)

@bot.command(name="history")
async def trade_history_cmd(ctx):
    """View trading history"""
    c.execute("SELECT stock_symbol, action, shares, price, timestamp FROM trading_history WHERE user_id=? ORDER BY id DESC LIMIT 10", (ctx.author.id,))
    history = c.fetchall()
    if not history:
        await ctx.send("📭 No trading history!")
        return
    embed = discord.Embed(title="📜 Trading History", color=discord.Color.blue())
    for symbol, action, shares, price, timestamp in history:
        emoji = "🟢" if action == "BUY" else "🔴"
        ts = datetime.fromisoformat(timestamp).strftime("%m/%d %H:%M")
        embed.add_field(name=f"{emoji} {action} {shares} {symbol} @ ${price}", value=f"📅 {ts}", inline=False)
    await ctx.send(embed=embed)

async def market_update_loop():
    await bot.wait_until_ready()
    while not bot.is_closed():
        await asyncio.sleep(300)
        changes = market.update_prices()
        embed = discord.Embed(title="📈 Market Update!", color=discord.Color.green())
        for symbol, old, new in changes:
            change = ((new - old) / old) * 100
            emoji = "🟢" if change > 0 else "🔴"
            embed.add_field(name=symbol, value=f"${old} → ${new}\n{emoji} {change:+.1f}%", inline=True)
        for guild in bot.guilds:
            for channel in guild.text_channels:
                if channel.name in ["trading", "casino", "bot-commands"]:
                    await channel.send(embed=embed)
                    break

# ============================================================
#  GAMBLING GAMES (with "all" support)
# ============================================================
def parse_bet(amount_str: str, user_id: int) -> int:
    if amount_str.lower() == "all":
        return get_balance(user_id)
    return parse_amount(amount_str)

@bot.command(name="j")
async def gamble_all_cmd(ctx, amount_str: str):
    """50/50 gamble. Supports 'all'"""
    if not get_user(ctx.author.id):
        await ctx.send("❌ Use `Rstart` first!")
        return
    bet = parse_bet(amount_str, ctx.author.id)
    if not bet or bet <= 0 or get_balance(ctx.author.id) < bet:
        await ctx.send(f"❌ Invalid bet. Balance: {get_balance(ctx.author.id):,}")
        return
    if random.random() < 0.5:
        update_balance(ctx.author.id, bet)
        await ctx.send(f"🎉 WON! +{bet:,} → Balance: {get_balance(ctx.author.id):,}")
    else:
        update_balance(ctx.author.id, -bet)
        await ctx.send(f"💔 LOST! -{bet:,} → Balance: {get_balance(ctx.author.id):,}")

@bot.command(name="slots")
async def slots_all_cmd(ctx, amount_str: str):
    """Slot machine. Supports 'all'"""
    if not get_user(ctx.author.id):
        await ctx.send("❌ Use `Rstart` first!")
        return
    bet = parse_bet(amount_str, ctx.author.id)
    if not bet or bet <= 0 or get_balance(ctx.author.id) < bet:
        await ctx.send(f"❌ Invalid bet. Balance: {get_balance(ctx.author.id):,}")
        return
    update_balance(ctx.author.id, -bet)
    reels = [random.choice(["🍒", "🍋", "🍊", "⭐", "💎", "7️⃣"]) for _ in range(3)]
    if reels[0] == reels[1] == reels[2]:
        mult = 10 if reels[0] in ["💎", "7️⃣"] else 5
        win = bet * mult
        update_balance(ctx.author.id, win)
        await ctx.send(f"🎰 JACKPOT! {' '.join(reels)}\nWon {win:,} coins!")
    elif len(set(reels)) == 2:
        win = bet * 2
        update_balance(ctx.author.id, win)
        await ctx.send(f"🎰 MATCH! {' '.join(reels)}\nWon {win:,} coins!")
    else:
        await ctx.send(f"🎰 {' '.join(reels)}\nLost {bet:,} coins!")

@bot.command(name="dice")
async def dice_all_cmd(ctx, amount_str: str):
    """Dice roll 1-6. Supports 'all'"""
    if not get_user(ctx.author.id):
        await ctx.send("❌ Use `Rstart` first!")
        return
    bet = parse_bet(amount_str, ctx.author.id)
    if not bet or bet <= 0 or get_balance(ctx.author.id) < bet:
        await ctx.send(f"❌ Invalid bet. Balance: {get_balance(ctx.author.id):,}")
        return
    roll = random.randint(1, 6)
    if roll > 3:
        update_balance(ctx.author.id, bet)
        await ctx.send(f"🎲 Rolled {roll}! You win {bet:,} coins!")
    else:
        update_balance(ctx.author.id, -bet)
        await ctx.send(f"🎲 Rolled {roll}! You lose {bet:,} coins!")

@bot.command(name="coinflip")
async def coinflip_all_cmd(ctx, amount_str: str, choice: str = None):
    """Coin flip. Supports 'all'"""
    if not get_user(ctx.author.id):
        await ctx.send("❌ Use `Rstart` first!")
        return
    bet = parse_bet(amount_str, ctx.author.id)
    if not bet or bet <= 0 or get_balance(ctx.author.id) < bet:
        await ctx.send(f"❌ Invalid bet. Balance: {get_balance(ctx.author.id):,}")
        return
    result = random.choice(["heads", "tails"])
    if choice and choice.lower() == result:
        update_balance(ctx.author.id, bet)
        await ctx.send(f"🪙 {result}! You win {bet:,} coins!")
    else:
        update_balance(ctx.author.id, -bet)
        await ctx.send(f"🪙 {result}! You lose {bet:,} coins!")

# ============================================================
#  EVENTS
# ============================================================
snipe_cache = {}

@bot.event
async def on_ready():
    print(f"🌸 {bot.user} is now online!")
    await bot.change_presence(activity=discord.Game("✨ Rhelp | Rtrade | Rspin"))
    bot.loop.create_task(market_update_loop())
    reminder_loop.start()

@bot.event
async def on_message(message):
    if message.author.bot: return
    uid = message.author.id

    for mention in message.mentions:
        c.execute("SELECT reason, since FROM afk WHERE user_id=?", (mention.id,))
        r = c.fetchone()
        if r:
            since = datetime.fromisoformat(r[1])
            ago   = int((datetime.now() - since).total_seconds() // 60)
            await message.channel.send(
                f"💤 **{mention.display_name}** is AFK: *{r[0]}* — {ago}m ago")

    c.execute("SELECT 1 FROM afk WHERE user_id=?", (uid,))
    if c.fetchone():
        c.execute("DELETE FROM afk WHERE user_id=?", (uid,)); conn.commit()
        await message.channel.send(
            f"👋 Welcome back, {message.author.mention}! AFK removed.", delete_after=5)

    if get_user(uid):
        new_level = add_xp(uid, random.randint(3, 8))
        if new_level:
            reward = new_level * 500
            update_balance(uid, reward)
            rank_label, _ = get_rank_tier(new_level)
            embed = discord.Embed(
                title="🎉 Level Up!",
                description=(
                    f"{message.author.mention} reached **Level {new_level}**! {rank_label}\n"
                    f"🏆 Reward: **+{reward:,} coins**"
                ),
                color=discord.Color.gold(),
            )
            await message.channel.send(embed=embed)

    await bot.process_commands(message)

@bot.event
async def on_message_delete(message):
    if message.author.bot: return
    snipe_cache[message.guild.id] = {
        "content":   message.content,
        "author":    str(message.author),
        "avatar":    message.author.display_avatar.url,
        "channel":   message.channel.id,
        "timestamp": datetime.now(),
    }

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandOnCooldown):
        remaining = int(error.retry_after)
        h, m = divmod(remaining // 60, 60)
        s     = remaining % 60
        time_str = f"{h}h {m}m {s}s" if h else (f"{m}m {s}s" if m else f"{s}s")
        embed = discord.Embed(description=f"⏰ Cooldown! Try again in **{time_str}**.",
                              color=discord.Color.orange())
        await ctx.send(embed=embed)
    elif isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ You lack the required permissions.")
    elif isinstance(error, commands.MissingRequiredArgument):
        embed = discord.Embed(
            description=f"❌ Missing argument: `{error.param.name}`\nUse `Rhelp {ctx.command.name}` for usage.",
            color=discord.Color.red())
        await ctx.send(embed=embed)
    elif isinstance(error, commands.BadArgument):
        await ctx.send("❌ Invalid argument. Check `Rhelp` for usage.")
    elif isinstance(error, commands.CheckFailure):
        await ctx.send("🚫 You don't have permission to use that command.")
    elif isinstance(error, commands.CommandNotFound):
        pass

# ============================================================
#  GAME: HORSE RACING
# ============================================================
@bot.command(name="race")
@commands.cooldown(1, 30, commands.BucketType.user)
async def horse_racing_cmd(ctx, bet_str: str, horse_num: int = None):
    """Bet on horse races! Usage: Rrace 1000 3"""
    if not get_user(ctx.author.id):
        await ctx.send("❌ Use `Rstart` first!")
        return
    bet = parse_bet_amount(bet_str, ctx.author.id)
    if not bet or bet <= 0 or get_balance(ctx.author.id) < bet:
        await ctx.send(f"❌ Invalid bet. You have {get_balance(ctx.author.id):,} coins")
        return
    horses = [
        {"name": "🏇 Lightning Bolt", "odds": 3.0, "speed": 92, "stamina": 85, "color": 0xFFD700},
        {"name": "🐎 Midnight Shadow", "odds": 4.5, "speed": 88, "stamina": 90, "color": 0x6A0DAD},
        {"name": "🦄 Thunder Strike", "odds": 2.5, "speed": 95, "stamina": 80, "color": 0x00BFFF},
        {"name": "⭐ Shooting Star", "odds": 6.0, "speed": 85, "stamina": 88, "color": 0xFF69B4},
        {"name": "🔥 Blazing Fury", "odds": 8.0, "speed": 98, "stamina": 70, "color": 0xFF4500},
        {"name": "💨 Wind Runner", "odds": 5.0, "speed": 90, "stamina": 85, "color": 0x32CD32},
        {"name": "🏔️ Mountain King", "odds": 10.0, "speed": 75, "stamina": 95, "color": 0x8B4513},
        {"name": "🌊 Ocean Spirit", "odds": 7.0, "speed": 82, "stamina": 92, "color": 0x1E90FF},
    ]
    if horse_num is None or horse_num < 1 or horse_num > len(horses):
        embed = discord.Embed(title="🏇 HORSE RACING", color=discord.Color.gold())
        embed.add_field(name="Your Bet", value=f"{bet:,} coins", inline=True)
        desc = ""
        for i, h in enumerate(horses, 1):
            desc += f"`{i}` {h['name']} — {h['odds']}× odds (Speed: {h['speed']})\n"
        embed.description = desc
        embed.set_footer(text="Choose: Rrace <bet> <horse_number>")
        await ctx.send(embed=embed)
        return
    selected = horses[horse_num - 1]
    update_balance(ctx.author.id, -bet)
    positions = [0] * len(horses)
    track_length = 30
    race_msg = await ctx.send(f"🏁 **RACE START!** Bet: {bet:,} on {selected['name']} 🏁")
    await asyncio.sleep(1)
    for step in range(track_length):
        for i, horse in enumerate(horses):
            fatigue = step / track_length
            effective_speed = horse["speed"] * (1 - fatigue * 0.3)
            move = random.randint(1, 3) if random.random() < (effective_speed / 100) else random.randint(0, 1)
            positions[i] += move
        track_lines = []
        for i, horse in enumerate(horses):
            pos = min(positions[i], track_length)
            bar = "=" * pos + horse["name"][0] + "-" * (track_length - pos)
            track_lines.append(bar)
        await race_msg.edit(content=f"```\n🏁 RACE IN PROGRESS 🏁\n\n" + "\n".join(track_lines[:5]) + "\n```")
        await asyncio.sleep(0.3)
    max_pos = max(positions)
    winners = [i for i, pos in enumerate(positions) if pos == max_pos]
    winner_idx = random.choice(winners)
    winner = horses[winner_idx]
    if winner_idx == horse_num - 1:
        winnings = int(bet * winner["odds"])
        update_balance(ctx.author.id, winnings)
        add_xp(ctx.author.id, 20)
        embed = discord.Embed(title="🏆 YOU WIN! 🏆", description=f"{winner['name']} wins!\nWon: **{winnings:,}** coins!", color=discord.Color.gold())
    else:
        embed = discord.Embed(title="😔 You Lost", description=f"{winner['name']} won the race.\nLost: **{bet:,}** coins.", color=discord.Color.red())
    embed.add_field(name="Odds", value=f"{winner['odds']}×", inline=True)
    embed.add_field(name="Speed", value=f"{winner['speed']}", inline=True)
    embed.set_footer(text=f"Balance: {get_balance(ctx.author.id):,} coins")
    await race_msg.edit(content=None, embed=embed)

# ============================================================
#  GAME: POKER (Texas Hold'em)
# ============================================================
class PokerGame:
    def __init__(self, player_id, bet):
        self.player_id = player_id
        self.bet = bet
        self.deck = []
        self.player_cards = []
        self.ai_cards = []
        self.community = []
        self.pot = bet * 2
        
    def create_deck(self):
        suits = ["♠", "♥", "♦", "♣"]
        values = ["2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A"]
        self.deck = [f"{v}{s}" for v in values for s in suits]
        random.shuffle(self.deck)
    
    def deal(self):
        self.create_deck()
        self.player_cards = [self.deck.pop(), self.deck.pop()]
        self.ai_cards = [self.deck.pop(), self.deck.pop()]
        self.community = [self.deck.pop(), self.deck.pop(), self.deck.pop()]
    
    def deal_turn_river(self):
        self.community.append(self.deck.pop())
        self.community.append(self.deck.pop())
    
    def evaluate_hand(self, cards):
        values = [c[:-1] for c in cards]
        suits = [c[-1] for c in cards]
        value_counts = {v: values.count(v) for v in set(values)}
        is_flush = len(set(suits)) == 1
        values_sorted = sorted(["23456789JQKA".index(v) if v in "JQKA" else int(v) for v in values], reverse=True)
        is_straight = False
        for i in range(len(values_sorted)-4):
            if values_sorted[i] - values_sorted[i+4] == 4:
                is_straight = True
                break
        if is_flush and is_straight:
            return 9
        if 4 in value_counts.values():
            return 8
        if 3 in value_counts.values() and 2 in value_counts.values():
            return 7
        if is_flush:
            return 6
        if is_straight:
            return 5
        if 3 in value_counts.values():
            return 4
        if list(value_counts.values()).count(2) == 2:
            return 3
        if 2 in value_counts.values():
            return 2
        return 1

@bot.command(name="poker")
@commands.cooldown(1, 45, commands.BucketType.user)
async def poker_cmd(ctx, bet_str: str):
    """Play Texas Hold'em vs AI. Usage: Rpoker 1000"""
    if not get_user(ctx.author.id):
        await ctx.send("❌ Use `Rstart` first!")
        return
    bet = parse_bet_amount(bet_str, ctx.author.id)
    if not bet or bet <= 0 or get_balance(ctx.author.id) < bet:
        await ctx.send(f"❌ Invalid bet. You have {get_balance(ctx.author.id):,} coins")
        return
    update_balance(ctx.author.id, -bet)
    game = PokerGame(ctx.author.id, bet)
    game.deal()
    embed = discord.Embed(title="♠️ TEXAS HOLD'EM ♥️", color=discord.Color.green())
    embed.add_field(name="Your Hand", value=" ".join(game.player_cards), inline=False)
    embed.add_field(name="Flop", value=" ".join(game.community), inline=False)
    embed.add_field(name="Bet", value=f"{bet:,} coins", inline=True)
    embed.set_footer(text="Revealing turn & river...")
    msg = await ctx.send(embed=embed)
    await asyncio.sleep(2)
    game.deal_turn_river()
    all_player = game.player_cards + game.community
    all_ai = game.ai_cards + game.community
    player_rank = game.evaluate_hand(all_player)
    ai_rank = game.evaluate_hand(all_ai)
    rank_names = {9: "Straight Flush", 8: "Four of a Kind", 7: "Full House", 6: "Flush", 
                  5: "Straight", 4: "Three of a Kind", 3: "Two Pair", 2: "One Pair", 1: "High Card"}
    embed = discord.Embed(title="♠️ POKER RESULT ♥️", color=discord.Color.blue())
    embed.add_field(name="Your Hand", value=" ".join(game.player_cards), inline=True)
    embed.add_field(name="AI Hand", value=" ".join(game.ai_cards), inline=True)
    embed.add_field(name="Community", value=" ".join(game.community), inline=False)
    embed.add_field(name="Your Rank", value=rank_names[player_rank], inline=True)
    embed.add_field(name="AI Rank", value=rank_names[ai_rank], inline=True)
    if player_rank > ai_rank:
        winnings = bet * 2
        update_balance(ctx.author.id, winnings)
        embed.title = "🎉 YOU WIN! 🎉"
        embed.description = f"Won: **{winnings:,}** coins!"
        embed.color = discord.Color.gold()
    elif ai_rank > player_rank:
        embed.title = "😔 You Lose"
        embed.description = f"Lost: **{bet:,}** coins."
        embed.color = discord.Color.red()
    else:
        update_balance(ctx.author.id, bet)
        embed.title = "🤝 Push"
        embed.description = "Tie! Bet returned."
        embed.color = discord.Color.greyple()
    embed.set_footer(text=f"Balance: {get_balance(ctx.author.id):,} coins")
    await msg.edit(embed=embed)

# ============================================================
#  GAME: MINING
# ============================================================
@bot.command(name="mine")
@commands.cooldown(1, 45, commands.BucketType.user)
async def mining_cmd(ctx):
    """Mine for treasures! Risk of collapse. Usage: Rmine"""
    if not get_user(ctx.author.id):
        await ctx.send("❌ Use `Rstart` first!")
        return
    depth = random.randint(1, 100)
    collapse_chance = depth / 100
    rewards = {
        (1, 20): (50, 200, "🪨", "Stone & Coal", "common"),
        (21, 40): (200, 500, "🪙", "Copper Ore", "common"),
        (41, 60): (500, 1000, "🥈", "Silver Vein", "uncommon"),
        (61, 75): (1000, 2000, "🥇", "Gold Nugget", "rare"),
        (76, 90): (2000, 5000, "💎", "Diamond Cluster", "epic"),
        (91, 100): (5000, 10000, "👑", "Ancient Treasure!", "legendary"),
    }
    if has_item(ctx.author.id, "Dynamite"):
        reward_range = rewards.get((76, 100), rewards[(61, 75)])
        use_item(ctx.author.id, "Dynamite")
        await ctx.send("💥 **DYNAMITE USED!** Guaranteed deep mine!")
    else:
        for (low, high), reward in rewards.items():
            if low <= depth <= high:
                reward_range = reward
                break
    msg = await ctx.send(f"⛏️ **Mining at {depth}m depth...**")
    await asyncio.sleep(random.uniform(1, 2))
    if random.random() < collapse_chance:
        loss = random.randint(100, 500)
        update_balance(ctx.author.id, -loss)
        embed = discord.Embed(title="💥 MINE COLLAPSE!", description=f"You dug to {depth}m but the cave collapsed!\nLost **{loss:,}** coins!", color=discord.Color.red())
    else:
        min_reward, max_reward, emoji, desc, rarity = reward_range
        found = random.randint(min_reward, max_reward)
        update_balance(ctx.author.id, found)
        add_xp(ctx.author.id, random.randint(10, 25))
        rarity_colors = {"common": 0x808080, "uncommon": 0x00FF00, "rare": 0x0000FF, "epic": 0x800080, "legendary": 0xFFD700}
        embed = discord.Embed(title=f"⛏️ Mining Success! {emoji}", description=f"You found {desc}!\nGained **{found:,}** coins!", color=rarity_colors.get(rarity, 0x00FF00))
        embed.add_field(name="Depth", value=f"{depth}m", inline=True)
        embed.add_field(name="Rarity", value=rarity.upper(), inline=True)
        embed.add_field(name="XP", value=f"+{random.randint(10, 25)}", inline=True)
    embed.set_footer(text=f"Balance: {get_balance(ctx.author.id):,} coins")
    await msg.edit(content=None, embed=embed)

# ============================================================
#  GAME: FISHING 🎣
# ============================================================
@bot.command(name="fish")
@commands.cooldown(1, 30, commands.BucketType.user)
async def fishing_cmd(ctx):
    """Go fishing! Cast your line and wait. Usage: Rfish"""
    if not get_user(ctx.author.id):
        await ctx.send("❌ Use `Rstart` first!")
        return
    fish = [
        ("🐟", "Small Fish", 10, 50, "common"),
        ("🐠", "Colorful Fish", 50, 150, "common"),
        ("🐡", "Pufferfish", 100, 200, "uncommon"),
        ("🦑", "Squid", 150, 300, "uncommon"),
        ("🐙", "Octopus", 200, 400, "rare"),
        ("🦈", "Shark", 500, 1000, "rare"),
        ("🐋", "Whale", 1000, 2500, "legendary"),
        ("🐊", "Alligator", 800, 1500, "epic"),
        ("👟", "Old Boot", -50, -10, "trash"),
        ("🌿", "Seaweed", -20, -5, "trash"),
        ("🧦", "Lost Sock", -10, -1, "trash"),
    ]
    msg = await ctx.send("🎣 **Casting line...**")
    await asyncio.sleep(random.uniform(1.5, 3))
    await msg.edit(content="🎣 **Got a bite!** 🎣")
    await asyncio.sleep(random.uniform(0.8, 1.5))
    emoji, name, min_val, max_val, rarity = random.choice(fish)
    value = random.randint(min_val, max_val)
    if has_item(ctx.author.id, "Lucky Charm") and value > 0:
        value = int(value * 1.5)
        use_item(ctx.author.id, "Lucky Charm")
        value_bonus = " 🍀 Lucky Charm applied!"
    else:
        value_bonus = ""
    update_balance(ctx.author.id, value)
    rarity_colors = {"common": 0x808080, "uncommon": 0x00FF00, "rare": 0x0000FF, "epic": 0x800080, "legendary": 0xFFD700, "trash": 0xFF0000}
    embed = discord.Embed(title=f"🎣 Fishing Result {emoji}", description=f"You caught **{name}**!", color=rarity_colors.get(rarity, 0x808080))
    if value > 0:
        embed.add_field(name="Value", value=f"+**{value:,}** coins", inline=True)
    else:
        embed.add_field(name="Value", value=f"**{value:,}** coins (trash!)", inline=True)
    embed.add_field(name="Rarity", value=rarity.upper(), inline=True)
    if value_bonus:
        embed.add_field(name="Bonus", value=value_bonus, inline=False)
    embed.set_footer(text=f"Balance: {get_balance(ctx.author.id):,} coins")
    await msg.edit(content=None, embed=embed)

# ============================================================
#  GAME: WHEEL OF FORTUNE 🎡
# ============================================================
@bot.command(name="wheel")
@commands.cooldown(1, 60, commands.BucketType.user)
async def wheel_of_fortune_cmd(ctx, bet_str: str = "free"):
    """Spin the Wheel of Fortune! Usage: Rwheel 500 or Rwheel free"""
    if not get_user(ctx.author.id):
        await ctx.send("❌ Use `Rstart` first!")
        return
    wheel = [
        {"name": "💀 BANKRUPT", "multiplier": 0, "color": 0xFF0000, "weight": 15},
        {"name": "🍒 SMALL", "multiplier": 2, "color": 0x00FF00, "weight": 25},
        {"name": "🍋 MEDIUM", "multiplier": 3, "color": 0x00FF00, "weight": 20},
        {"name": "🍊 LARGE", "multiplier": 5, "color": 0xFFD700, "weight": 15},
        {"name": "⭐ JACKPOT", "multiplier": 10, "color": 0x800080, "weight": 8},
        {"name": "🎁 MYSTERY", "multiplier": "mystery", "color": 0x0000FF, "weight": 10},
        {"name": "🔄 FREE SPIN", "multiplier": "free", "color": 0x00FFFF, "weight": 5},
        {"name": "💎 DIAMOND", "multiplier": 7, "color": 0x00BFFF, "weight": 2},
    ]
    if bet_str.lower() == "free":
        if not can_spin(ctx.author.id):
            await ctx.send("❌ Free spin available in 60 minutes!")
            return
        bet = 0
        update_spin(ctx.author.id)
    else:
        bet = parse_bet_amount(bet_str, ctx.author.id)
        if not bet or bet <= 0 or get_balance(ctx.author.id) < bet:
            await ctx.send(f"❌ Invalid bet. You have {get_balance(ctx.author.id):,} coins")
            return
        update_balance(ctx.author.id, -bet)
    total_weight = sum(s["weight"] for s in wheel)
    rand = random.randint(1, total_weight)
    cumulative = 0
    result = None
    for segment in wheel:
        cumulative += segment["weight"]
        if rand <= cumulative:
            result = segment
            break
    spin_msg = await ctx.send("🎡 **Spinning the wheel...** 🎡")
    for _ in range(8):
        temp = random.choice(wheel)
        await spin_msg.edit(content=f"🎡 **Spinning...** → {temp['name']}")
        await asyncio.sleep(0.12)
    if result["multiplier"] == 0:
        embed = discord.Embed(title="🎡 Wheel of Fortune", description=f"Landing on **{result['name']}**!", color=result["color"])
        embed.add_field(name="Result", value=f"You lost **{bet:,}** coins!", inline=True)
    elif result["multiplier"] == "mystery":
        mystery_mult = random.choice([1, 2, 3, 5, 10, 25, 50])
        winnings = int(bet * mystery_mult) if bet > 0 else random.randint(100, 1000)
        update_balance(ctx.author.id, winnings)
        embed = discord.Embed(title="🎡 Wheel of Fortune", description=f"Landing on **{result['name']}**! 🎁", color=result["color"])
        embed.add_field(name="Mystery Multiplier", value=f"{mystery_mult}×", inline=True)
        embed.add_field(name="Won", value=f"**{winnings:,}** coins!", inline=True)
    elif result["multiplier"] == "free":
        update_spin(ctx.author.id)
        winnings = random.randint(50, 300)
        update_balance(ctx.author.id, winnings)
        embed = discord.Embed(title="🎡 Wheel of Fortune", description=f"Landing on **{result['name']}**! 🔄", color=result["color"])
        embed.add_field(name="Bonus", value=f"+1 Free Spin! +{winnings:,} coins", inline=True)
    else:
        winnings = int(bet * result["multiplier"]) if bet > 0 else random.randint(100, 800)
        update_balance(ctx.author.id, winnings)
        embed = discord.Embed(title="🎡 Wheel of Fortune", description=f"Landing on **{result['name']}**!", color=result["color"])
        embed.add_field(name="Multiplier", value=f"{result['multiplier']}×", inline=True)
        embed.add_field(name="Won", value=f"**{winnings:,}** coins!", inline=True)
    embed.set_footer(text=f"Balance: {get_balance(ctx.author.id):,} coins")
    await spin_msg.edit(content=None, embed=embed)

# ============================================================
#  GAME: PRISON BREAK 🔓
# ============================================================
@bot.command(name="prison")
@commands.cooldown(1, 300, commands.BucketType.user)
async def prison_break_cmd(ctx, difficulty: str = "normal"):
    """Try to break out of prison! Difficulty: easy, normal, hard, insane"""
    if not get_user(ctx.author.id):
        await ctx.send("❌ Use `Rstart` first!")
        return
    difficulties = {
        "easy": {"success": 0.70, "reward": 500, "penalty": 100, "emoji": "🟢"},
        "normal": {"success": 0.45, "reward": 1000, "penalty": 300, "emoji": "🟡"},
        "hard": {"success": 0.25, "reward": 2500, "penalty": 800, "emoji": "🟠"},
        "insane": {"success": 0.10, "reward": 10000, "penalty": 2000, "emoji": "🔴"},
    }
    if difficulty.lower() not in difficulties:
        await ctx.send("❌ Difficulty: easy, normal, hard, insane")
        return
    diff = difficulties[difficulty.lower()]
    tools = []
    for tool in ["Shovel", "Rope", "Hacksaw"]:
        if has_item(ctx.author.id, tool):
            tools.append(tool)
    tool_bonus = len(tools) * 0.10
    success_chance = min(diff["success"] + tool_bonus, 0.95)
    embed = discord.Embed(title="🔓 PRISON BREAK", color=discord.Color.orange())
    embed.add_field(name="Difficulty", value=f"{diff['emoji']} {difficulty.upper()}", inline=True)
    embed.add_field(name="Base Success", value=f"{diff['success']*100:.0f}%", inline=True)
    embed.add_field(name="Tools", value=", ".join(tools) if tools else "None", inline=True)
    embed.add_field(name="Success Rate", value=f"{success_chance*100:.0f}%", inline=True)
    embed.add_field(name="Reward", value=f"{diff['reward']:,} coins", inline=True)
    embed.add_field(name="Penalty", value=f"{diff['penalty']:,} coins", inline=True)
    msg = await ctx.send(embed=embed)
    await asyncio.sleep(2)
    for frame in ["🔒 Digging...", "⛏️ Cutting bars...", "🏃 Running...", "🚪 Almost there..."]:
        await msg.edit(content=frame, embed=None)
        await asyncio.sleep(0.8)
    for tool in tools:
        use_item(ctx.author.id, tool)
    if random.random() < success_chance:
        update_balance(ctx.author.id, diff["reward"])
        add_xp(ctx.author.id, 30)
        embed = discord.Embed(title="✅ ESCAPE SUCCESSFUL!", description=f"You escaped from {difficulty} prison!\nWon **{diff['reward']:,}** coins +30 XP!", color=discord.Color.green())
    else:
        update_balance(ctx.author.id, -diff["penalty"])
        embed = discord.Embed(title="❌ CAUGHT!", description=f"You were caught escaping {difficulty} prison!\nLost **{diff['penalty']:,}** coins!", color=discord.Color.red())
    embed.set_footer(text=f"Balance: {get_balance(ctx.author.id):,} coins")
    await msg.edit(content=None, embed=embed)

# ============================================================
#  GAME: MEMORY MATCH
# ============================================================
class MemoryGameView(View):
    def __init__(self, user_id, bet):
        super().__init__(timeout=60)
        self.user_id = user_id
        self.bet = bet
        self.cards = []
        self.revealed = [False] * 16
        self.selected = None
        self.matched = [False] * 16
        self.attempts = 0
        self.create_cards()
    
    def create_cards(self):
        emojis = ["🐶", "🐱", "🐭", "🐹", "🐰", "🦊", "🐻", "🐼", "🐶", "🐱", "🐭", "🐹", "🐰", "🦊", "🐻", "🐼"]
        random.shuffle(emojis)
        self.cards = emojis
    
    async def check_win(self, interaction):
        if all(self.matched):
            reward = self.bet * (2 + (16 - self.attempts) / 16)
            reward = int(reward)
            update_balance(self.user_id, reward)
            add_xp(self.user_id, 25)
            embed = discord.Embed(title="🧠 MEMORY MATCH - PERFECT!", description=f"You matched all pairs!\nWon: **{reward:,}** coins!", color=discord.Color.gold())
            embed.set_footer(text=f"Attempts: {self.attempts}")
            await interaction.response.edit_message(embed=embed, view=None)
            self.stop()
            return True
        return False

class MemoryButton(Button):
    def __init__(self, index, label, style):
        super().__init__(label=label, style=style, custom_id=f"memory_{index}")
        self.index = index
    
    async def callback(self, interaction: discord.Interaction):
        view = self.view
        if interaction.user.id != view.user_id:
            await interaction.response.send_message("❌ Not your game!", ephemeral=True)
            return
        if view.revealed[self.index] or view.matched[self.index]:
            await interaction.response.defer()
            return
        view.revealed[self.index] = True
        view.attempts += 1
        if view.selected is None:
            view.selected = self.index
            await update_memory_display(interaction, view)
        else:
            first = view.selected
            second = self.index
            if view.cards[first] == view.cards[second]:
                view.matched[first] = True
                view.matched[second] = True
                view.revealed[first] = True
                view.revealed[second] = True
                view.selected = None
                await update_memory_display(interaction, view)
                if await view.check_win(interaction):
                    return
            else:
                await update_memory_display(interaction, view)
                await asyncio.sleep(1)
                view.revealed[first] = False
                view.revealed[second] = False
                view.selected = None
                await update_memory_display(interaction, view)

async def update_memory_display(interaction, view):
    embed = discord.Embed(title="🧠 MEMORY MATCH", description=f"Bet: {view.bet:,} coins | Attempts: {view.attempts}", color=discord.Color.blue())
    grid = ""
    for i in range(0, 16, 4):
        row = ""
        for j in range(4):
            idx = i + j
            if view.matched[idx]:
                row += "✅ "
            elif view.revealed[idx]:
                row += f"{view.cards[idx]} "
            else:
                row += "❓ "
        grid += row + "\n"
    embed.description = f"```\n{grid}\n```"
    view.clear_items()
    for i in range(16):
        if not view.matched[i] and not view.revealed[i]:
            view.add_item(MemoryButton(i, "❓", discord.ButtonStyle.secondary))
    await interaction.response.edit_message(embed=embed, view=view)

@bot.command(name="memory")
@commands.cooldown(1, 60, commands.BucketType.user)
async def memory_game_cmd(ctx, bet_str: str):
    """Memory match game! Match pairs to win. Usage: Rmemory 500"""
    if not get_user(ctx.author.id):
        await ctx.send("❌ Use `Rstart` first!")
        return
    bet = parse_bet_amount(bet_str, ctx.author.id)
    if not bet or bet <= 0 or get_balance(ctx.author.id) < bet:
        await ctx.send(f"❌ Invalid bet. You have {get_balance(ctx.author.id):,} coins")
        return
    update_balance(ctx.author.id, -bet)
    view = MemoryGameView(ctx.author.id, bet)
    embed = discord.Embed(title="🧠 MEMORY MATCH", description=f"Bet: {bet:,} coins\nMatch all pairs to win!", color=discord.Color.blue())
    for i in range(16):
        view.add_item(MemoryButton(i, "❓", discord.ButtonStyle.secondary))
    await ctx.send(embed=embed, view=view)

# ============================================================
#  GAME: FRUIT MACHINE 🍒
# ============================================================
@bot.command(name="fruit")
@commands.cooldown(1, 5, commands.BucketType.user)
async def fruit_machine_cmd(ctx, bet_str: str):
    """Classic fruit machine slots. Usage: Rfruit 500"""
    if not get_user(ctx.author.id):
        await ctx.send("❌ Use `Rstart` first!")
        return
    bet = parse_bet_amount(bet_str, ctx.author.id)
    if not bet or bet <= 0 or get_balance(ctx.author.id) < bet:
        await ctx.send(f"❌ Invalid bet. You have {get_balance(ctx.author.id):,} coins")
        return
    update_balance(ctx.author.id, -bet)
    fruits = {
        "🍒": 2, "🍋": 3, "🍊": 4, "🍉": 5, "⭐": 10, "💎": 20, "🔔": 15, "7️⃣": 50
    }
    fruit_list = list(fruits.keys())
    weights = [30, 25, 20, 12, 6, 3, 3, 1]
    msg = await ctx.send("🍒 **SPINNING...** 🍒")
    for _ in range(5):
        temp = [random.choices(fruit_list, weights=weights)[0] for _ in range(3)]
        await msg.edit(content=f"🎰 `{' | '.join(temp)}`")
        await asyncio.sleep(0.2)
    reels = [random.choices(fruit_list, weights=weights)[0] for _ in range(3)]
    win = 0
    if reels[0] == reels[1] == reels[2]:
        win = bet * fruits[reels[0]]
    elif reels[0] == reels[1]:
        win = bet * (fruits[reels[0]] // 2)
    elif reels[1] == reels[2]:
        win = bet * (fruits[reels[1]] // 2)
    if win > 0:
        update_balance(ctx.author.id, win)
        embed = discord.Embed(title="🍒 FRUIT MACHINE - WINNER!", description=f"`{' | '.join(reels)}`\nWon: **{win:,}** coins!", color=discord.Color.gold())
    else:
        embed = discord.Embed(title="🍒 FRUIT MACHINE - LOST", description=f"`{' | '.join(reels)}`\nLost: **{bet:,}** coins!", color=discord.Color.red())
    embed.set_footer(text=f"Balance: {get_balance(ctx.author.id):,} coins")
    await msg.edit(content=None, embed=embed)

# ============================================================
#  GAME: DICE DUEL (PvP)
# ============================================================
class DiceDuelView(View):
    def __init__(self, challenger, opponent, bet):
        super().__init__(timeout=60)
        self.challenger = challenger
        self.opponent = opponent
        self.bet = bet
        self.challenger_rolls = []
        self.opponent_rolls = []
        self.challenger_kept = []
        self.opponent_kept = []
        self.turn = challenger
        self.round = 1
    
    def calculate_score(self, rolls):
        counts = {i: rolls.count(i) for i in range(1, 7)}
        if 5 in counts.values():
            return 50
        if 4 in counts.values():
            return sum(rolls) + 20
        if 3 in counts.values() and 2 in counts.values():
            return sum(rolls) + 15
        if sorted(rolls) in [[1,2,3,4,5], [2,3,4,5,6]]:
            return sum(rolls) + 10
        if 3 in counts.values():
            return sum(rolls)
        return sum(rolls) // 2

@bot.command(name="duel")
@commands.cooldown(1, 30, commands.BucketType.user)
async def dice_duel_cmd(ctx, opponent: discord.Member, bet_str: str):
    """Challenge someone to a dice duel! Usage: Rduel @user 500"""
    if not get_user(ctx.author.id) or not get_user(opponent.id):
        await ctx.send("❌ Both players need accounts!")
        return
    if opponent == ctx.author:
        await ctx.send("❌ Can't duel yourself!")
        return
    bet = parse_amount(bet_str)
    if not bet or bet <= 0:
        await ctx.send("❌ Invalid bet!")
        return
    if get_balance(ctx.author.id) < bet or get_balance(opponent.id) < bet:
        await ctx.send("❌ One of you can't afford the bet!")
        return
    view = ConfirmView(opponent.id, 30)
    embed = discord.Embed(title="🎲 DICE DUEL CHALLENGE!", description=f"{ctx.author.mention} challenges you to a dice duel!\nBet: **{bet:,}** coins\nDo you accept?", color=discord.Color.orange())
    msg = await ctx.send(f"{opponent.mention}", embed=embed, view=view)
    await view.wait()
    if not view.value:
        await msg.edit(content=f"❌ {opponent.mention} declined the duel!", embed=None, view=None)
        return
    update_balance(ctx.author.id, -bet)
    update_balance(opponent.id, -bet)
    pot = bet * 2
    embed = discord.Embed(title="🎲 DICE DUEL", color=discord.Color.blue())
    embed.add_field(name=f"{ctx.author.display_name}", value="Rolling...", inline=True)
    embed.add_field(name=f"{opponent.display_name}", value="Rolling...", inline=True)
    embed.add_field(name="Prize Pool", value=f"{pot:,} coins", inline=True)
    await msg.edit(embed=embed, view=None)
    await asyncio.sleep(1)
    p1_rolls = [random.randint(1, 6) for _ in range(5)]
    p2_rolls = [random.randint(1, 6) for _ in range(5)]
    p1_score = sum(p1_rolls)
    p2_score = sum(p2_rolls)
    if sorted(p1_rolls) == [1,2,3,4,5] or sorted(p1_rolls) == [2,3,4,5,6]:
        p1_score += 20
    if sorted(p2_rolls) == [1,2,3,4,5] or sorted(p2_rolls) == [2,3,4,5,6]:
        p2_score += 20
    if len(set(p1_rolls)) == 1:
        p1_score += 50
    if len(set(p2_rolls)) == 1:
        p2_score += 50
    result_embed = discord.Embed(title="🎲 DICE DUEL RESULT", color=discord.Color.gold())
    result_embed.add_field(name=f"{ctx.author.display_name}", value=f"Rolls: {', '.join(map(str, p1_rolls))}\nScore: **{p1_score}**", inline=True)
    result_embed.add_field(name=f"{opponent.display_name}", value=f"Rolls: {', '.join(map(str, p2_rolls))}\nScore: **{p2_score}**", inline=True)
    result_embed.add_field(name="Prize Pool", value=f"{pot:,} coins", inline=True)
    if p1_score > p2_score:
        update_balance(ctx.author.id, pot)
        result_embed.title = f"🏆 {ctx.author.display_name} WINS!"
        result_embed.description = f"Won **{pot:,}** coins!"
        result_embed.color = discord.Color.green()
    elif p2_score > p1_score:
        update_balance(opponent.id, pot)
        result_embed.title = f"🏆 {opponent.display_name} WINS!"
        result_embed.description = f"Won **{pot:,}** coins!"
        result_embed.color = discord.Color.green()
    else:
        update_balance(ctx.author.id, bet)
        update_balance(opponent.id, bet)
        result_embed.title = "🤝 TIE!"
        result_embed.description = "Bets returned to both players!"
        result_embed.color = discord.Color.greyple()
    await msg.edit(embed=result_embed)

# ============================================================
#  ADD GAME ITEMS TO SHOP (already done earlier, but ensure)
# ============================================================
game_items = [
    ("Dynamite", 5000, "Guaranteed deep mine in mining game", "💥"),
    ("Lucky Charm", 3000, "1.5x fishing/mining rewards", "🍀"),
    ("Shovel", 1000, "Prison break tool (+10% success)", "🪣"),
    ("Rope", 800, "Prison break tool (+10% success)", "🪢"),
    ("Hacksaw", 1500, "Prison break tool (+10% success)", "🔧"),
]
for item, price, desc, emoji in game_items:
    c.execute("INSERT OR IGNORE INTO shop VALUES (?,?,?,?)", (item, price, desc, emoji))
conn.commit()

# ============================================================
#  BACKGROUND TASKS
# ============================================================
@tasks.loop(seconds=30)
async def reminder_loop():
    now = datetime.now().isoformat()
    c.execute("SELECT id, user_id, channel_id, message FROM reminders WHERE remind_at <= ?", (now,))
    due = c.fetchall()
    for rid, uid, chid, msg in due:
        ch = bot.get_channel(chid)
        if ch:
            try:
                user = await bot.fetch_user(uid)
                embed = discord.Embed(
                    title="⏰ Reminder!",
                    description=f"{user.mention} — **{msg}**",
                    color=discord.Color.teal(),
                )
                await ch.send(embed=embed)
            except Exception: pass
        c.execute("DELETE FROM reminders WHERE id=?", (rid,)); conn.commit()

# ============================================================
#  HELP COMMAND with select menu
# ============================================================
HELP_CATEGORIES = {
    "🎰 Casino": "`Rj <all/bet>` `Rslots <all/bet>` `Rdice <all/bet>` `Rcoinflip <all/bet>` `Rcrash <bet>` `Rroulette <bet> <type>` `Rblackjack <bet>` `Rhigherlower <bet>`",
    "💰 Economy": "`Rstart` `Rbalance` `Rdaily` `Rwork` `Rtransfer` `Rgive` `Rrob` `Rheist` `Rleaderboard`",
    "🎮 Games": "`Rguess` `Rrps` `Rtrivia` `Rkof` `R8ball` `Rroast` `Rrate` `Rhowgay`",
    "🎁 Lottery": "`Rbuy` `Rjackpot` `Rdraw`",
    "🛒 Shop": "`Rshop` `Rbuyitem` `Rinventory`",
    "👤 Profile": "`Rprofile` `Rsetbio` `Rsetcolor` `Rrep` `Rtopbio` `Rlevel`",
    "💍 Social": "`Rmarry` `Rdivorce` `Rship`",
    "🤖 AI": "`Rai` — ask RAA AI anything",
    "🎵 Music": "`Rplay` `Rpl` `Rp` `Rstop` `Rskip` `Rqueue` `Rnowplaying` `Rpause` `Rresume` `Rvolume` `Rleave`",
    "🎨 Fun": "`Rcat` `Rmeme` `Rsnipe` `Rafk`",
    "⚙️ Utility": "`Rping` `Ravatar` `Ruserinfo` `Rserverinfo` `Rcmt` `Rpoll` `Rremind` `Rgiveaway`",
    "🛡️ Moderation": "`Rkick` `Rban` `Runban` `Rmute` `Runmute` `Rwarn` `Rwarns` `Rclearwarns`",
    "👑 Owner": "`Rm` (add coins)  `Rreset` (reset user) `Rsetlevel` `Rshop_add` `Rshop_remove`",
    "📈 Trading": "`Rmarket` – View stock prices\n`Rtrade` – Open trading GUI\n`Rhistory` – View trade history",
    "🏇 New Games": (
        "`Rrace <bet> <horse>` – Horse racing with live animation\n"
        "`Rpoker <bet>` – Texas Hold'em vs AI\n"
        "`Rmine` – Mining for treasures (risk collapse)\n"
        "`Rfish` – Fishing with rarity system\n"
        "`Rwheel [bet/free]` – Wheel of Fortune\n"
        "`Rprison <difficulty>` – Prison break (easy/hard/insane)\n"
        "`Rmemory <bet>` – Memory match pairs game\n"
        "`Rfruit <bet>` – Classic fruit machine slots\n"
        "`Rduel @user <bet>` – PvP dice duel"
    ),
}

class HelpSelect(Select):
    def __init__(self):
        options = [discord.SelectOption(label=k, value=k) for k in HELP_CATEGORIES]
        super().__init__(placeholder="📖 Choose a category…", options=options)

    async def callback(self, interaction: discord.Interaction):
        cat = self.values[0]
        embed = discord.Embed(title=f"{cat} Commands", description=HELP_CATEGORIES[cat],
                              color=discord.Color.pink())
        embed.set_footer(text="Use Rhelp <command> for detailed help on any command.")
        await interaction.response.edit_message(embed=embed, view=self.view)

class HelpView(View):
    def __init__(self):
        super().__init__(timeout=60)
        self.add_item(HelpSelect())

@bot.command(name="help", aliases=["h"])
async def cute_help(ctx, cmd_name=None):
    if cmd_name:
        cmd = bot.get_command(cmd_name)
        if cmd:
            embed = discord.Embed(title=f"📖 R{cmd_name}",
                                  description=cmd.help or "No description available.",
                                  color=discord.Color.pink())
            await ctx.send(embed=embed)
            return
        await ctx.send("❌ Command not found.")
        return
    embed = discord.Embed(
        title="༉‧₊˚✧ BOBxRAA Casino Bot v4.0 ✧˚₊‧༉",
        description=(
            "Select a category below to see commands.\n"
            "Use `Rhelp <command>` for details on any command.\n\n"
            "✨ **New Features:** Trading stocks, Horse Racing, Poker, Mining, Fishing,\n"
            "Wheel of Fortune, Prison Break, Memory Match, Fruit Machine, Dice Duel!"
        ),
        color=discord.Color.pink(),
    )
    embed.set_footer(text="✨ BOBxRAA v4.0 — upgraded with 10 new games 💕")
    await ctx.send(embed=embed, view=HelpView())

# ============================================================
#  ACCOUNT & ECONOMY COMMANDS (start, balance, daily, work, transfer, give, rob, heist, leaderboard)
# ============================================================
@bot.command(name="start")
async def start(ctx):
    embed = discord.Embed(
        title="🌸 Welcome to BOBxRAA Casino!",
        description="Click below to create your account and start earning!",
        color=discord.Color.pink(),
    )
    await ctx.send(embed=embed, view=CreateAccountView())

@bot.command(name="create")
async def create_acc(ctx):
    if get_user(ctx.author.id):
        await ctx.send("⚠️ You already have an account!"); return
    create_user(ctx.author.id)
    await ctx.send(f"✨ Account created for {ctx.author.mention}! You start with **100 coins**.")

@bot.command(name="balance", aliases=["bal", "money"])
async def balance_cmd(ctx, member: discord.Member = None):
    target = member or ctx.author
    row = get_user(target.id)
    if not row:
        await ctx.send("❌ That user has no account."); return
    uid, balance, *_, xp, level, _, _, streak = row
    needed = level * 100
    c.execute("SELECT COUNT(*)+1 FROM users WHERE balance>(SELECT balance FROM users WHERE user_id=?)", (target.id,))
    lb_rank = c.fetchone()[0]
    rank_label, _ = get_rank_tier(level)
    embed = discord.Embed(title=f"💰 {target.display_name}'s Wallet", color=discord.Color.gold())
    embed.set_thumbnail(url=target.display_avatar.url)
    embed.add_field(name="Balance",     value=f"**{balance:,}** coins\n`{coin_bar(balance)}`", inline=True)
    embed.add_field(name="Level",       value=f"{rank_label}\nLv **{level}** ({xp}/{needed} XP)", inline=True)
    embed.add_field(name="LB Rank",     value=f"**#{lb_rank}**", inline=True)
    embed.add_field(name="🔥 Streak",   value=f"**{streak}** day streak", inline=True)
    await ctx.send(embed=embed)

@bot.command(name="daily")
async def daily_cmd(ctx):
    if not get_user(ctx.author.id):
        await ctx.send("❌ Use `Rstart` first!"); return
    if not can_daily(ctx.author.id):
        time_left = time_until_daily(ctx.author.id)
        embed = discord.Embed(
            description=f"❌ Already claimed today! Come back in **{time_left}**. 🕐",
            color=discord.Color.red())
        await ctx.send(embed=embed); return

    c.execute("SELECT streak, daily_last FROM users WHERE user_id=?", (ctx.author.id,))
    streak_row = c.fetchone()
    streak = streak_row[0] if streak_row else 0
    last   = streak_row[1] if streak_row and streak_row[1] else None
    if last:
        try:
            diff   = datetime.now() - datetime.fromisoformat(last)
            streak = streak + 1 if diff < timedelta(days=2) else 1
        except: streak = 1
    else: streak = 1

    base   = random.randint(100, 400)
    bonus  = streak * 25
    reward = base + bonus

    boosted = False
    if has_item(ctx.author.id, "Lucky Charm"):
        reward  = int(reward * 1.5)
        boosted = True
        use_item(ctx.author.id, "Lucky Charm")

    update_balance(ctx.author.id, reward)
    update_daily(ctx.author.id)
    c.execute("UPDATE users SET streak=? WHERE user_id=?", (streak, ctx.author.id)); conn.commit()

    streak_bonus = 0
    if streak >= 7 and streak % 7 == 0:
        streak_bonus = 500
        update_balance(ctx.author.id, streak_bonus)

    embed = discord.Embed(title="🎁 Daily Reward!", color=discord.Color.green())
    embed.add_field(name="Base Reward",   value=f"**{base:,}** coins",          inline=True)
    embed.add_field(name="Streak Bonus",  value=f"**+{bonus:,}** coins",        inline=True)
    embed.add_field(name="🔥 Streak",     value=f"**{streak} days**",           inline=True)
    if boosted:
        embed.add_field(name="🍀 Lucky Charm", value="1.5× boost applied!",    inline=False)
    if streak_bonus:
        embed.add_field(name="🏅 7-Day Bonus", value=f"**+{streak_bonus}** coins!", inline=False)
    embed.add_field(name="💰 Total",      value=f"**{reward + streak_bonus:,}** coins", inline=False)
    embed.set_footer(text=f"New balance: {get_balance(ctx.author.id):,} coins")
    await ctx.send(embed=embed)

@bot.command(name="work")
@commands.cooldown(1, 1800, commands.BucketType.user)
async def work_cmd(ctx):
    if not get_user(ctx.author.id):
        await ctx.send("❌ Use `Rstart` first!"); return
    earnings = random.randint(50, 300)
    xp_gain  = random.randint(8, 15)
    update_balance(ctx.author.id, earnings)
    new_level = add_xp(ctx.author.id, xp_gain)
    jobs = [
        ("📝 coded an app",        "Developer",    "🖥️"),
        ("🍰 baked cakes",          "Baker",        "👨‍🍳"),
        ("📚 tutored students",     "Tutor",        "📖"),
        ("🎨 designed logos",       "Designer",     "🎨"),
        ("💻 freelanced online",    "Freelancer",   "💼"),
        ("🚗 drove Uber",           "Driver",       "🚘"),
        ("🎵 busked on the street", "Musician",     "🎸"),
        ("🛒 stocked shelves",      "Storehand",    "🏪"),
        ("🔧 fixed computers",      "Technician",   "🔩"),
        ("📸 took photos",          "Photographer", "📷"),
        ("📦 delivered packages",   "Courier",      "🚚"),
        ("🌿 mowed lawns",          "Landscaper",   "🌱"),
        ("🧹 cleaned offices",      "Cleaner",      "🧽"),
        ("🐕 walked dogs",          "Dog Walker",   "🐾"),
        ("💇 cut hair",             "Barber",       "✂️"),
    ]
    job_text, title, icon = random.choice(jobs)
    embed = discord.Embed(
        title=f"{icon} Work Complete!",
        description=f"You {job_text} as a **{title}**.",
        color=discord.Color.green(),
    )
    embed.add_field(name="💰 Earned",  value=f"**{earnings:,}** coins", inline=True)
    embed.add_field(name="⚡ XP",      value=f"+{xp_gain} XP",          inline=True)
    embed.add_field(name="⏳ Next Job", value="30 minutes",              inline=True)
    if new_level:
        embed.add_field(name="🎉 Level Up!", value=f"You reached **Level {new_level}**!", inline=False)
    embed.set_footer(text=f"Balance: {get_balance(ctx.author.id):,} coins")
    await ctx.send(embed=embed)

@bot.command(name="transfer")
async def transfer_cmd(ctx, member: discord.Member, amount: int):
    if not get_user(ctx.author.id) or not get_user(member.id):
        await ctx.send("❌ Both users need accounts!"); return
    if amount <= 0:
        await ctx.send("❌ Amount must be positive!"); return
    if get_balance(ctx.author.id) < amount:
        await ctx.send(f"❌ Insufficient balance! You have **{get_balance(ctx.author.id):,}** coins."); return
    if member == ctx.author:
        await ctx.send("❌ Can't transfer to yourself."); return
    update_balance(ctx.author.id, -amount)
    update_balance(member.id, amount)
    embed = discord.Embed(
        title="💸 Transfer Complete",
        description=f"{ctx.author.mention} → {member.mention}\n**{amount:,}** coins transferred.",
        color=discord.Color.green(),
    )
    embed.add_field(name="Your Balance", value=f"{get_balance(ctx.author.id):,} coins", inline=True)
    await ctx.send(embed=embed)

@bot.command(name="give")
async def give_cmd(ctx, member: discord.Member, amount_str: str):
    if amount_str.lower() == "all":
        amt = get_balance(ctx.author.id)
    else:
        amt = parse_amount(amount_str)
    if not amt or amt <= 0:
        await ctx.send("❌ Invalid amount! e.g. `Rgive @user 100k`"); return
    if not get_user(ctx.author.id) or not get_user(member.id):
        await ctx.send("❌ Both need accounts!"); return
    if get_balance(ctx.author.id) < amt:
        await ctx.send(f"❌ You only have **{get_balance(ctx.author.id):,}** coins."); return
    if member == ctx.author:
        await ctx.send("❌ Can't give to yourself."); return
    update_balance(ctx.author.id, -amt)
    update_balance(member.id, amt)
    embed = discord.Embed(
        title="💝 Gift Sent!",
        description=f"{ctx.author.mention} gifted **{amt:,}** coins to {member.mention}!",
        color=discord.Color.green(),
    )
    await ctx.send(embed=embed)

@bot.command(name="rob")
@commands.cooldown(1, 2700, commands.BucketType.user)
async def rob_cmd(ctx, member: discord.Member):
    if not get_user(ctx.author.id) or not get_user(member.id):
        await ctx.send("❌ Both users need accounts!"); return
    if member == ctx.author:
        await ctx.send("❌ Can't rob yourself."); return
    victim_bal = get_balance(member.id)
    if victim_bal < 500:
        await ctx.send(f"❌ {member.mention} is too poor to rob (under 500 coins)."); return
    if has_item(member.id, "Shield"):
        use_item(member.id, "Shield")
        fine = random.randint(100, 300)
        update_balance(ctx.author.id, -fine)
        embed = discord.Embed(
            title="🛡️ Robbery Failed!",
            description=f"{member.mention} had a **Shield**!\nYou got caught and paid a **{fine:,}** coin fine.",
            color=discord.Color.red())
        await ctx.send(embed=embed); return

    success = random.random() < 0.45
    if success:
        multiplier = 2 if has_item(ctx.author.id, "Bomb") else 1
        if multiplier == 2: use_item(ctx.author.id, "Bomb")
        stolen = random.randint(100, min(victim_bal // 3, 5000)) * multiplier
        update_balance(member.id, -stolen)
        update_balance(ctx.author.id, stolen)
        embed = discord.Embed(
            title="🦹 Robbery Successful!",
            description=f"You stole **{stolen:,}** coins from {member.mention}!\n" + ("💣 **Bomb doubled it!**" if multiplier == 2 else ""),
            color=discord.Color.green())
    else:
        fine = random.randint(100, 500)
        update_balance(ctx.author.id, -fine)
        embed = discord.Embed(
            title="🚨 Caught!",
            description=f"You got caught robbing {member.mention} and paid a **{fine:,}** coin fine!",
            color=discord.Color.red())
    await ctx.send(embed=embed)

@bot.command(name="heist")
async def heist_cmd(ctx, amount_str: str):
    amt = parse_amount(amount_str)
    if not amt or amt < 500:
        await ctx.send("❌ Minimum heist bet is **500 coins**."); return
    if get_balance(ctx.author.id) < amt:
        await ctx.send("❌ Not enough coins!"); return
    if not get_user(ctx.author.id):
        await ctx.send("❌ Use `Rstart` first!"); return
    update_balance(ctx.author.id, -amt)
    view = HeistJoinView(ctx.author.id, amt)
    embed = discord.Embed(
        title="🏦 HEIST STARTING!",
        description=(
            f"{ctx.author.mention} is organizing a heist!\n"
            f"**Entry bet:** {amt:,} coins\n"
            f"Click the button to join! (60 seconds)"
        ),
        color=discord.Color.orange(),
    )
    msg = await ctx.send(embed=embed, view=view)
    await asyncio.sleep(60)
    view.stop()
    view.participants.add(ctx.author.id)
    if len(view.participants) < 2:
        update_balance(ctx.author.id, amt)
        await msg.edit(content="❌ Not enough players joined! Bets refunded.", embed=None, view=None)
        return
    pot = amt * len(view.participants)
    success_chance = min(0.3 + len(view.participants) * 0.1, 0.75)
    if random.random() < success_chance:
        share = (pot * 2) // len(view.participants)
        for uid in view.participants: update_balance(uid, share)
        names = " ".join(f"<@{uid}>" for uid in view.participants)
        result_embed = discord.Embed(
            title="🎉 HEIST SUCCESSFUL!",
            description=f"{names}\nEach won **{share:,}** coins!",
            color=discord.Color.green())
    else:
        result_embed = discord.Embed(
            title="🚨 HEIST FAILED!",
            description=f"The police caught everyone! All **{pot:,}** coins lost!",
            color=discord.Color.red())
    await msg.edit(embed=result_embed, view=None)

@bot.command(name="leaderboard", aliases=["lb"])
async def leaderboard_cmd(ctx):
    top = get_top_balances()
    if not top:
        await ctx.send("❌ No players yet."); return
    embed = discord.Embed(title="🏆 Coin Leaderboard", color=discord.Color.gold())
    medals = {1: "👑", 2: "🥈", 3: "🥉"}
    desc = []
    for i, (uid, bal) in enumerate(top, 1):
        try: user = await bot.fetch_user(uid)
        except: user = None
        name  = user.display_name if user else f"User#{uid}"
        medal = medals.get(i, f"`#{i}`")
        bar   = coin_bar(bal, 8)
        desc.append(f"{medal} **{name}**\n`{bar}` {bal:,} coins")
    embed.description = "\n".join(desc)
    await ctx.send(embed=embed)

# ============================================================
#  SHOP & INVENTORY
# ============================================================
@bot.command(name="shop")
async def shop_cmd(ctx):
    c.execute("SELECT item, price, description, emoji FROM shop ORDER BY price")
    items = c.fetchall()
    embed = discord.Embed(
        title="🛒 BOBxRAA Item Shop",
        description="Select an item below to view details and purchase!",
        color=discord.Color.blue(),
    )
    for item, price, desc, emoji in items:
        embed.add_field(name=f"{emoji} {item}", value=f"{price:,} coins — {desc}", inline=False)
    await ctx.send(embed=embed, view=ShopView(items))

@bot.command(name="buyitem")
async def buy_item_cmd(ctx, *, item_name: str):
    c.execute("SELECT item, price, emoji FROM shop WHERE LOWER(item)=LOWER(?)", (item_name,))
    row = c.fetchone()
    if not row:
        await ctx.send("❌ Item not found. Use `Rshop` to browse."); return
    item, price, emoji = row
    if not get_user(ctx.author.id):
        await ctx.send("❌ Use `Rstart` first!"); return
    if get_balance(ctx.author.id) < price:
        await ctx.send(f"❌ Not enough coins! You need **{price:,}** coins."); return
    view = ConfirmView(ctx.author.id)
    embed = discord.Embed(
        title=f"{emoji} Purchase {item}?",
        description=f"**Price:** {price:,} coins\n**Your balance:** {get_balance(ctx.author.id):,} coins",
        color=discord.Color.blue(),
    )
    msg = await ctx.send(embed=embed, view=view)
    await view.wait()
    if view.value:
        if get_balance(ctx.author.id) < price:
            await msg.edit(content="❌ Not enough coins!", embed=None, view=None); return
        update_balance(ctx.author.id, -price)
        give_item(ctx.author.id, item)
        await msg.edit(content=f"{emoji} Purchased **{item}** for **{price:,}** coins! ✅", embed=None, view=None)
    else:
        await msg.edit(content="❌ Purchase cancelled.", embed=None, view=None)

@bot.command(name="inventory", aliases=["inv", "bag"])
async def inventory_cmd(ctx, member: discord.Member = None):
    target = member or ctx.author
    c.execute("SELECT item, qty FROM inventory WHERE user_id=? AND qty>0", (target.id,))
    items = c.fetchall()
    if not items:
        await ctx.send(f"🎒 {target.display_name}'s bag is empty!"); return
    embed = discord.Embed(title=f"🎒 {target.display_name}'s Inventory", color=discord.Color.teal())
    embed.set_thumbnail(url=target.display_avatar.url)
    for item, qty in items:
        c.execute("SELECT emoji, description FROM shop WHERE item=?", (item,))
        sr = c.fetchone()
        emoji = sr[0] if sr else "🎁"
        desc  = sr[1] if sr else "Special item"
        embed.add_field(name=f"{emoji} {item} ×{qty}", value=desc, inline=False)
    await ctx.send(embed=embed)

# ============================================================
#  GAMBLING GAMES (crash, roulette, blackjack, higherlower)
# ============================================================
@bot.command(name="crash")
@commands.cooldown(1, 10, commands.BucketType.user)
async def crash_cmd(ctx, amount_str: str):
    if not get_user(ctx.author.id):
        await ctx.send("❌ Use `Rstart` first!"); return
    if amount_str.lower() == "all":
        bet = get_balance(ctx.author.id)
    else:
        bet = parse_amount(amount_str)
    if not bet or bet <= 0:
        await ctx.send("❌ Invalid bet."); return
    if get_balance(ctx.author.id) < bet:
        await ctx.send("❌ Insufficient balance."); return

    crash_point = round(random.uniform(1.1, 10.0), 2)
    multiplier  = 1.0
    update_balance(ctx.author.id, -bet)

    def _bar(m, cap=10.0, length=10):
        filled = min(int((m / cap) * length), length)
        return "🟩" * filled + "⬛" * (length - filled)

    embed = discord.Embed(title="🚀 CRASH GAME", color=discord.Color.blue())
    embed.add_field(name="Bet",        value=f"{bet:,} coins")
    embed.add_field(name="Multiplier", value=f"**{multiplier:.2f}×**")
    embed.add_field(name="Payout",     value=f"{int(bet * multiplier):,} coins")
    embed.add_field(name="Progress",   value=f"`{_bar(multiplier)}`", inline=False)
    embed.set_footer(text="Type  Rcashout  to cash out now!")
    msg = await ctx.send(embed=embed)

    cashout_event = asyncio.Event()

    def check(m):
        return m.author == ctx.author and m.content.lower() == "rcashout" and m.channel == ctx.channel

    async def cashout_listener():
        try:
            await bot.wait_for("message", timeout=25, check=check)
            cashout_event.set()
        except asyncio.TimeoutError: pass

    listener_task = asyncio.create_task(cashout_listener())

    while multiplier < crash_point and not cashout_event.is_set():
        await asyncio.sleep(1)
        multiplier = round(multiplier + random.uniform(0.05, 0.4), 2)
        multiplier = min(multiplier, crash_point)
        embed.set_field_at(1, name="Multiplier", value=f"**{multiplier:.2f}×**")
        embed.set_field_at(2, name="Payout",     value=f"{int(bet * multiplier):,} coins")
        embed.set_field_at(3, name="Progress",   value=f"`{_bar(multiplier)}`", inline=False)
        color = discord.Color.green() if multiplier < crash_point * 0.5 else discord.Color.orange()
        embed.color = color
        try: await msg.edit(embed=embed)
        except Exception: break

    listener_task.cancel()

    if cashout_event.is_set():
        payout = int(bet * multiplier)
        update_balance(ctx.author.id, payout)
        embed.title = f"✅ Cashed out at {multiplier:.2f}×!"
        embed.color = discord.Color.green()
        embed.set_field_at(2, name="Won", value=f"**{payout:,}** coins")
        embed.set_footer(text=f"New balance: {get_balance(ctx.author.id):,} coins")
    else:
        embed.title = f"💥 CRASHED at {crash_point:.2f}×!"
        embed.color = discord.Color.red()
        embed.set_field_at(2, name="Lost", value=f"**{bet:,}** coins")
        embed.set_footer(text=f"Better luck next time · Balance: {get_balance(ctx.author.id):,}")
    embed.remove_field(3)
    await msg.edit(embed=embed)

@bot.command(name="roulette")
@commands.cooldown(1, 5, commands.BucketType.user)
async def roulette_cmd(ctx, amount_str: str, *, bet_type: str):
    if not get_user(ctx.author.id):
        await ctx.send("❌ Use `Rstart` first!"); return
    if amount_str.lower() == "all":
        bet = get_balance(ctx.author.id)
    else:
        bet = parse_amount(amount_str)
    if not bet or bet <= 0:
        await ctx.send("❌ Invalid bet."); return
    if get_balance(ctx.author.id) < bet:
        await ctx.send("❌ Insufficient balance."); return
    bet_type = bet_type.lower().strip()
    spin   = random.randint(0, 36)
    reds   = {1,3,5,7,9,12,14,16,18,19,21,23,25,27,30,32,34,36}
    blacks = set(range(1, 37)) - reds
    is_red   = spin in reds
    is_black = spin in blacks
    is_green = spin == 0
    is_odd   = spin % 2 != 0 and spin != 0
    is_even  = spin % 2 == 0 and spin != 0
    color_emoji = "🔴" if is_red else "⚫" if is_black else "🟢"

    type_map = {
        "red":   (is_red,   2),
        "black": (is_black, 2),
        "green": (is_green, 14),
        "odd":   (is_odd,   2),
        "even":  (is_even,  2),
    }
    if bet_type in type_map:
        win, mult = type_map[bet_type]
    elif bet_type.isdigit() and 0 <= int(bet_type) <= 36:
        win, mult = spin == int(bet_type), 35
    else:
        await ctx.send("❌ Invalid bet type. Use: `red` `black` `green` `odd` `even` or `0-36`.")
        return

    embed = discord.Embed(title="🎡 Roulette")
    embed.add_field(name="Spin Result", value=f"{color_emoji} **{spin}**", inline=True)
    embed.add_field(name="Your Bet",    value=f"`{bet_type}` — {bet:,} coins", inline=True)

    if win:
        payout = bet * mult
        update_balance(ctx.author.id, payout - bet)
        embed.title       = "🎡 Roulette — WIN!"
        embed.color       = discord.Color.green()
        embed.description = f"✅ You won **{payout:,}** coins! ({mult}×)"
    else:
        update_balance(ctx.author.id, -bet)
        embed.title       = "🎡 Roulette — LOSE"
        embed.color       = discord.Color.red()
        embed.description = f"❌ You lost **{bet:,}** coins."
    embed.set_footer(text=f"Balance: {get_balance(ctx.author.id):,} coins")
    await ctx.send(embed=embed)

@bot.command(name="blackjack")
@commands.cooldown(1, 5, commands.BucketType.user)
async def blackjack_cmd(ctx, amount_str: str = None):
    if not amount_str:
        await ctx.send("❌ Usage: `Rblackjack 500`"); return
    if amount_str.lower() == "all":
        bet = get_balance(ctx.author.id)
    else:
        bet = parse_amount(amount_str)
    if not bet or bet <= 0:
        await ctx.send("❌ Invalid bet."); return
    if get_balance(ctx.author.id) < bet:
        await ctx.send("❌ Insufficient coins."); return

    suits  = ["♠", "♥", "♦", "♣"]
    values = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]

    def card(): return (random.choice(values), random.choice(suits))
    def hand_val(h):
        total, aces = 0, 0
        for v, _ in h:
            if v in ["J", "Q", "K"]: total += 10
            elif v == "A":           total += 11; aces += 1
            else:                    total += int(v)
        while total > 21 and aces: total -= 10; aces -= 1
        return total
    def fmt(h): return " ".join(f"`{v}{s}`" for v, s in h)

    player = [card(), card()]
    dealer = [card(), card()]
    update_balance(ctx.author.id, -bet)
    doubled = False

    def make_embed(footer=""):
        e = discord.Embed(title="🃏 Blackjack", color=discord.Color.dark_green())
        e.add_field(name=f"Your Hand ({hand_val(player)})", value=fmt(player), inline=False)
        e.add_field(name="Dealer Shows",                   value=fmt([dealer[0]]), inline=False)
        e.add_field(name="Bet",                            value=f"{bet:,} coins", inline=True)
        if footer: e.set_footer(text=footer)
        return e

    if hand_val(player) == 21:
        while hand_val(dealer) < 17: dealer.append(card())
        payout = int(bet * 2.5)
        update_balance(ctx.author.id, payout)
        e = discord.Embed(title="🃏 Blackjack — NATURAL 21! 🎉", color=discord.Color.gold())
        e.add_field(name="Your Hand", value=f"{fmt(player)} = **21**", inline=False)
        e.add_field(name="Won",       value=f"**{payout:,}** coins (2.5×)", inline=False)
        await ctx.send(embed=e); return

    view = BlackjackView(ctx.author.id)
    msg  = await ctx.send(embed=make_embed("Hit, Stand, or Double Down!"), view=view)

    while hand_val(player) < 21:
        await view.wait()
        if view.action is None:
            break
        if view.action == "hit":
            player.append(card())
            if hand_val(player) >= 21: break
            view = BlackjackView(ctx.author.id)
            await msg.edit(embed=make_embed("Your turn…"), view=view)
        elif view.action == "double":
            if get_balance(ctx.author.id) >= bet:
                update_balance(ctx.author.id, -bet)
                bet *= 2; doubled = True
            player.append(card()); break
        else:
            break

    while hand_val(dealer) < 17: dealer.append(card())
    ps, ds = hand_val(player), hand_val(dealer)

    if ps > 21:         result, color = "💥 Bust! Dealer wins.", discord.Color.red(); win = False
    elif ds > 21:       result, color = "🎉 Dealer busts! You win!", discord.Color.green(); win = True
    elif ps > ds:       result, color = "✅ You win!", discord.Color.green(); win = True
    elif ds > ps:       result, color = "❌ Dealer wins.", discord.Color.red(); win = False
    else:               result, color = "🤝 Push — bet returned.", discord.Color.greyple(); win = None

    if win is True:  update_balance(ctx.author.id, bet * 2)
    elif win is None: update_balance(ctx.author.id, bet)

    final = discord.Embed(title=f"🃏 Blackjack — {result}", color=color)
    final.add_field(name=f"Your Hand ({ps})",   value=fmt(player),      inline=False)
    final.add_field(name=f"Dealer Hand ({ds})",  value=fmt(dealer),      inline=False)
    if doubled: final.add_field(name="🎰 Double Down", value="Applied!", inline=True)
    final.set_footer(text=f"Balance: {get_balance(ctx.author.id):,} coins")
    await msg.edit(embed=final, view=None)

@bot.command(name="higherlower")
@commands.cooldown(1, 10, commands.BucketType.user)
async def higher_lower_cmd(ctx, amount_str: str = None):
    if not amount_str:
        await ctx.send("❌ Usage: `Rhigherlower 200`"); return
    bet = parse_amount(amount_str)
    if not bet or bet <= 0 or get_balance(ctx.author.id) < bet:
        await ctx.send("❌ Invalid bet or insufficient balance."); return

    def cname(v): return {11: "J", 12: "Q", 13: "K", 14: "A"}.get(v, str(v))
    def suit(): return random.choice(["♠", "♥", "♦", "♣"])
    first  = random.randint(2, 14)
    second = random.randint(2, 14)

    class HLView(View):
        def __init__(self):
            super().__init__(timeout=20)
            self.choice = None
        @discord.ui.button(label="⬆️ Higher", style=discord.ButtonStyle.green)
        async def higher(self, i, b):
            if i.user != ctx.author: await i.response.send_message("❌ Not your game!", ephemeral=True); return
            self.choice = "higher"; self.stop(); await i.response.defer()
        @discord.ui.button(label="⬇️ Lower", style=discord.ButtonStyle.red)
        async def lower(self, i, b):
            if i.user != ctx.author: await i.response.send_message("❌ Not your game!", ephemeral=True); return
            self.choice = "lower"; self.stop(); await i.response.defer()

    view  = HLView()
    embed = discord.Embed(
        title="🃏 Higher or Lower?",
        description=f"Card: **{cname(first)}{suit()}** — Will the next card be higher or lower?",
        color=discord.Color.blue(),
    )
    embed.set_footer(text=f"Bet: {bet:,} coins | 20 seconds to decide")
    msg = await ctx.send(embed=embed, view=view)
    await view.wait()

    if not view.choice:
        await msg.edit(content="⏰ Timed out! Bet returned.", embed=None, view=None); return

    result_embed = discord.Embed()
    result_embed.add_field(name="First Card",  value=f"**{cname(first)}{suit()}**",  inline=True)
    result_embed.add_field(name="Second Card", value=f"**{cname(second)}{suit()}**", inline=True)
    if second == first:
        result_embed.title = "🤝 Tie!"
        result_embed.color = discord.Color.greyple()
        result_embed.description = "It's a tie — bet returned."
    elif (view.choice == "higher" and second > first) or (view.choice == "lower" and second < first):
        update_balance(ctx.author.id, bet)
        result_embed.title       = "✅ Correct!"
        result_embed.color       = discord.Color.green()
        result_embed.description = f"Won **{bet * 2:,}** coins!"
    else:
        update_balance(ctx.author.id, -bet)
        result_embed.title       = "❌ Wrong!"
        result_embed.color       = discord.Color.red()
        result_embed.description = f"Lost **{bet:,}** coins."
    result_embed.set_footer(text=f"Balance: {get_balance(ctx.author.id):,} coins")
    await msg.edit(embed=result_embed, view=None)

# ============================================================
#  KOF FIGHT
# ============================================================
KOF_CHARS = {
    "Kyo":    {"hp": 100, "atk": 25, "def": 15, "special": "Dokusai",        "emoji": "🔥"},
    "Terry":  {"hp": 110, "atk": 22, "def": 18, "special": "Power Geyser",   "emoji": "⚡"},
    "Iori":   {"hp": 95,  "atk": 30, "def": 12, "special": "Maiden Masher",  "emoji": "🌑"},
    "Mai":    {"hp": 90,  "atk": 27, "def": 14, "special": "Chou Hissatsu",  "emoji": "🌸"},
    "Ryo":    {"hp": 105, "atk": 23, "def": 20, "special": "Ryuuko Ranbu",   "emoji": "🥋"},
    "Leona":  {"hp": 100, "atk": 26, "def": 16, "special": "V-Slasher",      "emoji": "🗡️"},
    "K'":     {"hp": 98,  "atk": 28, "def": 13, "special": "Chain Drive",    "emoji": "💥"},
    "Athena": {"hp": 88,  "atk": 32, "def": 10, "special": "Psycho Sword",   "emoji": "🔮"},
}

@bot.command(name="kof")
async def kof_fight(ctx, bet_str: str, opponent: discord.Member):
    if not get_user(ctx.author.id) or not get_user(opponent.id):
        await ctx.send("❌ Both players need accounts!"); return
    if opponent == ctx.author:
        await ctx.send("❌ Can't fight yourself!"); return
    bet = parse_amount(bet_str)
    if not bet or bet <= 0:
        await ctx.send("❌ Invalid bet."); return
    if get_balance(ctx.author.id) < bet or get_balance(opponent.id) < bet:
        await ctx.send("❌ One of you can't afford the bet!"); return

    names = list(KOF_CHARS.keys())
    c1, c2 = random.sample(names, 2)
    p1 = dict(KOF_CHARS[c1]); p2 = dict(KOF_CHARS[c2])

    embed = discord.Embed(
        title="🥊 KOF FIGHT!",
        description=(
            f"{KOF_CHARS[c1]['emoji']} **{ctx.author.display_name}** plays **{c1}**\n"
            f"{KOF_CHARS[c2]['emoji']} **{opponent.display_name}** plays **{c2}**\n\n"
            f"Bet: **{bet:,}** coins each"
        ),
        color=discord.Color.orange(),
    )
    fight_msg = await ctx.send(embed=embed)
    await asyncio.sleep(1.5)

    log = []
    winner = loser = wchar = None

    for rnd in range(1, 20):
        dmg1 = max(1, random.randint(p1['atk'] // 2, p1['atk']) - random.randint(0, p2['def'] // 2))
        special1 = random.random() < 0.15
        if special1:
            dmg1 += p1['atk']
            log.append(f"`Rnd {rnd}` {KOF_CHARS[c1]['emoji']} **{c1}** uses **{p1['special']}**! (-{dmg1} HP)")
        else:
            log.append(f"`Rnd {rnd}` {KOF_CHARS[c1]['emoji']} **{c1}** hits **{c2}** for {dmg1} HP")
        p2['hp'] -= dmg1

        if p2['hp'] <= 0:
            winner, loser = ctx.author, opponent; wchar = c1; break

        dmg2 = max(1, random.randint(p2['atk'] // 2, p2['atk']) - random.randint(0, p1['def'] // 2))
        special2 = random.random() < 0.15
        if special2:
            dmg2 += p2['atk']
            log.append(f"`Rnd {rnd}` {KOF_CHARS[c2]['emoji']} **{c2}** uses **{p2['special']}**! (-{dmg2} HP)")
        else:
            log.append(f"`Rnd {rnd}` {KOF_CHARS[c2]['emoji']} **{c2}** hits **{c1}** for {dmg2} HP")
        p1['hp'] -= dmg2

        if p1['hp'] <= 0:
            winner, loser = opponent, ctx.author; wchar = c2; break

        if rnd % 3 == 0:
            hp1 = max(p1['hp'], 0); hp2 = max(p2['hp'], 0)
            bar1 = "🟥" * (hp1 // 10) + "⬛" * (10 - hp1 // 10)
            bar2 = "🟩" * (hp2 // 10) + "⬛" * (10 - hp2 // 10)
            live = discord.Embed(
                title=f"🥊 Round {rnd}",
                description="\n".join(log[-3:]),
                color=discord.Color.orange(),
            )
            live.add_field(name=f"{c1} HP {hp1}", value=f"`{bar1}`", inline=True)
            live.add_field(name=f"{c2} HP {hp2}", value=f"`{bar2}`", inline=True)
            await fight_msg.edit(embed=live)
            await asyncio.sleep(1.2)
    else:
        winner = ctx.author if p1['hp'] > p2['hp'] else opponent
        loser  = opponent if winner == ctx.author else ctx.author
        wchar  = c1 if winner == ctx.author else c2

    update_balance(loser.id, -bet)
    update_balance(winner.id, bet)
    add_xp(winner.id, 25)

    battle_log = "\n".join(log[-5:]) if log else "Lightning-fast fight!"
    final = discord.Embed(
        title=f"🏆 {winner.display_name} WINS!",
        description=f"{KOF_CHARS[wchar]['emoji']} **{wchar}** is victorious!\n+**{bet:,}** coins",
        color=discord.Color.gold(),
    )
    final.add_field(name="Last Rounds", value=battle_log, inline=False)
    final.set_footer(text=f"Winner balance: {get_balance(winner.id):,} coins")
    await fight_msg.edit(embed=final)

# ============================================================
#  TRIVIA
# ============================================================
TRIVIA_QUESTIONS = [
    {"q": "What is the capital of France?",          "a": "Paris",      "opts": ["London", "Berlin", "Paris", "Madrid"]},
    {"q": "How many sides does a hexagon have?",      "a": "6",          "opts": ["5", "6", "7", "8"]},
    {"q": "What planet is closest to the Sun?",       "a": "Mercury",    "opts": ["Venus", "Mercury", "Mars", "Earth"]},
    {"q": "Who painted the Mona Lisa?",               "a": "Da Vinci",   "opts": ["Picasso", "Rembrandt", "Da Vinci", "Monet"]},
    {"q": "What is H2O?",                             "a": "Water",      "opts": ["Oxygen", "Hydrogen", "Water", "Carbon dioxide"]},
    {"q": "How many continents are there?",           "a": "7",          "opts": ["5", "6", "7", "8"]},
    {"q": "What is the largest ocean?",               "a": "Pacific",    "opts": ["Atlantic", "Indian", "Pacific", "Arctic"]},
    {"q": "What gas do plants absorb?",               "a": "CO2",        "opts": ["O2", "N2", "CO2", "H2"]},
    {"q": "How many bones in the human body?",        "a": "206",        "opts": ["196", "206", "216", "226"]},
    {"q": "What is the fastest land animal?",         "a": "Cheetah",    "opts": ["Lion", "Cheetah", "Horse", "Peregrine falcon"]},
    {"q": "Who wrote Romeo and Juliet?",              "a": "Shakespeare","opts": ["Dickens", "Shakespeare", "Austen", "Hemingway"]},
    {"q": "How many days in a leap year?",            "a": "366",        "opts": ["364", "365", "366", "367"]},
    {"q": "What is the chemical symbol for Gold?",    "a": "Au",         "opts": ["Go", "Gd", "Au", "Ag"]},
    {"q": "How many strings does a guitar have?",     "a": "6",          "opts": ["4", "5", "6", "7"]},
    {"q": "What country is the Amazon rainforest in?","a": "Brazil",     "opts": ["Peru", "Colombia", "Brazil", "Venezuela"]},
]

@bot.command(name="trivia")
@commands.cooldown(1, 15, commands.BucketType.user)
async def trivia_cmd(ctx, amount_str: str = "100"):
    if not get_user(ctx.author.id):
        await ctx.send("❌ Use `Rstart` first!"); return
    bet = parse_amount(amount_str)
    if not bet or bet <= 0:
        await ctx.send("❌ Invalid bet."); return
    if get_balance(ctx.author.id) < bet:
        await ctx.send("❌ Not enough coins!"); return

    q       = random.choice(TRIVIA_QUESTIONS)
    opts    = q["opts"][:]
    random.shuffle(opts)
    letters = ["A", "B", "C", "D"]
    answer_letter = letters[opts.index(q["a"])]

    embed = discord.Embed(
        title="🧠 TRIVIA TIME!",
        description=f"**{q['q']}**",
        color=discord.Color.purple(),
    )
    for l, o in zip(letters, opts):
        embed.add_field(name=f"`{l}`", value=o, inline=True)
    embed.set_footer(text=f"Bet: {bet:,} coins · 20 seconds!")

    view = TriviaView(ctx.author.id, answer_letter)
    msg  = await ctx.send(embed=embed, view=view)
    await view.wait()

    if not view.chosen:
        update_balance(ctx.author.id, -(bet // 2))
        await msg.edit(
            content=f"⏰ Time's up! Answer: **{q['a']}** ({answer_letter}). Lost **{bet//2:,}** coins.",
            embed=None, view=None)
        return

    if view.chosen == answer_letter:
        update_balance(ctx.author.id, bet)
        add_xp(ctx.author.id, 20)
        result = discord.Embed(
            title="✅ Correct!",
            description=f"The answer was **{q['a']}** — Won **{bet:,}** coins + 20 XP!",
            color=discord.Color.green(),
        )
    else:
        update_balance(ctx.author.id, -bet)
        result = discord.Embed(
            title="❌ Wrong!",
            description=f"You chose **{view.chosen}**, answer was **{q['a']}** ({answer_letter}) — Lost **{bet:,}** coins.",
            color=discord.Color.red(),
        )
    result.set_footer(text=f"Balance: {get_balance(ctx.author.id):,} coins")
    await msg.edit(embed=result, view=None)

# ============================================================
#  LOTTERY
# ============================================================
@bot.command(name="buy")
async def buy_entries(ctx, number: int):
    if number <= 0: await ctx.send("❌ Buy at least 1 entry!"); return
    cost = number * 10
    if not get_user(ctx.author.id) or get_balance(ctx.author.id) < cost:
        await ctx.send(f"❌ Need **{cost:,}** coins for {number} entries."); return
    if has_item(ctx.author.id, "Lotto Boost"):
        number *= 3
        use_item(ctx.author.id, "Lotto Boost")
        await ctx.send("🎟️ **Lotto Boost** activated — 3× entries!")
    update_balance(ctx.author.id, -cost)
    update_entries(ctx.author.id, number)
    add_to_jackpot(cost)
    c.execute("SELECT count FROM entries WHERE user_id=?", (ctx.author.id,))
    total_entries = c.fetchone()[0]
    embed = discord.Embed(
        title="🎫 Entries Purchased!",
        description=f"Bought **{number}** entries for **{cost:,}** coins.",
        color=discord.Color.purple(),
    )
    embed.add_field(name="Your Total Entries", value=f"**{total_entries}**",           inline=True)
    embed.add_field(name="🎰 Jackpot",          value=f"**{get_jackpot():,}** coins",  inline=True)
    await ctx.send(embed=embed)

@bot.command(name="jackpot")
async def jackpot_cmd(ctx):
    c.execute("SELECT COUNT(*) FROM entries")
    total_entries = c.fetchone()[0]
    c.execute("SELECT COUNT(DISTINCT user_id) FROM entries")
    participants  = c.fetchone()[0]
    embed = discord.Embed(title="🎰 Current Jackpot", color=discord.Color.gold())
    embed.add_field(name="💰 Prize",        value=f"**{get_jackpot():,}** coins",  inline=True)
    embed.add_field(name="🎟️ Entries",       value=f"**{total_entries}**",          inline=True)
    embed.add_field(name="👥 Participants",  value=f"**{participants}**",            inline=True)
    embed.set_footer(text="Use Rbuy <number> to buy entries at 10 coins each!")
    await ctx.send(embed=embed)

@bot.command(name="draw")
@commands.check(is_owner)
async def draw_cmd(ctx):
    c.execute("SELECT user_id, count FROM entries WHERE count>0")
    parts = c.fetchall()
    if not parts: await ctx.send("❌ No entries!"); return
    pool      = [uid for uid, cnt in parts for _ in range(cnt)]
    winner_id = random.choice(pool)
    jp        = get_jackpot()
    if not jp:  await ctx.send("❌ Jackpot is empty!"); return
    update_balance(winner_id, jp)
    winner = await bot.fetch_user(winner_id)
    reset_jackpot(); reset_all_entries()
    embed = discord.Embed(
        title="🎉 LOTTERY WINNER!",
        description=f"{winner.mention} wins **{jp:,}** coins! 🥳",
        color=discord.Color.gold(),
    )
    embed.set_thumbnail(url=winner.display_avatar.url)
    await ctx.send(embed=embed)

# ============================================================
#  PROFILE & SOCIAL
# ============================================================
@bot.command(name="profile", aliases=["prof"])
async def profile_cmd(ctx, member: discord.Member = None):
    target = member or ctx.author
    row    = get_user(target.id)
    if not row:
        await ctx.send(f"❌ **{target.display_name}** has no account. Use `Rstart`."); return

    uid, balance, _, bio, color_hex, rep, xp, level, work_last, rob_last, streak = row
    needed      = level * 100
    rank_label, rank_color = get_rank_tier(level)
    try:    embed_color = discord.Color.from_str(f"#{color_hex}")
    except: embed_color = discord.Color.from_rgb(255, 182, 193)

    c.execute("SELECT COUNT(*)+1 FROM users WHERE balance>(SELECT balance FROM users WHERE user_id=?)", (uid,))
    lb_rank = c.fetchone()[0]

    c.execute(
        "SELECT user2, since FROM marriages WHERE user1=? "
        "UNION SELECT user1, since FROM marriages WHERE user2=?",
        (uid, uid))
    marriage       = c.fetchone()
    spouse_mention = None
    married_since  = None
    if marriage:
        try:
            spouse         = await bot.fetch_user(marriage[0])
            spouse_mention = spouse.mention
            married_since  = marriage[1][:10] if marriage[1] else "?"
        except: spouse_mention = f"<@{marriage[0]}>"

    badges = get_badges(uid)
    work_ready = "✅ Ready" if can_work(uid) else f"⏳ {time_until_work(uid)}"
    rob_ready  = "✅ Ready" if can_rob(uid)  else "⏳ On cooldown"
    flavor     = random.Random(uid).choice(FLAVOR_LINES)

    embed = discord.Embed(color=embed_color)
    embed.set_author(name=f"{target.display_name}  ·  {rank_label}", icon_url=target.display_avatar.url)
    embed.set_thumbnail(url=target.display_avatar.url)

    embed.add_field(name="💰 Balance",
                    value=f"**{balance:,}** coins\n`{coin_bar(balance)}`  #{lb_rank} LB", inline=True)
    embed.add_field(name="🏅 Level & XP",
                    value=f"**Level {level}**\n`{xp_bar(xp, needed)}` {xp}/{needed} XP", inline=True)
    embed.add_field(name="⭐ Rep",  value=f"**{rep}** pts", inline=True)
    embed.add_field(name="🔥 Streak", value=f"**{streak}** day{'s' if streak!=1 else ''}", inline=True)
    embed.add_field(name="💼 Work",   value=work_ready,  inline=True)
    embed.add_field(name="🦹 Rob",    value=rob_ready,   inline=True)

    if spouse_mention:
        embed.add_field(name="💍 Married to", value=f"{spouse_mention}\n*Since {married_since}*", inline=True)
    if badges:
        embed.add_field(name="🎖️ Badges", value="  ".join(badges), inline=not spouse_mention)
    embed.add_field(name="💫 Bio", value=f"*{bio}*", inline=False)
    embed.set_footer(text=f"{flavor}  ·  Rsetbio & Rsetcolor to customise")
    await ctx.send(embed=embed)

@bot.command(name="level", aliases=["rank"])
async def level_cmd(ctx, member: discord.Member = None):
    target = member or ctx.author
    row    = get_user(target.id)
    if not row: await ctx.send("❌ No account."); return
    uid, bal, _, bio, color, rep, xp, level, *_ = row
    needed     = level * 100
    bar        = xp_bar(xp, needed)
    rank_label, rank_color = get_rank_tier(level)
    c.execute("SELECT COUNT(*)+1 FROM users WHERE xp>(SELECT xp FROM users WHERE user_id=?)", (uid,))
    xp_rank = c.fetchone()[0]
    embed = discord.Embed(
        title=f"🏅 {target.display_name}'s Level",
        description=f"**{rank_label}**\n`{bar}` {xp}/{needed} XP",
        color=discord.Color.from_rgb(*[int(f"{rank_color:06x}"[i:i+2], 16) for i in (0, 2, 4)]),
    )
    embed.add_field(name="Level",   value=f"**{level}**",  inline=True)
    embed.add_field(name="XP Rank", value=f"**#{xp_rank}**", inline=True)
    embed.add_field(name="Next Level", value=f"Need **{needed - xp}** more XP", inline=True)
    embed.set_thumbnail(url=target.display_avatar.url)
    await ctx.send(embed=embed)

@bot.command(name="setlevel")
@commands.check(is_owner)
async def setlevel_cmd(ctx, member: discord.Member, level: int):
    if level < 1:
        await ctx.send("❌ Level must be at least 1.")
        return
    if not get_user(member.id):
        await ctx.send(f"❌ {member.mention} doesn't have an account. Use `Rstart` first.")
        return
    c.execute("SELECT xp FROM users WHERE user_id=?", (member.id,))
    current_xp = c.fetchone()[0]
    total_xp_needed = (level - 1) * level * 50
    if total_xp_needed < 0:
        total_xp_needed = 0
    c.execute("UPDATE users SET level=?, xp=? WHERE user_id=?", (level, total_xp_needed, member.id))
    conn.commit()
    rank_label, rank_color = get_rank_tier(level)
    embed = discord.Embed(
        title="👑 Owner Action",
        description=f"Set **{member.display_name}** to **Level {level}** {rank_label}",
        color=discord.Color.gold()
    )
    embed.add_field(name="Previous XP", value=f"{current_xp:,} XP", inline=True)
    embed.add_field(name="New XP", value=f"{total_xp_needed:,} XP", inline=True)
    embed.set_footer(text=f"Action by {ctx.author.display_name}")
    await ctx.send(embed=embed)

@bot.command(name="setxp")
@commands.check(is_owner)
async def setxp_cmd(ctx, member: discord.Member, xp_amount: int):
    if xp_amount < 0:
        await ctx.send("❌ XP cannot be negative.")
        return
    if not get_user(member.id):
        await ctx.send(f"❌ {member.mention} doesn't have an account.")
        return
    c.execute("SELECT level FROM users WHERE user_id=?", (member.id,))
    current_level = c.fetchone()[0]
    c.execute("UPDATE users SET xp=? WHERE user_id=?", (xp_amount, member.id))
    conn.commit()
    temp_xp = xp_amount
    new_level = 1
    while temp_xp >= new_level * 100:
        temp_xp -= new_level * 100
        new_level += 1
    c.execute("UPDATE users SET level=?, xp=? WHERE user_id=?", (new_level, temp_xp, member.id))
    conn.commit()
    embed = discord.Embed(
        title="👑 Owner Action",
        description=f"Set **{member.display_name}** XP to **{xp_amount:,}**",
        color=discord.Color.gold()
    )
    embed.add_field(name="Resulting Level", value=f"**Level {new_level}**", inline=True)
    embed.set_footer(text=f"Action by {ctx.author.display_name}")
    await ctx.send(embed=embed)

@bot.command(name="rep")
@commands.cooldown(1, 86400, commands.BucketType.user)
async def rep_cmd(ctx, member: discord.Member):
    if member == ctx.author:
        await ctx.send("❌ Can't rep yourself."); return
    if not get_user(member.id):
        await ctx.send("❌ That user has no account."); return
    c.execute("UPDATE users SET rep=rep+1 WHERE user_id=?", (member.id,)); conn.commit()
    c.execute("SELECT rep FROM users WHERE user_id=?", (member.id,))
    new_rep = c.fetchone()[0]
    embed = discord.Embed(
        description=f"⭐ {ctx.author.mention} repped {member.mention}! They now have **{new_rep}** rep.",
        color=discord.Color.yellow())
    await ctx.send(embed=embed)

@bot.command(name="setbio")
async def setbio_cmd(ctx, *, bio: str):
    if len(bio) > 200: await ctx.send("❌ Bio too long (max 200 characters)."); return
    if not get_user(ctx.author.id): await ctx.send("❌ No account."); return
    c.execute("UPDATE users SET bio=? WHERE user_id=?", (bio, ctx.author.id)); conn.commit()
    await ctx.send(f"✅ Bio updated!")

@bot.command(name="setcolor")
async def setcolor_cmd(ctx, hex_color: str):
    hex_color = hex_color.lstrip("#").upper()
    if len(hex_color) != 6 or not all(ch in "0123456789ABCDEF" for ch in hex_color):
        await ctx.send("❌ Invalid hex color! Example: `Rsetcolor FF69B4`"); return
    c.execute("UPDATE users SET color=? WHERE user_id=?", (hex_color, ctx.author.id)); conn.commit()
    embed = discord.Embed(description="✅ Profile color updated!", color=discord.Color.from_str(f"#{hex_color}"))
    await ctx.send(embed=embed)

@bot.command(name="topbio")
async def topbio_cmd(ctx):
    if not get_user(ctx.author.id): await ctx.send("❌ Use `Rstart` first."); return
    top  = get_top_balances(10)
    rank = next((i for i, (uid, _) in enumerate(top, 1) if uid == ctx.author.id), None)
    if not rank: await ctx.send("❌ Not in top 10. Keep grinding! 💪"); return
    medals = {1: "👑", 2: "🥈", 3: "🥉"}
    medal  = medals.get(rank, "⭐")
    bio    = f"{medal} Rank #{rank} | {get_balance(ctx.author.id):,} coins"
    c.execute("UPDATE users SET bio=? WHERE user_id=?", (bio, ctx.author.id)); conn.commit()
    await ctx.send(f"✅ Bio updated: **{bio}**")

@bot.command(name="marry")
async def marry_cmd(ctx, member: discord.Member):
    if member == ctx.author: await ctx.send("❌ Can't marry yourself!"); return
    if not get_user(ctx.author.id) or not get_user(member.id):
        await ctx.send("❌ Both need accounts!"); return
    c.execute("SELECT 1 FROM marriages WHERE user1=? OR user2=? OR user1=? OR user2=?",
              (ctx.author.id, ctx.author.id, member.id, member.id))
    if c.fetchone():
        await ctx.send("❌ One of you is already married! Use `Rdivorce` first."); return

    view = ConfirmView(member.id, timeout=60)
    embed = discord.Embed(
        title="💍 Marriage Proposal!",
        description=f"{ctx.author.mention} is proposing to {member.mention}!\n\n{member.mention}, do you accept? 💕",
        color=discord.Color.pink(),
    )
    msg = await ctx.send(embed=embed, view=view)
    await view.wait()
    if view.value:
        c.execute("INSERT INTO marriages VALUES(?,?,?)",
                  (ctx.author.id, member.id, datetime.now().isoformat())); conn.commit()
        embed2 = discord.Embed(
            title="💒 Just Married!",
            description=f"🎊 {ctx.author.mention} & {member.mention} are now married! 💕",
            color=discord.Color.pink())
        await msg.edit(embed=embed2, view=None)
    else:
        await msg.edit(content=f"💔 {member.mention} rejected the proposal...", embed=None, view=None)

@bot.command(name="divorce")
async def divorce_cmd(ctx):
    c.execute("DELETE FROM marriages WHERE user1=? OR user2=?", (ctx.author.id, ctx.author.id))
    if conn.total_changes > 0:
        conn.commit(); await ctx.send(f"💔 {ctx.author.mention} is now divorced.")
    else: await ctx.send("❌ You're not married.")

# ============================================================
#  SHIP
# ============================================================
SONGS = [
    "Perfect — Ed Sheeran", "Love Story — Taylor Swift", "At Last — Etta James",
    "Can't Help Falling in Love — Elvis", "All of Me — John Legend",
    "A Thousand Years — Christina Perri", "Make You Feel My Love — Adele",
    "Die For You — The Weeknd", "Lover — Taylor Swift", "Ocean Eyes — Billie Eilish",
]
DATES = [
    "Stargazing 🌟", "Candlelit dinner 🕯️", "Midnight drive 🚗", "Beach sunset 🌅",
    "Cooking together 👨‍🍳", "Hiking trail 🏔️", "Coffee & bookstore ☕", "Rooftop picnic 🧺",
    "Karaoke night 🎤", "Arcade date 🕹️",
]
TIERS_SHIP = [
    (90, "💘 Soulmate Material",   "#D4537E", "The universe conspired to bring these two together. Rare and beautiful. 🌟"),
    (75, "💖 Deeply Compatible",   "#D4537E", "High emotional connection. Serious long-term potential. 💕"),
    (60, "💕 Good Vibes Only",     "#D85A30", "Solid chemistry and mutual respect. Worth exploring. ✨"),
    (45, "💛 Friendly Sparks",     "#BA7517", "Comfortable connection — could turn into something more. 🌼"),
    (30, "🤍 It's Complicated",    "#888780", "Mixed signals. Communication is key here. 💬"),
    (0,  "💔 Rough Compatibility", "#A32D2D", "The stars aren't aligned... but love is unpredictable. 🌧️"),
]

def make_ship_name(n1, n2):
    h1 = n1[:max(1, len(n1) // 2)]
    h2 = n2[max(1, len(n2) // 2):]
    return (h1 + h2).title()

@bot.command(name="ship")
async def ship_cmd(ctx, member1: discord.Member, member2: discord.Member):
    love = random.randint(1, 100)
    n1, n2 = member1.display_name, member2.display_name
    ship   = make_ship_name(n1, n2)
    bar    = "❤️" * (love // 10) + "🖤" * (10 - love // 10)

    tier_label, tier_color, tier_msg = TIERS_SHIP[-1][1], TIERS_SHIP[-1][2], TIERS_SHIP[-1][3]
    for threshold, label, color, msg in TIERS_SHIP:
        if love >= threshold:
            tier_label, tier_color, tier_msg = label, color, msg; break

    seed = (member1.id + member2.id) % 10000
    rng  = random.Random(seed)
    stats = {
        "Chemistry": rng.randint(max(10, love-20), min(100, love+20)),
        "Trust":     rng.randint(max(10, love-25), min(100, love+15)),
        "Humor":     rng.randint(max(10, love-30), min(100, love+10)),
        "Loyalty":   rng.randint(max(10, love-15), min(100, love+25)),
        "Vibes":     rng.randint(max(10, love-20), min(100, love+20)),
    }
    kids   = rng.randint(0, 5)
    kisses = rng.randint(1, 20)
    date   = rng.choice(DATES)
    song   = rng.choice(SONGS)

    embed = discord.Embed(
        title=f"💞  {n1}  ×  {n2}",
        description=(f"**Ship name:** `{ship}`\n\n`{bar}`\n\n## {love}% match\n### {tier_label}\n*{tier_msg}*"),
        color=discord.Color.from_str(tier_color),
    )
    embed.set_thumbnail(url=member1.display_avatar.url)
    embed.set_image(url=member2.display_avatar.url)
    compat = "\n".join(
        f"`{'█'*(v//10)}{'░'*(10-v//10)}` **{k}** — {v}%"
        for k, v in stats.items()
    )
    embed.add_field(name="📊 Compatibility Breakdown", value=compat, inline=False)
    embed.add_field(name="👶 Future kids",  value=str(kids),   inline=True)
    embed.add_field(name="💋 Kisses/day",   value=str(kisses), inline=True)
    embed.add_field(name="📅 First date",   value=date,        inline=True)
    embed.add_field(name="🎵 Their song",   value=f"*{song}*", inline=False)
    if love >= 90:
        embed.add_field(name="👑 Destiny", value="Written in the stars. Get the rings! 💍", inline=False)
    embed.set_footer(text="Results may vary. Feelings are legally binding. 💌")
    await ctx.send(embed=embed)

# ============================================================
#  RAA AI
# ============================================================
@bot.command(name="ai", aliases=["ask", "ra"])
async def ai_cmd(ctx, *, question: str):
    embed_thinking = discord.Embed(
        description="🤔 **RAA AI** is thinking...",
        color=discord.Color.purple())
    thinking = await ctx.send(embed=embed_thinking)
    answer = await ask_raa_ai(
        question,
        system_message=(
            "You are RAA AI, a helpful and witty assistant for a Discord casino and economy bot. "
            "Keep answers concise (under 600 chars), fun, and relevant to the server. "
            "Use Discord formatting like **bold** and *italics* where appropriate."
        ),
    )
    if len(answer) > 1900: answer = answer[:1900] + "…"
    embed = discord.Embed(title="🤖 RAA AI", description=answer, color=discord.Color.purple())
    embed.set_footer(text=f"Asked by {ctx.author.display_name} · Powered by Groq LLaMA 3.3")
    await thinking.edit(embed=embed)

# ============================================================
#  FUN COMMANDS
# ============================================================
@bot.command(name="8ball")
async def eightball_cmd(ctx, *, q: str):
    responses = [
        ("It is certain ✨",      discord.Color.green()),
        ("Without a doubt 💫",    discord.Color.green()),
        ("Yes definitely 🌟",     discord.Color.green()),
        ("Most likely 💕",        discord.Color.green()),
        ("Ask again later 🍀",    discord.Color.gold()),
        ("Cannot predict now 🌙", discord.Color.gold()),
        ("Don't count on it 💔",  discord.Color.red()),
        ("Very doubtful 🌧️",      discord.Color.red()),
    ]
    answer, color = random.choice(responses)
    embed = discord.Embed(title="🎱 Magic 8-Ball", color=color)
    embed.add_field(name="❓ Question", value=q,      inline=False)
    embed.add_field(name="🎱 Answer",   value=answer, inline=False)
    await ctx.send(embed=embed)

@bot.command(name="roast")
async def roast_cmd(ctx, member: discord.Member = None):
    target = member or ctx.author
    roasts = [
        f"{target.mention}, you're the human version of a participation trophy.",
        f"{target.mention}, I'd agree with you but then we'd both be wrong!",
        f"{target.mention}, you're like a cloud — when you disappear, it's a beautiful day.",
        f"{target.mention}, if brains were dynamite, you couldn't blow your hat off.",
        f"{target.mention}, your Wi-Fi password and personality have the same thing in common — nobody wants them.",
        f"{target.mention}, you're not completely useless. You can always serve as a bad example.",
        f"{target.mention}, I'd call you a clown, but that would be an insult to clowns.",
        f"{target.mention}, even your shadow walks away from you sometimes.",
    ]
    await ctx.send(random.choice(roasts))

@bot.command(name="rate")
async def rate_cmd(ctx, *, thing: str = None):
    target = thing or ctx.author.display_name
    r      = random.randint(1, 100)
    stars  = "⭐" * min(r // 10, 10)
    color  = discord.Color.green() if r >= 70 else discord.Color.gold() if r >= 40 else discord.Color.red()
    embed  = discord.Embed(title=f"📊 Rating: {target[:50]}", color=color)
    embed.add_field(name="Score", value=f"**{r}/100**")
    embed.add_field(name="Stars", value=stars or "💩")
    await ctx.send(embed=embed)

@bot.command(name="howgay")
async def howgay_cmd(ctx, member: discord.Member = None):
    target = member or ctx.author
    p      = random.randint(0, 100)
    bar    = "🌈" * (p // 10) + "⬛" * (10 - p // 10)
    embed  = discord.Embed(
        title=f"🏳️‍🌈 Gay Meter",
        description=f"**{target.display_name}** is **{p}% gay**\n`{bar}`",
        color=discord.Color.from_rgb(255, 105, 180))
    await ctx.send(embed=embed)

@bot.command(name="cat")
async def cat_cmd(ctx):
    facts = [
        "Cats sleep 70% of their lives! 😴",
        "A group of cats is called a clowder 🐈",
        "Cats can't taste sweetness 🍬",
        "A cat's nose print is unique like a fingerprint 🐾",
        "Cats have 32 muscles in each ear 👂",
        "A cat's purr vibrates at 25–50 Hz, the same frequency that promotes bone healing 🦴",
        "Cats can jump up to 6× their body length in a single leap 🐱",
    ]
    embed = discord.Embed(title="🐱 Cat Fact", description=random.choice(facts), color=discord.Color.orange())
    await ctx.send(embed=embed)

@bot.command(name="meme")
async def meme_cmd(ctx):
    async with aiohttp.ClientSession() as s:
        async with s.get("https://meme-api.com/gimme", timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status == 200:
                d = await r.json()
                embed = discord.Embed(title=d["title"], color=discord.Color.orange())
                embed.set_image(url=d["url"])
                embed.set_footer(text=f"r/{d['subreddit']} · 👍 {d['ups']}")
                await ctx.send(embed=embed)
            else:
                await ctx.send("❌ Couldn't fetch a meme right now. Try again!")

@bot.command(name="snipe")
async def snipe_cmd(ctx):
    data = snipe_cache.get(ctx.guild.id)
    if not data or data["channel"] != ctx.channel.id:
        await ctx.send("❌ Nothing to snipe in this channel!"); return
    embed = discord.Embed(description=data["content"] or "*[no text]*", color=discord.Color.red())
    embed.set_author(name=data["author"], icon_url=data.get("avatar", ""))
    embed.set_footer(text=f"Deleted at {data['timestamp'].strftime('%H:%M:%S')}")
    await ctx.send("🔫 **Sniped!**", embed=embed)

@bot.command(name="afk")
async def afk_cmd(ctx, *, reason: str = "AFK"):
    c.execute("INSERT OR REPLACE INTO afk VALUES(?,?,?)",
              (ctx.author.id, reason, datetime.now().isoformat())); conn.commit()
    embed = discord.Embed(description=f"💤 {ctx.author.mention} is now AFK: *{reason}*",
                          color=discord.Color.light_grey())
    await ctx.send(embed=embed)


@bot.command(name="testyt")
async def test_yt(ctx, *, query: str):
    try:
        import yt_dlp
        ydl_opts = {'quiet': True, 'extract_flat': True}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"ytsearch:{query}", download=False)
            if 'entries' in info:
                info = info['entries'][0]
            await ctx.send(f"✅ Found: {info.get('title', 'Unknown')}")
    except Exception as e:
        await ctx.send(f"❌ Error: {e}")

# ============================================================
#  UTILITY COMMANDS
# ============================================================
@bot.command(name="ping")
async def ping_cmd(ctx):
    latency = round(bot.latency * 1000)
    color   = discord.Color.green() if latency < 100 else discord.Color.gold() if latency < 200 else discord.Color.red()
    embed   = discord.Embed(description=f"🏓 Pong! **{latency}ms**", color=color)
    await ctx.send(embed=embed)

@bot.command(name="avatar")
async def avatar_cmd(ctx, member: discord.Member = None):
    t = member or ctx.author
    embed = discord.Embed(title=f"🖼️ {t.display_name}'s Avatar", color=discord.Color.blue())
    embed.set_image(url=t.display_avatar.url)
    embed.add_field(name="PNG",  value=f"[Link]({t.display_avatar.with_format('png').url})",  inline=True)
    embed.add_field(name="WebP", value=f"[Link]({t.display_avatar.with_format('webp').url})", inline=True)
    await ctx.send(embed=embed)

@bot.command(name="userinfo")
async def userinfo_cmd(ctx, member: discord.Member = None):
    t = member or ctx.author
    roles = [r.mention for r in reversed(t.roles[1:])][:8]
    status_map = {
        discord.Status.online: "🟢 Online",
        discord.Status.idle:   "🟡 Idle",
        discord.Status.dnd:    "🔴 DND",
        discord.Status.offline:"⚫ Offline",
    }
    embed = discord.Embed(title=f"ℹ️ {t.display_name}", color=t.color)
    embed.set_thumbnail(url=t.display_avatar.url)
    embed.add_field(name="ID",           value=t.id,                                              inline=True)
    embed.add_field(name="Status",       value=status_map.get(t.status, "❓ Unknown"),             inline=True)
    embed.add_field(name="Bot",          value="✅ Yes" if t.bot else "❌ No",                    inline=True)
    embed.add_field(name="Joined Server",value=discord.utils.format_dt(t.joined_at, "R") if t.joined_at else "?", inline=True)
    embed.add_field(name="Account Age",  value=discord.utils.format_dt(t.created_at, "R"),        inline=True)
    embed.add_field(name="Roles",        value=" ".join(roles) or "None",                          inline=False)
    await ctx.send(embed=embed)

@bot.command(name="serverinfo")
async def serverinfo_cmd(ctx):
    g = ctx.guild
    bots    = sum(1 for m in g.members if m.bot)
    humans  = g.member_count - bots
    embed   = discord.Embed(title=f"ℹ️ {g.name}", color=discord.Color.blue())
    if g.icon: embed.set_thumbnail(url=g.icon.url)
    if g.banner: embed.set_image(url=g.banner.url)
    embed.add_field(name="Owner",     value=g.owner.mention,                           inline=True)
    embed.add_field(name="Members",   value=f"👥 {humans} humans · 🤖 {bots} bots",   inline=True)
    embed.add_field(name="Channels",  value=f"💬 {len(g.text_channels)} text · 🔊 {len(g.voice_channels)} voice", inline=True)
    embed.add_field(name="Roles",     value=str(len(g.roles)),                          inline=True)
    embed.add_field(name="Boosts",    value=f"Level {g.premium_tier} ({g.premium_subscription_count} boosts)", inline=True)
    embed.add_field(name="Created",   value=discord.utils.format_dt(g.created_at, "R"), inline=True)
    await ctx.send(embed=embed)

@bot.command(name="cmt")
async def cmt_cmd(ctx, *, msg: str):
    embed = discord.Embed(description=msg, color=discord.Color.pink())
    embed.set_author(name=ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
    await ctx.message.delete()
    await ctx.send(embed=embed)

@bot.command(name="poll")
async def poll_cmd(ctx, question: str, *options):
    if len(options) < 2:
        await ctx.send('❌ Provide at least 2 options in quotes: `Rpoll "Question" "A" "B"`'); return
    if len(options) > 9:
        await ctx.send("❌ Maximum 9 options."); return
    view  = PollView(list(options))
    embed = discord.Embed(title=f"📊 {question}", color=discord.Color.blue())
    embed.description = "\n".join(
        f"{['1️⃣','2️⃣','3️⃣','4️⃣','5️⃣','6️⃣','7️⃣','8️⃣','9️⃣'][i]} {opt}"
        for i, opt in enumerate(options)
    )
    embed.set_footer(text=f"Poll by {ctx.author.display_name} · Click to vote!")
    await ctx.send(embed=embed, view=view)

@bot.command(name="remind")
async def remind_cmd(ctx, time_str: str, *, message: str):
    unit = time_str[-1].lower()
    try: val = int(time_str[:-1])
    except:
        await ctx.send("❌ Format: `Rremind 30m message` or `2h` or `1d`"); return
    if unit == 'm':   delta = timedelta(minutes=val)
    elif unit == 'h': delta = timedelta(hours=val)
    elif unit == 'd': delta = timedelta(days=val)
    else:
        await ctx.send("❌ Use **m** (minutes), **h** (hours), or **d** (days)."); return
    remind_at = (datetime.now() + delta).isoformat()
    c.execute("INSERT INTO reminders (user_id, channel_id, message, remind_at) VALUES(?,?,?,?)",
              (ctx.author.id, ctx.channel.id, message, remind_at)); conn.commit()
    embed = discord.Embed(
        description=f"⏰ Reminder set for **{val}{unit}**: *{message}*",
        color=discord.Color.teal())
    await ctx.send(embed=embed)

@bot.command(name="giveaway")
@commands.has_permissions(manage_guild=True)
async def giveaway_cmd(ctx, duration_str: str, winners: int, *, prize: str):
    unit = duration_str[-1].lower()
    try: val = int(duration_str[:-1])
    except:
        await ctx.send("❌ Format: `Rgiveaway 1h 1 Prize`"); return
    if unit == 'm':   delta = timedelta(minutes=val)
    elif unit == 'h': delta = timedelta(hours=val)
    elif unit == 'd': delta = timedelta(days=val)
    else: await ctx.send("❌ Use m/h/d."); return

    ends_at = datetime.now() + delta
    view    = GiveawayView(0)

    embed = discord.Embed(title="🎉 GIVEAWAY!", description=f"**Prize:** {prize}", color=discord.Color.gold())
    embed.add_field(name="Winners",   value=str(winners))
    embed.add_field(name="Ends",      value=discord.utils.format_dt(ends_at, "R"))
    embed.add_field(name="Hosted by", value=ctx.author.mention)
    embed.set_footer(text="Click the button to enter!")
    msg = await ctx.send(embed=embed, view=view)

    await asyncio.sleep(delta.total_seconds())
    view.stop()

    if not view.entrants:
        await msg.edit(content="🎉 Giveaway ended — no entries!", embed=None, view=None); return

    chosen   = random.sample(list(view.entrants), min(winners, len(view.entrants)))
    mentions = " ".join(f"<@{uid}>" for uid in chosen)
    end_embed = discord.Embed(
        title="🎉 Giveaway Ended!",
        description=f"**Winner(s):** {mentions}\n**Prize:** {prize}",
        color=discord.Color.gold())
    await msg.edit(embed=end_embed, view=None)
    await ctx.send(f"🎉 Congratulations {mentions}! You won **{prize}**!")

# ============================================================
#  MISC GAMES (guess, rps)
# ============================================================
@bot.command(name="guess")
async def guess_cmd(ctx, guess: int = None):
    if guess is None or not 1 <= guess <= 10:
        await ctx.send("❌ Guess a number 1–10: `Rguess 5`"); return
    secret = random.randint(1, 10)
    if guess == secret:
        update_balance(ctx.author.id, 100)
        await ctx.send(f"🎉 Correct! The number was **{secret}**. +**100 coins**!")
    else:
        await ctx.send(f"😢 Wrong! It was **{secret}**. Better luck next time!")

@bot.command(name="rps")
async def rps_cmd(ctx, choice: str = None):
    if not choice or choice.lower() not in ["rock", "paper", "scissors"]:
        await ctx.send("❌ Usage: `Rrps rock | paper | scissors`"); return
    choice   = choice.lower()
    bot_pick = random.choice(["rock", "paper", "scissors"])
    beats    = {"rock": "scissors", "paper": "rock", "scissors": "paper"}
    icons    = {"rock": "🪨", "paper": "📄", "scissors": "✂️"}
    if choice == bot_pick:
        embed = discord.Embed(title="🤝 Tie!",
                              description=f"We both picked **{icons[choice]} {choice}**.", color=discord.Color.gold())
    elif beats[choice] == bot_pick:
        update_balance(ctx.author.id, 50)
        embed = discord.Embed(title="✅ You Win! +50 coins",
                              description=f"**{icons[choice]} {choice}** beats **{icons[bot_pick]} {bot_pick}**!",
                              color=discord.Color.green())
    else:
        update_balance(ctx.author.id, -30)
        embed = discord.Embed(title="❌ You Lose! -30 coins",
                              description=f"**{icons[bot_pick]} {bot_pick}** beats **{icons[choice]} {choice}**.",
                              color=discord.Color.red())
    await ctx.send(embed=embed)

# ============================================================
#  MODERATION
# ============================================================
@bot.command(name="kick")
@commands.has_permissions(kick_members=True)
async def kick_cmd(ctx, member: discord.Member, *, reason: str = "No reason provided"):
    if member.top_role >= ctx.author.top_role:
        await ctx.send("❌ You can't kick someone with an equal or higher role."); return
    await member.kick(reason=reason)
    embed = discord.Embed(title="👢 Member Kicked",
                          description=f"**{member.display_name}** — {reason}", color=discord.Color.orange())
    await ctx.send(embed=embed)

@bot.command(name="ban")
@commands.has_permissions(ban_members=True)
async def ban_cmd(ctx, member: discord.Member, *, reason: str = "No reason provided"):
    if member.top_role >= ctx.author.top_role:
        await ctx.send("❌ You can't ban someone with an equal or higher role."); return
    await member.ban(reason=reason)
    embed = discord.Embed(title="🔨 Member Banned",
                          description=f"**{member.display_name}** — {reason}", color=discord.Color.red())
    await ctx.send(embed=embed)

@bot.command(name="unban")
@commands.has_permissions(ban_members=True)
async def unban_cmd(ctx, *, name: str):
    bans = [entry async for entry in ctx.guild.bans()]
    for entry in bans:
        if name.lower() in str(entry.user).lower():
            await ctx.guild.unban(entry.user)
            await ctx.send(f"✅ **{entry.user}** has been unbanned."); return
    await ctx.send("❌ User not found in ban list.")

@bot.command(name="mute")
@commands.has_permissions(manage_roles=True)
async def mute_cmd(ctx, member: discord.Member, *, reason: str = "No reason"):
    if member.top_role >= ctx.author.top_role:
        await ctx.send("❌ Can't mute someone with an equal or higher role."); return
    mute_role = discord.utils.get(ctx.guild.roles, name="Muted")
    if not mute_role:
        mute_role = await ctx.guild.create_role(name="Muted", reason="Auto-created by BOBxRAA")
        for ch in ctx.guild.channels:
            await ch.set_permissions(mute_role, send_messages=False, speak=False, add_reactions=False)
    await member.add_roles(mute_role)
    embed = discord.Embed(title="🔇 Member Muted",
                          description=f"**{member.display_name}** — {reason}", color=discord.Color.red())
    await ctx.send(embed=embed)
    try:
        dm_embed = discord.Embed(title=f"🔇 You were muted in {ctx.guild.name}",
                                 description=f"Reason: {reason}", color=discord.Color.red())
        await member.send(embed=dm_embed)
    except Exception: pass

@bot.command(name="unmute")
@commands.has_permissions(manage_roles=True)
async def unmute_cmd(ctx, member: discord.Member):
    mute_role = discord.utils.get(ctx.guild.roles, name="Muted")
    if mute_role and mute_role in member.roles:
        await member.remove_roles(mute_role)
        await ctx.send(f"🔊 **{member.display_name}** has been unmuted.")
    else:
        await ctx.send("❌ That user is not muted.")

@bot.command(name="warn")
@commands.has_permissions(manage_messages=True)
async def warn_cmd(ctx, member: discord.Member, *, reason: str = "No reason"):
    c.execute("INSERT INTO warns (user_id, guild_id, reason, timestamp) VALUES(?,?,?,?)",
              (member.id, ctx.guild.id, reason, datetime.now().isoformat())); conn.commit()
    c.execute("SELECT COUNT(*) FROM warns WHERE user_id=? AND guild_id=?", (member.id, ctx.guild.id))
    total = c.fetchone()[0]
    embed = discord.Embed(
        title="⚠️ Warning Issued",
        description=f"**{member.display_name}** — {reason}",
        color=discord.Color.yellow(),
    )
    embed.add_field(name="Total Warnings", value=f"**{total}**")
    await ctx.send(embed=embed)
    try:
        dm = discord.Embed(
            title=f"⚠️ Warning in {ctx.guild.name}",
            description=f"**Reason:** {reason}\n**Total warns:** {total}",
            color=discord.Color.yellow())
        await member.send(embed=dm)
    except Exception: pass

@bot.command(name="warns")
async def warns_cmd(ctx, member: discord.Member = None):
    target = member or ctx.author
    c.execute("SELECT reason, timestamp FROM warns WHERE user_id=? AND guild_id=? ORDER BY timestamp DESC",
              (target.id, ctx.guild.id))
    rows = c.fetchall()
    if not rows:
        await ctx.send(f"✅ {target.mention} has no warnings."); return
    embed = discord.Embed(title=f"⚠️ Warnings for {target.display_name}", color=discord.Color.yellow())
    for i, (reason, ts) in enumerate(rows[:10], 1):
        embed.add_field(name=f"#{i} — {ts[:10]}", value=reason, inline=False)
    await ctx.send(embed=embed)

@bot.command(name="clearwarns")
@commands.has_permissions(manage_messages=True)
async def clearwarns_cmd(ctx, member: discord.Member):
    c.execute("DELETE FROM warns WHERE user_id=? AND guild_id=?", (member.id, ctx.guild.id)); conn.commit()
    await ctx.send(f"✅ Cleared all warnings for **{member.display_name}**.")

# ============================================================
#  OWNER COMMANDS
# ============================================================
@bot.command(name="m")
@commands.check(is_owner)
async def owner_money(ctx, amount_str: str, target: discord.Member = None):
    amt = parse_amount(amount_str)
    if not amt or amt <= 0: await ctx.send("❌ Invalid amount."); return
    user = target or ctx.author
    if not get_user(user.id): create_user(user.id)
    update_balance(user.id, amt)
    embed = discord.Embed(
        title="👑 Owner Gift",
        description=f"Added **{amt:,}** coins to {user.mention}!",
        color=discord.Color.gold())
    embed.add_field(name="New Balance", value=f"{get_balance(user.id):,} coins")
    await ctx.send(embed=embed)

@bot.command(name="reset")
@commands.check(is_owner)
async def owner_reset(ctx, member: discord.Member):
    view = ConfirmView(ctx.author.id)
    msg  = await ctx.send(f"⚠️ Reset **{member.display_name}**'s account?", view=view)
    await view.wait()
    if view.value:
        c.execute("DELETE FROM users WHERE user_id=?", (member.id,))
        c.execute("DELETE FROM inventory WHERE user_id=?", (member.id,))
        conn.commit()
        await msg.edit(content=f"✅ Account for {member.mention} has been reset.", view=None)
    else:
        await msg.edit(content="❌ Reset cancelled.", view=None)

@bot.command(name="test")
async def test_cmd(ctx):
    latency = round(bot.latency * 1000)
    embed   = discord.Embed(
        title="✅ BOBxRAA Online",
        description=f"**{latency}ms** latency",
        color=discord.Color.green())
    embed.add_field(name="Your Balance", value=f"{get_balance(ctx.author.id):,} coins")
    embed.add_field(name="Version",      value="v4.0")
    await ctx.send(embed=embed)

# ============================================================
#  SHUTDOWN & RUN
# ============================================================
@bot.event
async def on_disconnect():
    conn.close()

if __name__ == "__main__":
    bot.run(TOKEN)
