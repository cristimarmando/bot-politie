import asyncio
import json
import time
from collections import defaultdict, deque
from datetime import timedelta

import discord

from ..db import (
    audit,
    fetchall,
    fetchone,
    get_config,
    get_member_protection_snapshot,
    get_security,
    is_protected_user,
    is_superuser,
    save_member_protection_snapshot,
)
from ..utils import owner_id
from .snapshot_service import restore_deleted_channel, restore_deleted_role

counters = defaultdict(deque)
join_events = deque()
spam_events = defaultdict(deque)


def _bool(key, default="0"):
    return get_security(key, default) == "1"


def is_whitelisted(user_id, scope="all"):
    return fetchone(
        """
        SELECT 1 FROM security_whitelist
        WHERE user_id=? AND (scope='all' OR scope=?)
        """,
        (str(user_id), scope),
    ) is not None


def _hit(actor_id, key, window_sec):
    q = counters[f"{actor_id}:{key}"]
    now = time.time()
    while q and now - q[0] > window_sec:
        q.popleft()
    q.append(now)
    return len(q)


async def log(guild, text, key="log_security"):
    cid = get_config(key)
    if not cid:
        return
    ch = guild.get_channel(int(cid))
    if ch and ch.is_text_based():
        try:
            await ch.send(text)
        except Exception:
            pass


def actor_exempt(guild, actor_id, scope, *, include_superusers=True):
    if not actor_id:
        return True
    if guild.me and int(actor_id) == guild.me.id:
        return True
    if int(actor_id) == guild.owner_id or int(actor_id) == owner_id():
        return True
    if include_superusers and is_superuser(actor_id):
        return True
    return is_whitelisted(actor_id, scope)


def protected_role_ids():
    ids = set()
    keys = ["role_cco", "role_leadership", "role_tester", "role_head_tester", "role_inactive",
            "role_av_1_1", "role_fw_1_3", "role_fw_2_3", "role_fw_3_3"]
    keys += [
        "rank_role_detectiv_stagiar", "rank_role_detectiv", "rank_role_investigator_operativ",
        "rank_role_investigator_principal", "rank_role_ofiter_coordonator_caz",
        "rank_role_sef_serviciu_investigatii", "rank_role_coordonator_operational",
        "rank_role_comandant_operational", "rank_role_director_adjunct", "rank_role_director_general",
    ]
    for k in keys:
        value = get_config(k)
        if value:
            try:
                ids.add(int(value))
            except (TypeError, ValueError):
                pass
    return ids


def dangerous_permissions(perms: discord.Permissions):
    return {
        "administrator": perms.administrator,
        "manage_guild": perms.manage_guild,
        "manage_roles": perms.manage_roles,
        "manage_channels": perms.manage_channels,
        "ban_members": perms.ban_members,
        "kick_members": perms.kick_members,
        "manage_webhooks": perms.manage_webhooks,
        "moderate_members": perms.moderate_members,
    }


async def strip_editable_roles(member, reason):
    removable = [
        r for r in member.roles
        if not r.is_default() and not r.managed and r < member.guild.me.top_role
    ]
    if removable:
        try:
            await member.remove_roles(*removable, reason=reason)
        except Exception:
            pass


async def punish(guild, actor_id, reason, scope):
    if actor_exempt(guild, actor_id, scope):
        return False
    member = guild.get_member(int(actor_id))
    if not member:
        return False
    action = get_security("dangerous_action", "strip_roles")
    try:
        if action == "kick":
            await member.kick(reason=reason)
        elif action == "ban":
            await guild.ban(member, reason=reason)
        else:
            await strip_editable_roles(member, reason)
        audit("security_punish", actor_id, None, f"{action}: {reason}")
        await log(guild, f"🛡️ <@{actor_id}> — **{action}** — {reason}")
        return True
    except Exception as e:
        await log(guild, f"⚠️ Nu am putut sancționa <@{actor_id}>: `{e}`")
        return False


async def audit_entry(guild, action, target_id=None, max_age=12):
    await asyncio.sleep(0.8)
    try:
        async for entry in guild.audit_logs(limit=12, action=action):
            if target_id and getattr(entry.target, "id", None) != int(target_id):
                continue
            age = (discord.utils.utcnow() - entry.created_at).total_seconds()
            if age <= max_age:
                return entry
    except Exception:
        return None
    return None


