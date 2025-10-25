import os
import json
import discord
from discord.ext import commands
from discord import app_commands
from dataclasses import dataclass, asdict

DATA_PATH = "ladder_data.json"

@dataclass
class LadderConfig:
    ladder_channel_id: int | None = None
    emoji: str = "💪"
    threshold: int = 3
    promoted: dict | None = None  # { original_msg_id: {...infos...} }
    admin_role_id: int | None = None  # <-- rôle requis pour les commandes protégées

    def __post_init__(self):
        if self.promoted is None:
            self.promoted = {}

    @staticmethod
    def load():
        if os.path.exists(DATA_PATH):
            with open(DATA_PATH, "r", encoding="utf-8") as f:
                return LadderConfig(**json.load(f))
        return LadderConfig()

    def save(self):
        with open(DATA_PATH, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, ensure_ascii=False, indent=2)

config = LadderConfig.load()

# ---- Discord bot & intents ----
intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
intents.members = True  # <-- pour lire les rôles des membres
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# ---- Check utilitaire : rôle requis ----
def require_admin():
    async def predicate(inter: discord.Interaction) -> bool:
        # Les admins serveur gardent toujours l'accès
        if inter.user.guild_permissions.manage_guild:
            return True
        # Rôle configuré ?
        rid = config.admin_role_id
        if rid and hasattr(inter.user, "roles"):
            if any(r.id == rid for r in inter.user.roles):
                return True
        raise app_commands.CheckFailure("Missing ladder admin role")
    return app_commands.check(predicate)

# ---- Gestion propre des erreurs de /slash ----
@tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        msg = "⛔ Vous n'avez pas le rôle requis pour utiliser cette commande."
        if not interaction.response.is_done():
            await interaction.response.send_message(msg, ephemeral=True)
        else:
            await interaction.followup.send(msg, ephemeral=True)

# ========= UTILITAIRES =========

async def count_reactions(message: discord.Message) -> int:
    for reaction in message.reactions:
        if str(reaction.emoji) == config.emoji:
            return reaction.count
    return 0

def make_embed(msg: discord.Message, count: int) -> discord.Embed:
    embed = discord.Embed(color=discord.Color.dark_grey())
    embed.description = f"{msg.content or '*—*'}\n\n[Aller au message]({msg.jump_url})"
    embed.set_author(name=msg.author.display_name, icon_url=msg.author.display_avatar.url)
    embed.set_footer(text=msg.created_at.strftime("%d/%m/%Y %H:%M"))
    if msg.attachments:
        att = msg.attachments[0]
        if att.content_type and att.content_type.startswith(("image/", "video/")):
            embed.set_image(url=att.url)
    embed.title = f"{config.emoji} **{count}** | {msg.channel.mention}"
    return embed

async def post_or_update(msg: discord.Message, count: int):
    ladder_ch = msg.guild.get_channel(config.ladder_channel_id)
    if ladder_ch is None:
        return

    embed = make_embed(msg, count)
    key = str(msg.id)

    if key in config.promoted:
        try:
            ladder_msg_id = config.promoted[key]["ladder_msg_id"]
            ladder_msg = await ladder_ch.fetch_message(ladder_msg_id)
            await ladder_msg.edit(embed=embed)
        except discord.NotFound:
            sent = await ladder_ch.send(embed=embed)
            config.promoted[key] = {
                "ladder_msg_id": sent.id,
                "author_id": msg.author.id,
                "author_name": msg.author.display_name,
                "author_avatar": msg.author.display_avatar.url,
                "content": msg.content,
                "url": msg.jump_url,
                "timestamp": msg.created_at.timestamp(),
                "count": count,
                "channel_id": msg.channel.id
            }
            config.save()
            return

        config.promoted[key]["count"] = count
        config.promoted[key]["author_id"] = msg.author.id
        config.promoted[key]["author_name"] = msg.author.display_name
        config.promoted[key]["author_avatar"] = msg.author.display_avatar.url
        config.promoted[key]["content"] = msg.content
        config.save()
    else:
        sent = await ladder_ch.send(embed=embed)
        config.promoted[key] = {
            "ladder_msg_id": sent.id,
            "author_id": msg.author.id,
            "author_name": msg.author.display_name,
            "author_avatar": msg.author.display_avatar.url,
            "content": msg.content,
            "url": msg.jump_url,
            "timestamp": msg.created_at.timestamp(),
            "count": count,
            "channel_id": msg.channel.id
        }
        config.save()

