import json
import discord
from ..db import execute, fetchone, fetchall, set_config

def _serialize_overwrites(channel):
    items = []
    for target, ow in channel.overwrites.items():
        allow, deny = ow.pair()
        items.append({
            "id": str(target.id),
            "is_role": isinstance(target, discord.Role),
            "allow": allow.value,
            "deny": deny.value,
        })
    return items

def make_snapshot(guild: discord.Guild):
    roles = []
    for r in guild.roles:
        if r.is_default() or r.managed:
            continue
        roles.append({
            "id": str(r.id),
            "name": r.name,
            "colour": r.colour.value,
            "hoist": r.hoist,
            "mentionable": r.mentionable,
            "permissions": r.permissions.value,
            "position": r.position,
            "members": [str(m.id) for m in r.members],
        })

    channels = []
    for ch in guild.channels:
        item = {
            "id": str(ch.id),
            "name": ch.name,
            "type": ch.type.value,
            "category_id": str(ch.category_id) if ch.category_id else None,
            "position": ch.position,
            "overwrites": _serialize_overwrites(ch),
        }
        if isinstance(ch, discord.TextChannel):
            item.update({
                "topic": ch.topic,
                "nsfw": ch.nsfw,
                "slowmode_delay": ch.slowmode_delay,
            })
        channels.append(item)

    data = json.dumps({"roles": roles, "channels": channels}, ensure_ascii=False)
    execute("INSERT INTO snapshots(kind,guild_id,data) VALUES('full',?,?)", (str(guild.id), data))
    return json.loads(data)

def latest_snapshot(guild_id):
    row = fetchone(
        "SELECT * FROM snapshots WHERE guild_id=? AND kind='full' ORDER BY id DESC LIMIT 1",
        (str(guild_id),)
    )
    return json.loads(row["data"]) if row else None

def _resolve_target(guild, item):
    if item["is_role"]:
        mapped = fetchone("SELECT new_id FROM id_mappings WHERE kind='role' AND old_id=?", (item["id"],))
        rid = int(mapped["new_id"]) if mapped else int(item["id"])
        return guild.get_role(rid)
    return guild.get_member(int(item["id"]))

def _build_overwrites(guild, items):
    out = {}
    for p in items:
        target = _resolve_target(guild, p)
        if target:
            out[target] = discord.PermissionOverwrite.from_pair(
                discord.Permissions(p["allow"]),
                discord.Permissions(p["deny"]),
            )
    return out

async def restore_deleted_role(guild, deleted_role):
    snap = latest_snapshot(guild.id)
    if not snap:
        return None
    original = next((r for r in snap["roles"] if r["id"] == str(deleted_role.id)), None)
    if not original:
        return None

    recreated = await guild.create_role(
        name=original["name"],
        colour=discord.Colour(original["colour"]),
        hoist=original["hoist"],
        mentionable=original["mentionable"],
        permissions=discord.Permissions(original["permissions"]),
        reason="C.C.O. Security Auto-Restore",
    )
    try:
        await recreated.edit(position=min(original["position"], len(guild.roles)-1))
    except Exception:
        pass

    for uid in original["members"]:
        m = guild.get_member(int(uid))
        if m:
            try:
                await m.add_roles(recreated, reason="C.C.O. role restore")
            except Exception:
                pass

    execute("""
        INSERT INTO id_mappings(kind,old_id,new_id) VALUES('role',?,?)
        ON CONFLICT(kind,old_id) DO UPDATE SET new_id=excluded.new_id
    """, (original["id"], str(recreated.id)))

    for row in fetchall("SELECT key,value FROM config WHERE key LIKE 'role_%' OR key LIKE 'rank_role_%'"):
        if row["value"] == original["id"]:
            set_config(row["key"], recreated.id)
    return recreated

async def restore_deleted_channel(guild, deleted_channel):
    snap = latest_snapshot(guild.id)
    if not snap:
        return None
    original = next((c for c in snap["channels"] if c["id"] == str(deleted_channel.id)), None)
    if not original:
        return None

    category = None
    if original["category_id"]:
        mapped = fetchone(
            "SELECT new_id FROM id_mappings WHERE kind='channel' AND old_id=?",
            (original["category_id"],)
        )
        cid = int(mapped["new_id"]) if mapped else int(original["category_id"])
        category = guild.get_channel(cid)

    overwrites = _build_overwrites(guild, original["overwrites"])
    ctype = original["type"]

    if ctype == discord.ChannelType.category.value:
        recreated = await guild.create_category(
            original["name"], overwrites=overwrites, reason="C.C.O. Security Auto-Restore"
        )
    elif ctype == discord.ChannelType.voice.value:
        recreated = await guild.create_voice_channel(
            original["name"], category=category, overwrites=overwrites, reason="C.C.O. Security Auto-Restore"
        )
    else:
        recreated = await guild.create_text_channel(
            original["name"],
            category=category,
            overwrites=overwrites,
            topic=original.get("topic"),
            nsfw=original.get("nsfw", False),
            slowmode_delay=original.get("slowmode_delay", 0),
            reason="C.C.O. Security Auto-Restore",
        )
    try:
        await recreated.edit(position=original["position"])
    except Exception:
        pass

    execute("""
        INSERT INTO id_mappings(kind,old_id,new_id) VALUES('channel',?,?)
        ON CONFLICT(kind,old_id) DO UPDATE SET new_id=excluded.new_id
    """, (original["id"], str(recreated.id)))

    for row in fetchall(
        "SELECT key,value FROM config WHERE key LIKE '%channel%' OR key LIKE 'log_%' OR key='ticket_category'"
    ):
        if row["value"] == original["id"]:
            set_config(row["key"], recreated.id)
    return recreated