async def capture_member_state(member: discord.Member):
    if member.bot:
        return
    save_member_protection_snapshot(
        member.id,
        member.nick,
        [r.id for r in member.roles if not r.is_default()],
    )


async def capture_all_member_states(guild: discord.Guild):
    for member in guild.members:
        if not member.bot:
            await capture_member_state(member)


async def restore_snapshot_roles(member: discord.Member, reason="C.C.O. Role Restore Snapshot"):
    snap = get_member_protection_snapshot(member.id)
    if not snap:
        return False
    try:
        wanted_ids = {int(x) for x in json.loads(snap["roles_json"] or "[]")}
    except Exception:
        return False
    current = {r.id: r for r in member.roles if not r.is_default() and not r.managed}
    wanted_roles = [member.guild.get_role(rid) for rid in wanted_ids]
    wanted_roles = [r for r in wanted_roles if r and r < member.guild.me.top_role and not r.managed]
    to_add = [r for r in wanted_roles if r.id not in current]
    to_remove = [r for rid, r in current.items() if rid not in wanted_ids and r < member.guild.me.top_role]
    try:
        if to_add:
            await member.add_roles(*to_add, reason=reason)
        if to_remove:
            await member.remove_roles(*to_remove, reason=reason)
        return True
    except Exception:
        return False


async def on_channel_delete(channel):
    if not _bool("antinuke_enabled", "1"):
        return
    entry = await audit_entry(channel.guild, discord.AuditLogAction.channel_delete, channel.id)
    actor = entry.user.id if entry and entry.user else None
    unauthorized = actor and not actor_exempt(channel.guild, actor, "channel_delete")

    if unauthorized:
        count = _hit(actor, "channel_delete", int(get_security("channel_delete_window_sec", "15")))
        limit = int(get_security("channel_delete_limit", "3"))
        if count >= limit:
            await punish(channel.guild, actor, "Anti-Nuke: ștergeri multiple de canale", "channel_delete")

    if unauthorized and _bool("restore_channels", "1"):
        restored = await restore_deleted_channel(channel.guild, channel)
        if restored:
            audit("security_channel_restore", actor, restored.id, channel.name)
            await log(channel.guild, f"♻️ Canal restaurat: **{restored.name}** | autor: <@{actor}>")


async def on_role_delete(role):
    if not _bool("antinuke_enabled", "1"):
        return
    entry = await audit_entry(role.guild, discord.AuditLogAction.role_delete, role.id)
    actor = entry.user.id if entry and entry.user else None
    unauthorized = actor and not actor_exempt(role.guild, actor, "role_delete")

    if unauthorized:
        count = _hit(actor, "role_delete", int(get_security("role_delete_window_sec", "15")))
        limit = int(get_security("role_delete_limit", "2"))
        if count >= limit:
            await punish(role.guild, actor, "Anti-Nuke: ștergeri multiple de roluri", "role_delete")

    if unauthorized and _bool("restore_roles", "1"):
        restored = await restore_deleted_role(role.guild, role)
        if restored:
            audit("security_role_restore", actor, restored.id, role.name)
            await log(role.guild, f"♻️ Rol restaurat: **{restored.name}** | autor: <@{actor}>")


async def on_role_update(before: discord.Role, after: discord.Role):
    guild = after.guild
    if guild.me and after.id == guild.me.top_role.id:
        return

    entry = await audit_entry(guild, discord.AuditLogAction.role_update, after.id)
    actor = entry.user.id if entry and entry.user else None
    if not actor or actor_exempt(guild, actor, "role_update"):
        return

    changed = []
    if before.name != after.name:
        changed.append(f"name `{before.name}` → `{after.name}`")
    if before.permissions.value != after.permissions.value:
        changed.append("permissions")
    if before.colour.value != after.colour.value:
        changed.append("colour")
    if before.hoist != after.hoist:
        changed.append("hoist")
    if before.mentionable != after.mentionable:
        changed.append("mentionable")

    is_protected = after.id in protected_role_ids()
    gained_dangerous = False
    before_d = dangerous_permissions(before.permissions)
    after_d = dangerous_permissions(after.permissions)
    for key, enabled in after_d.items():
        if enabled and not before_d.get(key):
            gained_dangerous = True
            break

    must_restore = (
        (_bool("role_lock_enabled", "1") and is_protected and bool(changed))
        or (_bool("role_permission_guard", "1") and gained_dangerous)
    )
    if not must_restore:
        return

    try:
        await after.edit(
            name=before.name,
            permissions=before.permissions,
            colour=before.colour,
            hoist=before.hoist,
            mentionable=before.mentionable,
            reason="C.C.O. Role Lock / Permission Guard",
        )
        audit("security_role_update_restore", actor, after.id, ", ".join(changed))
        await log(
            guild,
            f"🔒 **Role Guard** — rolul **{before.name}** a fost restaurat.\n"
            f"Autor modificare: <@{actor}>\nModificări detectate: {', '.join(changed) or 'permisiuni periculoase'}",
            "log_roles",
        )
    except Exception as e:
        await log(guild, f"⚠️ Role Guard nu a putut restaura **{after.name}**: `{e}`", "log_roles")


