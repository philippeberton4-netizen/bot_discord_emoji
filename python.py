import os
import json
import discord
import redis
from discord.ext import commands
from discord import app_commands
from dataclasses import dataclass, asdict
from dotenv import load_dotenv


# Connexion à Redis via Railway
redis_client = redis.from_url(os.getenv("REDIS_URL"))

# ---------- Storage helpers (Redis) ----------
def save_data(data: dict):
    """Sauvegarde la config dans Redis."""
    if not redis_client:
        raise RuntimeError("REDIS_URL manquant ou connexion Redis indisponible")
    redis_client.set("ladder_data", json.dumps(data))

def load_data() -> dict | None:
    """Charge la config depuis Redis (ou None si absent)."""
    if not redis_client:
        return None
    raw = redis_client.get("ladder_data")
    return json.loads(raw) if raw else None
# --------------------------------------------

DATA_PATH = os.getenv("LADDER_DATA_PATH", "ladder_data.json")

@dataclass
class LadderConfig:
    ladder_channel_id: int | None = None
    emoji: str = "💪"
    threshold: int = 3
    promoted: dict | None = None  # { original_msg_id: {...infos...} }

    def __post_init__(self):
        if self.promoted is None:
            self.promoted = {}

    @staticmethod
    def load():
        # 1) Essaye d'abord Redis
        data = load_data()
        if data:
            return LadderConfig(**data)

        # 2) Migration douce depuis un ancien fichier JSON s'il existe
        if os.path.exists(DATA_PATH):
            with open(DATA_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            # on pousse dans Redis pour les prochains runs
            save_data(data)
            return LadderConfig(**data)

        # 3) Sinon config par défaut
        return LadderConfig()

    def save(self):
        # Sauvegarde uniquement dans Redis (persistance Railway)
        save_data(asdict(self))



config = LadderConfig.load()

intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree


# ========= UTILITAIRES =========

async def count_reactions(message: discord.Message) -> int:
    """Compte les réactions correspondant à l'émoji configuré sur un message original."""
    for reaction in message.reactions:
        if str(reaction.emoji) == config.emoji:
            return reaction.count
    return 0


def make_embed(msg: discord.Message, count: int) -> discord.Embed:
    """Construit l'embed affiché dans le salon ladder (avatar/pseudo/texte/lien/date + compteur en titre)."""
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
    """Crée ou met à jour le post du ladder + enregistre toutes les infos utiles dans le JSON."""
    ladder_ch = msg.guild.get_channel(config.ladder_channel_id)
    if ladder_ch is None:
        return

    embed = make_embed(msg, count)
    key = str(msg.id)

    if key in config.promoted:
        # Déjà promu → on édite le post ladder existant et on met à jour les champs volatils
        try:
            ladder_msg_id = config.promoted[key]["ladder_msg_id"]
            ladder_msg = await ladder_ch.fetch_message(ladder_msg_id)
            await ladder_msg.edit(embed=embed)
        except discord.NotFound:
            # Le post ladder a été supprimé → on le recrée proprement
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

        # Champs mis à jour (compteur / identité)
        config.promoted[key]["count"] = count
        config.promoted[key]["author_id"] = msg.author.id
        config.promoted[key]["author_name"] = msg.author.display_name
        config.promoted[key]["author_avatar"] = msg.author.display_avatar.url
        # (optionnel) si le message d'origine est édité
        config.promoted[key]["content"] = msg.content
        config.save()
    else:
        # Première promotion
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

    # Si déjà promu, on met à jour le compteur (peut descendre)
    if str(msg.id) in config.promoted:
        count = await count_reactions(msg)
        await post_or_update(msg, count)


# ========= COMMANDES CONFIG =========

@tree.command(description="Configurer le salon ladder")
async def ladder_set_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    if not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message("⛔ Droit requis : Gérer le serveur", ephemeral=True)
    config.ladder_channel_id = channel.id
    config.save()
    await interaction.response.send_message(f"✅ Salon ladder : {channel.mention}", ephemeral=True)


@tree.command(description="Définir le seuil de réactions")
async def ladder_set_threshold(interaction: discord.Interaction, value: int):
    if not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message("⛔ Droit requis : Gérer le serveur", ephemeral=True)
    config.threshold = max(1, int(value))
    config.save()
    await interaction.response.send_message(f"✅ Seuil mis à {config.threshold}", ephemeral=True)


@tree.command(description="Définir l’émoji utilisé pour le ladder")
async def ladder_set_emoji(interaction: discord.Interaction, emoji: str):
    if not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message("⛔ Droit requis : Gérer le serveur", ephemeral=True)
    config.emoji = emoji
    config.save()
    await interaction.response.send_message(f"✅ Émoji mis à {emoji}", ephemeral=True)


@tree.command(description="Voir la config du ladder")
async def ladder_status(interaction: discord.Interaction):
    txt = (
        f"**Salon ladder :** <#{config.ladder_channel_id}>\n"
        f"**Émoji :** {config.emoji}\n"
        f"**Seuil :** {config.threshold}\n"
        f"**Messages promus :** {len(config.promoted)}"
    )
    await interaction.response.send_message(txt, ephemeral=True)


# ========= COMMANDES LADDER =========

@tree.command(description="Affiche le ladder des messages promus (triés par réactions puis ancienneté)")
@app_commands.describe(limit="Nombre maximum de messages à afficher (défaut: 10)")
async def ladder_top(interaction: discord.Interaction, limit: int = 10):
    if not config.promoted:
        return await interaction.response.send_message("Aucun message promu pour le moment.", ephemeral=True)

    # entries: (msg_id, count, timestamp, data)
    entries = [
        (msg_id, int(data.get("count", 0)), float(data.get("timestamp", 0.0)), data)
        for msg_id, data in config.promoted.items()
    ]
    # tri: nb réactions desc, puis ancienneté asc
    entries.sort(key=lambda x: (-x[1], x[2]))
    entries = entries[:limit]

    embed = discord.Embed(
        title=f"🏆 Top {limit} messages les plus {config.emoji}",
        color=discord.Color.gold()
    )

    # miniature = avatar du #1 s'il existe
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
    load_dotenv()
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_TOKEN manquant dans l'environnement.")
    bot.run(token)