# ========= ÉVÉNEMENTS =========

@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if bot.user and payload.user_id == bot.user.id:
        return
    if str(payload.emoji) != config.emoji:
        return

    guild = bot.get_guild(payload.guild_id)
    if guild is None:
        return
    channel = guild.get_channel(payload.channel_id) or await bot.fetch_channel(payload.channel_id)
    msg = await channel.fetch_message(payload.message_id)

    count = await count_reactions(msg)
    if count >= config.threshold:
        await post_or_update(msg, count)

@bot.event
async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
    if str(payload.emoji) != config.emoji:
        return

    guild = bot.get_guild(payload.guild_id)
    if guild is None:
        return
    channel = guild.get_channel(payload.channel_id) or await bot.fetch_channel(payload.channel_id)
    msg = await channel.fetch_message(payload.message_id)

    if str(msg.id) in config.promoted:
        count = await count_reactions(msg)
        await post_or_update(msg, count)

# ========= COMMANDES CONFIG =========

@tree.command(description="Définir le rôle requis pour administrer le ladder")
@app_commands.describe(role="Rôle autorisé à configurer le ladder")
@require_admin()  # <-- protège aussi cette commande (les admins serveur passent)
async def ladder_set_admin_role(interaction: discord.Interaction, role: discord.Role):
    if not interaction.user.guild_permissions.manage_guild:
        # On exige qu'un vrai admin serveur pose ce rôle la première fois
        return await interaction.response.send_message("⛔ Droit requis : Gérer le serveur", ephemeral=True)
    config.admin_role_id = role.id
    config.save()
    await interaction.response.send_message(f"✅ Rôle admin ladder défini : {role.mention}", ephemeral=True)

@tree.command(description="Configurer le salon ladder")
@require_admin()
async def ladder_set_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    config.ladder_channel_id = channel.id
    config.save()
    await interaction.response.send_message(f"✅ Salon ladder : {channel.mention}", ephemeral=True)

@tree.command(description="Définir le seuil de réactions")
@require_admin()
async def ladder_set_threshold(interaction: discord.Interaction, value: int):
    config.threshold = max(1, int(value))
    config.save()
    await interaction.response.send_message(f"✅ Seuil mis à {config.threshold}", ephemeral=True)

@tree.command(description="Définir l’émoji utilisé pour le ladder")
@require_admin()
async def ladder_set_emoji(interaction: discord.Interaction, emoji: str):
    config.emoji = emoji
    config.save()
    await interaction.response.send_message(f"✅ Émoji mis à {emoji}", ephemeral=True)

@tree.command(description="Voir la config du ladder")
async def ladder_status(interaction: discord.Interaction):
    admin_role_txt = f"<@&{config.admin_role_id}>" if config.admin_role_id else "*non défini*"
    txt = (
        f"**Salon ladder :** <#{config.ladder_channel_id}>\n"
        f"**Émoji :** {config.emoji}\n"
        f"**Seuil :** {config.threshold}\n"
        f"**Rôle admin :** {admin_role_txt}\n"
        f"**Messages promus :** {len(config.promoted)}"
    )
    await interaction.response.send_message(txt, ephemeral=True)

# ========= COMMANDES LADDER =========