async def on_member_join(member):
    if member.bot:
        entry = await audit_entry(member.guild, discord.AuditLogAction.bot_add, member.id)
        actor = entry.user.id if entry and entry.user else None
        if actor and not actor_exempt(member.guild, actor, "bot_add"):
            try:
                await member.kick(reason="Bot neautorizat")
            except Exception:
                pass
            await punish(member.guild, actor, "Anti Bot-Add", "bot_add")
            return

    if _bool("antiraid_enabled", "1") and not member.bot:
        now = time.time()
        window = int(get_security("antiraid_window_sec", "15"))
        while join_events and now - join_events[0] > window:
            join_events.popleft()
        join_events.append(now)
        if len(join_events) >= int(get_security("antiraid_joins", "10")):
            await log(member.guild, f"🚨 Posibil RAID: **{len(join_events)}** intrări în **{window}s**.")

    min_days = int(get_security("min_account_age_days", "0"))
    if min_days > 0 and not member.bot:
        age_days = (discord.utils.utcnow() - member.created_at).total_seconds() / 86400
        if age_days < min_days:
            rid = get_config("role_unverified")
            if rid and (role := member.guild.get_role(int(rid))):
                try:
                    await member.add_roles(role, reason="Cont nou")
                except Exception:
                    pass
            await log(member.guild, f"⚠️ Cont nou: {member.mention} — {age_days:.1f} zile.")

    await capture_member_state(member)


async def on_member_remove(member):
    if not _bool("antinuke_enabled", "1"):
        return
    entry = await audit_entry(member.guild, discord.AuditLogAction.kick, member.id)
    if not entry or not entry.user:
        return
    actor = entry.user.id

    if _bool("protected_users_enabled", "1") and is_protected_user(member.id) and not actor_exempt(member.guild, actor, "protected_user_kick"):
        audit("security_protected_user_kick", actor, member.id, "kick detected")
        await log(member.guild, f"🚨 **Protected User** — <@{actor}> l-a dat kick pe utilizatorul protejat <@{member.id}>.")
        await punish(member.guild, actor, "A încercat să elimine un utilizator protejat", "protected_user_kick")
        return

    if actor_exempt(member.guild, actor, "mass_kick"):
        return
    count = _hit(actor, "mass_kick", int(get_security("mass_kick_window_sec", "30")))
    if count >= int(get_security("mass_kick_limit", "5")):
        await punish(member.guild, actor, "Anti-Nuke: mass kick", "mass_kick")


async def on_ban_add(ban):
    if not _bool("antinuke_enabled", "1"):
        return
    entry = await audit_entry(ban.guild, discord.AuditLogAction.ban, ban.user.id)
    if not entry or not entry.user:
        return
    actor = entry.user.id

    if _bool("protected_users_enabled", "1") and is_protected_user(ban.user.id) and not actor_exempt(ban.guild, actor, "protected_user_ban"):
        try:
            await ban.guild.unban(ban.user, reason="C.C.O. Protected User Guard")
        except Exception:
            pass
        audit("security_protected_user_unban", actor, ban.user.id, "ban reverted")
        await log(ban.guild, f"🚨 **Protected User** — ban-ul pentru <@{ban.user.id}> a fost anulat. Autor: <@{actor}>.")
        await punish(ban.guild, actor, "A încercat să baneze un utilizator protejat", "protected_user_ban")
        return

    if actor_exempt(ban.guild, actor, "mass_ban"):
        return
    count = _hit(actor, "mass_ban", int(get_security("mass_ban_window_sec", "20")))
    if count >= int(get_security("mass_ban_limit", "3")):
        await punish(ban.guild, actor, "Anti-Nuke: mass ban", "mass_ban")


async def on_webhooks_update(channel):
    entry = await audit_entry(channel.guild, discord.AuditLogAction.webhook_create)
    if not entry or not entry.user:
        return
    actor = entry.user.id
    if actor_exempt(channel.guild, actor, "webhook"):
        return
    count = _hit(actor, "webhook", int(get_security("webhook_window_sec", "60")))
    if count >= int(get_security("webhook_create_limit", "3")):
        await punish(channel.guild, actor, "Webhook abuse", "webhook")


async def _restore_member_nickname(old: discord.Member, new: discord.Member, actor):
    row = fetchone("SELECT * FROM members WHERE discord_id=? AND status IN ('active','inactive','suspended')", (str(new.id),))
    if not row:
        return False
    if row["callsign"]:
        wanted = f"[{row['callsign']}] {row['first_name']} {row['last_name']} | {row['internal_id']}"[:32]
    else:
        wanted = f"{row['first_name']} {row['last_name']} | {row['internal_id']}"[:32]
    if new.nick == wanted:
        return False
    try:
        changed_to = new.nick or new.name
        await new.edit(nick=wanted, reason="C.C.O. Nickname Guard Auto-Restore")
        audit("security_nickname_restore", actor, new.id, f"{changed_to} -> {wanted}")
        await log(
            new.guild,
            f"🪪 **Nickname Guard**\nMembru: {new.mention}\n"
            f"Modificat în: `{changed_to}`\nRestaurat la: `{wanted}`\n"
            f"Autor modificare: {f'<@{actor}>' if actor else '`necunoscut`'}",
            "log_roles",
        )
        return True
    except Exception as e:
        await log(new.guild, f"⚠️ Nickname Guard nu a putut restaura {new.mention}: `{e}`", "log_roles")
        return False


async def _restore_protected_member_roles(old: discord.Member, new: discord.Member, actor):
    protected = protected_role_ids()
    old_ids = {r.id for r in old.roles}
    new_ids = {r.id for r in new.roles}
    changed_protected = (old_ids ^ new_ids) & protected
    if not changed_protected:
        return False

    # Owner/Superuser pot administra prin bot/panel, dar modificarea directă Discord este tot restaurată.
    # Doar botul sau whitelist-ul explicit protected_roles_manual are voie să ocolească.
    if new.guild.me and actor == new.guild.me.id:
        return False
    if actor and is_whitelisted(actor, "protected_roles_manual"):
        return False

    added = [new.guild.get_role(rid) for rid in (new_ids - old_ids) & protected]
    removed = [new.guild.get_role(rid) for rid in (old_ids - new_ids) & protected]
    added = [r for r in added if r and r < new.guild.me.top_role]
    removed = [r for r in removed if r and r < new.guild.me.top_role]
    try:
        if added:
            await new.remove_roles(*added, reason="C.C.O. Protected Rank Roles")
        if removed:
            await new.add_roles(*removed, reason="C.C.O. Protected Rank Roles")
        audit(
            "security_protected_roles_restore",
            actor,
            new.id,
            f"added={[r.name for r in added]}; removed={[r.name for r in removed]}",
        )
        await log(
            new.guild,
            f"🛡️ **Protected Rank Roles** — rolurile lui {new.mention} au fost restaurate.\n"
            f"Roluri adăugate neautorizat: {', '.join(r.name for r in added) or '—'}\n"
            f"Roluri scoase neautorizat: {', '.join(r.name for r in removed) or '—'}\n"
            f"Autor: {f'<@{actor}>' if actor else '`necunoscut`'}",
            "log_roles",
        )
        return True
    except Exception as e:
        await log(new.guild, f"⚠️ Nu am putut restaura rolurile lui {new.mention}: `{e}`", "log_roles")
        return False