@tree.command(description="Affiche le ladder des messages promus (triés par réactions puis ancienneté)")
@app_commands.describe(limit="Nombre maximum de messages à afficher (défaut: 10)")
async def ladder_top(interaction: discord.Interaction, limit: int = 10):
    if not config.promoted:
        return await interaction.response.send_message("Aucun message promu pour le moment.", ephemeral=True)

    entries = [
        (msg_id, int(data.get("count", 0)), float(data.get("timestamp", 0.0)), data)
        for msg_id, data in config.promoted.items()
    ]
    entries.sort(key=lambda x: (-x[1], x[2]))
    entries = entries[:limit]

    embed = discord.Embed(
        title=f"🏆 Top {limit} messages les plus {config.emoji}",
        color=discord.Color.gold()
    )
    if entries and entries[0][3].get("author_avatar"):
        embed.set_thumbnail(url=entries[0][3]["author_avatar"])

    for rank, (_, count, _, data) in enumerate(entries, start=1):
        author = data.get("author_name", "Inconnu")
        content = (data.get("content") or "*—*").strip()
        url     = data.get("url", "")
        field_name  = f"#{rank} — {config.emoji} **{count}** — par **{author}**"
        field_value = f"> {content[:200]}{'…' if len(content) > 200 else ''}\n[🔗 Lien vers le message]({url})"
        embed.add_field(name=field_name, value=field_value, inline=False)

    await interaction.response.send_message(embed=embed)

@tree.command(description="Classement des auteurs (1 point = 1 réaction sur ses messages promus)")
@app_commands.describe(limit="Nombre maximum d'auteurs à afficher (défaut: 10)")
async def ladder_top_joueur(interaction: discord.Interaction, limit: int = 10):
    if not config.promoted:
        return await interaction.response.send_message("Aucun message promu pour le moment.", ephemeral=True)

    by_author: dict = {}
    for _msg_id, data in config.promoted.items():
        count = int(data.get("count", 0))
        author_id = data.get("author_id")
        author_name = data.get("author_name", "Inconnu")
        author_avatar = data.get("author_avatar", None)
        ts = float(data.get("timestamp", 0.0))

        key = author_id if isinstance(author_id, int) or (isinstance(author_id, str) and author_id.isdigit()) else f"name::{author_name}"
        entry = by_author.setdefault(key, {
            "points": 0, "msgs": 0, "author_name": author_name,
            "author_avatar": author_avatar, "first_ts": ts, "best_single": 0
        })
        entry["points"] += count
        entry["msgs"] += 1
        entry["author_name"] = author_name
        if author_avatar:
            entry["author_avatar"] = author_avatar
        entry["first_ts"] = min(entry["first_ts"], ts)
        entry["best_single"] = max(entry["best_single"], count)

    leaderboard = sorted(
        by_author.items(),
        key=lambda kv: (-kv[1]["points"], -kv[1]["best_single"], kv[1]["first_ts"])
    )[:limit]

    embed = discord.Embed(
        title=f"🏆 Top {limit} joueurs — ladder {config.emoji}",
        color=discord.Color.blurple()
    )
    if leaderboard and leaderboard[0][1].get("author_avatar"):
        embed.set_thumbnail(url=leaderboard[0][1]["author_avatar"])

    for rank, (akey, info) in enumerate(leaderboard, start=1):
        name = info["author_name"]; pts = info["points"]; nmsg = info["msgs"]
        mention = ""
        try:
            uid = int(akey) if not isinstance(akey, int) else akey
            mention = f" (<@{uid}>)"
        except Exception:
            pass
        embed.add_field(
            name=f"**#{rank}** — {name}{mention}",
            value=f"**{pts}** points • {nmsg} message{'s' if nmsg > 1 else ''} • meilleur post: {info['best_single']} {config.emoji}",
            inline=False
        )
    await interaction.response.send_message(embed=embed)

# ========= READY =========

@bot.event
async def on_ready():
    try:
        await tree.sync()
        print("Slash commands synchronisés ✅")
    except Exception as e:
        print("Erreur sync :", e)
    print(f"✅ Connecté en tant que {bot.user}")

# ========= MAIN =========

if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_TOKEN manquant dans l'environnement.")
    bot.run(token)