async def on_member_update(old, new):
    nick_changed = old.nick != new.nick
    roles_changed = {r.id for r in old.roles} != {r.id for r in new.roles}
    if not nick_changed and not roles_changed:
        return

    entry = None
    if nick_changed:
        entry = await audit_entry(new.guild, discord.AuditLogAction.member_update, new.id)
    if roles_changed:
        role_entry = await audit_entry(new.guild, discord.AuditLogAction.member_role_update, new.id)
        if role_entry:
            entry = role_entry
    actor = entry.user.id if entry and entry.user else None

    # Botul tocmai și-a aplicat propriile modificări legitime: actualizăm snapshotul.
    if actor and new.guild.me and actor == new.guild.me.id:
        await capture_member_state(new)
        return

    if nick_changed and _bool("nickname_protection", "1"):
        await _restore_member_nickname(old, new, actor)

    if roles_changed and _bool("protected_rank_roles", "1"):
        restored = await _restore_protected_member_roles(old, new, actor)
        if restored:
            return

    # Snapshot restore pentru roluri adăugate/scoase manual membrilor C.C.O.
    if roles_changed and _bool("member_role_restore", "1"):
        cco_row = fetchone("SELECT 1 FROM members WHERE discord_id=? AND status IN ('active','inactive','suspended')", (str(new.id),))
        if cco_row and actor and not actor_exempt(new.guild, actor, "member_role_manual"):
            before_names = {r.id: r.name for r in old.roles}
            after_names = {r.id: r.name for r in new.roles}
            added_names = [after_names[rid] for rid in after_names.keys() - before_names.keys()]
            removed_names = [before_names[rid] for rid in before_names.keys() - after_names.keys()]
            if await restore_snapshot_roles(new):
                audit("security_member_roles_snapshot_restore", actor, new.id, f"added={added_names};removed={removed_names}")
                await log(
                    new.guild,
                    f"♻️ **Role Snapshot Restore** — rolurile lui {new.mention} au fost restaurate.\n"
                    f"Adăugate manual: {', '.join(added_names) or '—'}\n"
                    f"Scoase manual: {', '.join(removed_names) or '—'}\n"
                    f"Autor: <@{actor}>",
                    "log_roles",
                )
                return

    # Role Permission / Escalation Guard pentru roluri periculoase adăugate unui utilizator.
    if roles_changed and _bool("role_escalation_protection", "1"):
        added = [r for r in new.roles if r not in old.roles]
        dangerous = [
            r for r in added
            if r.permissions.administrator
            or r.permissions.manage_guild
            or r.permissions.manage_roles
            or r.permissions.ban_members
            or r.permissions.kick_members
        ]
        if dangerous and actor and not actor_exempt(new.guild, actor, "role_escalation"):
            try:
                await new.remove_roles(*dangerous, reason="C.C.O. Anti Role Escalation")
            except Exception:
                pass
            audit("security_role_escalation", actor, new.id, ",".join(r.name for r in dangerous))
            await log(
                new.guild,
                f"🚨 **Role Escalation Guard** — roluri periculoase eliminate de la {new.mention}: "
                f"{', '.join(r.name for r in dangerous)} | autor: <@{actor}>",
                "log_roles",
            )
            await punish(new.guild, actor, "Anti Role Escalation", "role_escalation")
            return

    await capture_member_state(new)


async def on_message(message):
    if not message.guild or message.author.bot:
        return

    if _bool("antispam_enabled", "1"):
        q = spam_events[message.author.id]
        now = time.time()
        window = int(get_security("antispam_window_sec", "5"))
        while q and now - q[0] > window:
            q.popleft()
        q.append(now)
        if len(q) > int(get_security("antispam_messages", "6")):
            try:
                await message.delete()
                until = discord.utils.utcnow() + timedelta(minutes=int(get_security("antispam_timeout_min", "5")))
                await message.author.timeout(until, reason="Anti-Spam")
            except Exception:
                pass
            return

    if _bool("antimention_enabled", "1"):
        total_mentions = len(message.mentions) + len(message.role_mentions)
        if total_mentions > int(get_security("antimention_max", "5")) or message.mention_everyone:
            try:
                await message.delete()
                until = discord.utils.utcnow() + timedelta(minutes=int(get_security("antimention_timeout_min", "10")))
                await message.author.timeout(until, reason="Mention Spam")
            except Exception:
                pass
            return

    if _bool("antilink_enabled", "0"):
        txt = message.content.lower()
        if "http://" in txt or "https://" in txt or "discord.gg/" in txt:
            if not is_whitelisted(message.author.id, "links"):
                allowed = {x for x in get_security("antilink_allowed_channels", "").split(",") if x}
                if str(message.channel.id) not in allowed:
                    try:
                        await message.delete()
                    except Exception:
                        pass
