import os
import asyncio
import threading
import shutil
from pathlib import Path
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

from .db import (
    init_db, execute, fetchone, fetchall, get_config, set_config,
    get_security, set_security, is_superuser, audit
)
from .config.ranks import RANKS, rank_index, LEADERSHIP_MIN_INDEX, SSI_INDEX, is_leadership_rank
from .utils import owner_id, has_leadership, is_privileged, get_member_rank, can_manage_target
from .services.member_service import (
    set_member_identity, promote_member, demote_member, remove_member, change_member_rank,
    suspend_member, reactivate_member, sync_rank_roles, sync_nickname,
    available_callsigns, assign_callsign_db, leadership_callsigns_available
)
from .services.timesheet_service import (
    clock_in, set_log_message, clock_out, cancel_timesheet,
    adjust_time, reset_time, total_minutes, top_timesheets
)
from .services.snapshot_service import make_snapshot
from .services.security_service import (
    on_channel_delete as security_channel_delete,
    on_role_delete as security_role_delete,
    on_role_update as security_role_update,
    on_member_join as security_member_join,
    on_member_remove as security_member_remove,
    on_ban_add as security_ban_add,
    on_webhooks_update as security_webhooks_update,
    on_member_update as security_member_update,
    on_message as security_message,
    capture_all_member_states,
)
from .services.operational_service import (
    parse_user_ids, parse_duration_days, create_operational_record, set_record_message,
    create_sanction, expire_state, approve_inactivity, reject_inactivity,
    accept_resignation, reject_resignation, create_action_request,
    approve_action_request, reject_action_request, approved_action_request,
    operational_counts
)
from dashboard.app import run_dashboard
from .panel_bridge import init_bridge

load_dotenv()
init_db()

TZ = ZoneInfo(os.getenv("TIMEZONE", "Europe/Bucharest"))
GUILD_ID = int(os.getenv("GUILD_ID", "0") or 0)

intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.moderation = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# V6 - Command Permission Guard + Cooldown
COMMAND_LEVELS = {
    # membru C.C.O.
    "pontaj": "member", "adauga-buletin": "member", "adauga-filaj": "member",
    "adauga-interogatoriu": "member", "demisie": "member", "cerere-inactivitate": "member",
    "cerere-actiune": "ssi", "membru-info": "member",
    # conducere
    "adauga-membru": "leadership", "promoveaza-membru": "leadership", "retrogradeaza-membru": "leadership",
    "elimina-membru": "leadership", "suspenda-membru": "leadership", "reactiveaza-membru": "leadership",
    "schimba-grad": "leadership", "modifica-membru": "leadership", "sanctiune": "leadership",
    "adauga-actiune": "leadership", "pontaj-top": "leadership", "pontaj-redu": "leadership",
    "pontaj-adauga": "leadership", "pontaj-reset": "leadership", "forteaza-clockout": "leadership",
    "warn": "leadership", "remove-warn": "leadership", "setup-ticket": "leadership",
    "adauga-tester": "leadership", "sterge-tester": "leadership", "adauga-sef-tester": "superuser",
    "sterge-sef-tester": "superuser", "ticket-add": "leadership", "ticket-remove": "leadership",
    "ticket-rename": "leadership", "ticket-transfer": "leadership", "ticket-transcript": "leadership",
    # superuser
    "security-config": "superuser", "server-save": "superuser", "server-backups": "superuser", "backup": "superuser",
    "config-rol": "superuser", "config-canal": "superuser", "config-categorie": "superuser", "sync-membri": "superuser",
    "set-callsign-range": "superuser", "schimba-callsign": "superuser", "elibereaza-callsign": "superuser",
    "whitelist-add": "superuser", "whitelist-remove": "superuser", "whitelist-list": "superuser", "set-status": "superuser",
    # owner
    "adauga-superuser": "owner", "sterge-superuser": "owner",
}
_COMMAND_LAST = {}

async def _global_command_guard(interaction: discord.Interaction):
    if not interaction.guild or not interaction.command:
        return True
    if get_security("command_permission_guard", "1") != "1":
        return True
    member = interaction.guild.get_member(interaction.user.id)
    if not member:
        return False
    name = interaction.command.name
    level = COMMAND_LEVELS.get(name, "member")
    allowed = False
    if level == "owner":
        allowed = member.id == owner_id()
    elif level == "superuser":
        allowed = member.id == owner_id() or is_superuser(member.id)
    elif level == "leadership":
        allowed = member.id == owner_id() or is_superuser(member.id) or has_leadership(member)
    elif level == "ssi":
        row = fetchone("SELECT rank,status FROM members WHERE discord_id=?", (str(member.id),))
        allowed = bool(row and row["status"] in ("active", "inactive", "suspended") and rank_index(row["rank"]) == SSI_INDEX)
    else:
        allowed = member.id == owner_id() or is_superuser(member.id) or fetchone(
            "SELECT 1 FROM members WHERE discord_id=? AND status IN ('active','inactive','suspended')",
            (str(member.id),),
        ) is not None
    if not allowed:
        audit("command_denied", member.id, None, f"/{name} requires {level}")
        await safe_ephemeral(interaction, f"⛔ Nu ai nivelul necesar pentru `/{name}`.")
        return False

    if get_security("command_cooldown_enabled", "1") == "1":
        now = asyncio.get_running_loop().time()
        critical = {"elimina-membru", "schimba-grad", "sanctiune", "security-config", "server-save", "backup"}
        cooldown = 5.0 if name in critical else 2.0
        if member.id == owner_id() or is_superuser(member.id):
            cooldown = min(cooldown, 1.0)
        key = (member.id, name)
        last = _COMMAND_LAST.get(key, 0.0)
        if now - last < cooldown:
            await safe_ephemeral(interaction, f"⏳ Așteaptă {cooldown - (now-last):.1f}s înainte să refolosești comanda.")
            return False
        _COMMAND_LAST[key] = now
    audit("command_use", member.id, None, f"/{name}")
    return True

tree.interaction_check = _global_command_guard

def local_time(iso_string):
    dt = datetime.fromisoformat(iso_string)
    return dt.astimezone(TZ).strftime("%H:%M")

def fmt_min(m):
    return f"{m//60}h {m%60}m"

def rank_choices():
    return [app_commands.Choice(name=r["name"], value=r["name"]) for r in RANKS]

async def safe_ephemeral(interaction, text):
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True)
    else:
        await interaction.response.send_message(text, ephemeral=True)

async def require_priv(interaction):
    member = interaction.guild.get_member(interaction.user.id)
    if not member or not is_privileged(member):
        await safe_ephemeral(interaction, "Nu ai permisiunea necesară.")
        return None
    return member

async def require_leadership(interaction):
    member = interaction.guild.get_member(interaction.user.id)
    if not member or not (has_leadership(member) or is_superuser(member.id) or member.id == owner_id()):
        await safe_ephemeral(interaction, "Doar Conducerea C.C.O. poate folosi această comandă.")
        return None
    return member

async def send_log(guild, key, text, file=None):
    cid = get_config(key)
    if not cid:
        return
    ch = guild.get_channel(int(cid))
    if ch and ch.is_text_based():
        kwargs = {"content": text}
        if file:
            kwargs["file"] = discord.File(file)
        try:
            await ch.send(**kwargs)
        except Exception:
            pass

async def make_transcript(channel):
    lines = []
    async for m in channel.history(limit=None, oldest_first=True):
        content = m.clean_content
        attachments = " ".join(a.url for a in m.attachments)
        lines.append(f"[{m.created_at.isoformat()}] {m.author}: {content} {attachments}".rstrip())
    p = Path("backups/tickets") / f"{channel.id}.txt"
    p.write_text("\n".join(lines), encoding="utf-8")
    return p

class TicketCreateView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Ajutor / Întrebare", style=discord.ButtonStyle.primary, custom_id="cco_ticket_help")
    async def help(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await create_ticket(interaction, "Ajutor / Întrebare")
        await interaction.followup.send("Ticket creat.", ephemeral=True)

    @discord.ui.button(label="Reclamație", style=discord.ButtonStyle.danger, custom_id="cco_ticket_report")
    async def report(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await create_ticket(interaction, "Reclamație")
        await interaction.followup.send("Ticket creat.", ephemeral=True)

class TicketControlView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Preia Ticket", style=discord.ButtonStyle.success, custom_id="cco_ticket_claim")
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        ticket = fetchone("SELECT * FROM tickets WHERE channel_id=?", (str(interaction.channel.id),))
        if not ticket or ticket["status"] != "open":
            return await interaction.followup.send("Ticket invalid sau închis.", ephemeral=True)
        member = interaction.guild.get_member(interaction.user.id)
        if not member or not (has_leadership(member) or is_superuser(member.id) or member.id == owner_id()):
            return await interaction.followup.send("Doar Conducerea C.C.O. poate prelua ticketul.", ephemeral=True)
        if ticket["claimed_by"]:
            return await interaction.followup.send(f"Deja preluat de <@{ticket['claimed_by']}>.", ephemeral=True)

        lead_id = get_config("role_leadership")
        if lead_id and (lead_role := interaction.guild.get_role(int(lead_id))):
            await interaction.channel.set_permissions(
                lead_role, view_channel=True, read_message_history=True, send_messages=False
            )
        await interaction.channel.set_permissions(
            member, view_channel=True, read_message_history=True, send_messages=True
        )
        execute("UPDATE tickets SET claimed_by=? WHERE channel_id=?", (str(member.id), str(interaction.channel.id)))
        audit("ticket_claim", member.id, interaction.channel.id, "")
        await interaction.followup.send("Ai preluat ticketul.", ephemeral=True)

        # edităm mesajul principal, nu mai trimitem un al doilea mesaj în canal
        if ticket["panel_message_id"]:
            try:
                msg = await interaction.channel.fetch_message(int(ticket["panel_message_id"]))
                await msg.edit(
                    content=f"<@{ticket['creator_id']}>\n✅ Ticket preluat de {member.mention}.",
                    view=self
                )
            except Exception:
                pass

    @discord.ui.button(label="Renunță la Ticket", style=discord.ButtonStyle.secondary, custom_id="cco_ticket_release")
    async def release(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        ticket = fetchone("SELECT * FROM tickets WHERE channel_id=?", (str(interaction.channel.id),))
        if not ticket or not ticket["claimed_by"]:
            return await interaction.followup.send("Ticketul nu este preluat.", ephemeral=True)
        if str(interaction.user.id) != ticket["claimed_by"] and interaction.user.id != owner_id() and not is_superuser(interaction.user.id):
            return await interaction.followup.send("Doar responsabilul curent poate renunța.", ephemeral=True)

        old_member = interaction.guild.get_member(int(ticket["claimed_by"]))
        if old_member:
            await interaction.channel.set_permissions(old_member, overwrite=None)

        execute("UPDATE tickets SET claimed_by=NULL WHERE channel_id=?", (str(interaction.channel.id),))
        audit("ticket_release", interaction.user.id, interaction.channel.id, "")
        await interaction.followup.send("Ai renunțat la ticket.", ephemeral=True)

        if ticket["panel_message_id"]:
            try:
                msg = await interaction.channel.fetch_message(int(ticket["panel_message_id"]))
                await msg.edit(
                    content=f"<@{ticket['creator_id']}>\nUn membru al conducerii trebuie să apese **Preia Ticket**.",
                    view=self
                )
            except Exception:
                pass

    @discord.ui.button(label="Close Ticket", style=discord.ButtonStyle.danger, custom_id="cco_ticket_close")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        # DEFER imediat ca să nu apară "botul nu a răspuns la timp"
        await interaction.response.defer(ephemeral=True)
        ticket = fetchone("SELECT * FROM tickets WHERE channel_id=?", (str(interaction.channel.id),))
        if not ticket:
            return await interaction.followup.send("Acest canal nu este un ticket.", ephemeral=True)
        if ticket["status"] == "closed":
            return await interaction.followup.send("Ticketul este deja închis.", ephemeral=True)
        if str(interaction.user.id) not in {ticket["creator_id"], ticket["claimed_by"]} and interaction.user.id != owner_id() and not is_superuser(interaction.user.id):
            return await interaction.followup.send("Doar creatorul sau responsabilul poate închide ticketul.", ephemeral=True)

        transcript = await make_transcript(interaction.channel)
        now = datetime.now(TZ).strftime("%d.%m.%Y %H:%M")

        execute("""
            UPDATE tickets SET status='closed',closed_at=CURRENT_TIMESTAMP,closed_by=?
            WHERE channel_id=?
        """, (str(interaction.user.id), str(interaction.channel.id)))
        audit("ticket_close", interaction.user.id, interaction.channel.id, "")

        # Edităm MESAJUL PRINCIPAL, nu trimitem două mesaje.
        if ticket["panel_message_id"]:
            try:
                msg = await interaction.channel.fetch_message(int(ticket["panel_message_id"]))
                claimed = f"<@{ticket['claimed_by']}>" if ticket["claimed_by"] else "Nimeni"
                await msg.edit(
                    content=(
                        "🔒 **TICKET ÎNCHIS**\n"
                        f"Creator: <@{ticket['creator_id']}>\n"
                        f"Preluat de: {claimed}\n"
                        f"Închis de: {interaction.user.mention}\n"
                        f"Data/Ora: **{now}**"
                    ),
                    view=None
                )
            except Exception:
                pass

        # blocăm ticketul, îl păstrăm pentru vizualizare
        creator = interaction.guild.get_member(int(ticket["creator_id"]))
        if creator:
            await interaction.channel.set_permissions(
                creator, view_channel=True, read_message_history=True, send_messages=False
            )
        if ticket["claimed_by"]:
            handler = interaction.guild.get_member(int(ticket["claimed_by"]))
            if handler:
                await interaction.channel.set_permissions(
                    handler, view_channel=True, read_message_history=True, send_messages=False
                )
        try:
            await interaction.channel.edit(name=f"inchis-{interaction.channel.name}"[:100])
        except Exception:
            pass

        await send_log(
            interaction.guild,
            "log_tickets",
            f"🔒 Ticket `{interaction.channel.name}` închis de {interaction.user.mention}.",
            transcript
        )
        await interaction.followup.send("Ticket închis.", ephemeral=True)

async def create_ticket(interaction, ticket_type):
    guild = interaction.guild
    category = None
    cat_id = get_config("ticket_category")
    if cat_id:
        category = guild.get_channel(int(cat_id))

    lead_id = get_config("role_leadership")
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True, manage_messages=True),
    }
    if lead_id and (lead_role := guild.get_role(int(lead_id))):
        overwrites[lead_role] = discord.PermissionOverwrite(
            view_channel=True, read_message_history=True, send_messages=False
        )

    base = f"ticket-{interaction.user.name}".lower().replace("_","-")
    ch = await guild.create_text_channel(
        base[:90], category=category, overwrites=overwrites, reason=f"Ticket {ticket_type}"
    )
    tid = execute(
        "INSERT INTO tickets(channel_id,creator_id,type,status) VALUES(?,?,?,'open')",
        (str(ch.id), str(interaction.user.id), ticket_type)
    )
    msg = await ch.send(
        f"{interaction.user.mention}\nUn membru al conducerii trebuie să apese **Preia Ticket**.",
        view=TicketControlView()
    )
    execute("UPDATE tickets SET panel_message_id=? WHERE id=?", (str(msg.id), tid))
    audit("ticket_create", interaction.user.id, ch.id, ticket_type)

class TimesheetCancelView(discord.ui.View):
    def __init__(self, ts_id):
        super().__init__(timeout=None)
        self.ts_id = int(ts_id)
        btn = discord.ui.Button(
            label="❌ Anulează",
            style=discord.ButtonStyle.danger,
            custom_id=f"cco_ts_cancel:{self.ts_id}"
        )
        btn.callback = self.cancel
        self.add_item(btn)

    async def cancel(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        member = interaction.guild.get_member(interaction.user.id)
        if not member or not is_privileged(member):
            return await interaction.followup.send("Doar conducerea poate anula pontaje.", ephemeral=True)
        ts = cancel_timesheet(self.ts_id, interaction.user.id)
        if not ts:
            return await interaction.followup.send("Pontaj inexistent.", ephemeral=True)
        try:
            msg = await interaction.channel.fetch_message(int(ts["log_message_id"]))
            row = fetchone("SELECT * FROM members WHERE discord_id=?", (str(ts["user_id"]),))
            display = (
                f"[{row['callsign']}] {row['first_name']} {row['last_name']} | {row['internal_id']}"
                if row and row["callsign"] else f"<@{ts['user_id']}>"
            )
            out_text = local_time(ts["clock_out"]) if ts["clock_out"] else "—"
            duration = f" ({ts['duration_minutes']} min)" if ts["clock_out"] else ""
            content = (
                f"**{display}**\n"
                f"Clock in: **{local_time(ts['clock_in'])}** / Clock out: **{out_text}**{duration}\n\n"
                f"❌ **Pontaj anulat de {interaction.user.mention}**"
            )
            await msg.edit(content=content, view=None)
        except Exception:
            pass
        audit("timesheet_cancel", interaction.user.id, ts["user_id"], str(self.ts_id))
        await interaction.followup.send("Pontaj anulat.", ephemeral=True)

class PontajView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)

    @discord.ui.button(label="🟢 Clock In", style=discord.ButtonStyle.success)
    async def cin(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        try:
            ts = clock_in(interaction.user.id)
        except Exception as e:
            return await interaction.followup.send(str(e), ephemeral=True)

        row = fetchone("SELECT * FROM members WHERE discord_id=?", (str(interaction.user.id),))
        display = (
            f"[{row['callsign']}] {row['first_name']} {row['last_name']} | {row['internal_id']}"
            if row and row["callsign"] else interaction.user.display_name
        )
        cid = get_config("log_timesheets")
        if not cid:
            return await interaction.followup.send("Canalul de pontaje nu este configurat.", ephemeral=True)
        ch = interaction.guild.get_channel(int(cid))
        if not ch:
            return await interaction.followup.send("Canalul de pontaje nu mai există.", ephemeral=True)

        text = f"**{display}**\nClock in: **{local_time(ts['clock_in'])}** / Clock out: **—**"
        msg = await ch.send(text, view=TimesheetCancelView(ts["id"]))
        set_log_message(ts["id"], ch.id, msg.id)
        audit("clock_in", interaction.user.id, interaction.user.id, str(ts["id"]))
        await interaction.followup.send("🟢 Pontaj început.", ephemeral=True)

    @discord.ui.button(label="🔴 Clock Out", style=discord.ButtonStyle.danger)
    async def cout(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        try:
            ts = clock_out(interaction.user.id)
        except Exception as e:
            return await interaction.followup.send(str(e), ephemeral=True)

        row = fetchone("SELECT * FROM members WHERE discord_id=?", (str(interaction.user.id),))
        display = (
            f"[{row['callsign']}] {row['first_name']} {row['last_name']} | {row['internal_id']}"
            if row and row["callsign"] else interaction.user.display_name
        )
        content = (
            f"**{display}**\n"
            f"Clock in: **{local_time(ts['clock_in'])}** / "
            f"Clock out: **{local_time(ts['clock_out'])}** "
            f"(**{ts['duration_minutes']} min**)"
        )

        # EDITĂM mesajul Clock In existent
        if ts["log_channel_id"] and ts["log_message_id"]:
            ch = interaction.guild.get_channel(int(ts["log_channel_id"]))
            if ch:
                try:
                    msg = await ch.fetch_message(int(ts["log_message_id"]))
                    await msg.edit(content=content, view=TimesheetCancelView(ts["id"]))
                except Exception:
                    pass

        audit("clock_out", interaction.user.id, interaction.user.id, str(ts["id"]))
        await interaction.followup.send(
            f"🔴 Pontaj închis: **{ts['duration_minutes']} minute**.",
            ephemeral=True
        )


class DashboardButtonView(discord.ui.View):
    """Buton-link către dashboard-ul C.C.O."""
    def __init__(self):
        super().__init__(timeout=None)
        dashboard_url = os.getenv("DASHBOARD_BASE_URL", "http://localhost:3000").strip()
        self.add_item(
            discord.ui.Button(
                label="Deschide Panel C.C.O.",
                style=discord.ButtonStyle.link,
                url=dashboard_url,
                emoji="🖥️"
            )
        )

# ---------- V4: ACTIVITATE OPERAȚIONALĂ / APROBĂRI ----------

OP_CHANNELS = {
    "buletin": "log_buletine",
    "filaj": "log_filaje",
    "interogatoriu": "log_interogatorii",
    "actiune": "log_actiuni",
}


def is_cco_db_member(user_id):
    row = fetchone("SELECT status FROM members WHERE discord_id=?", (str(user_id),))
    return bool(row and row["status"] in {"active", "inactive"})


async def require_cco(interaction):
    if not is_cco_db_member(interaction.user.id):
        await safe_ephemeral(interaction, "Trebuie să fii membru C.C.O. pentru această comandă.")
        return None
    return interaction.guild.get_member(interaction.user.id)


async def configured_text_channel(guild, key):
    cid = get_config(key)
    if not cid:
        raise RuntimeError(f"Canalul `{key}` nu este configurat. Folosește /config-canal.")
    ch = guild.get_channel(int(cid))
    if not ch or not ch.is_text_based():
        raise RuntimeError(f"Canalul configurat pentru `{key}` nu mai există.")
    return ch


def _status_label(status):
    return {"pending": "🟡 ÎN AȘTEPTARE", "accepted": "🟢 ACCEPTAT", "rejected": "🔴 RESPINS"}.get(status, status.upper())


async def edit_approval_message(interaction, status, reviewer, reason=""):
    try:
        msg = interaction.message
        if not msg:
            return
        embeds = list(msg.embeds)
        if embeds:
            e = embeds[0].copy()
            e.colour = discord.Colour.green() if status == "accepted" else discord.Colour.red()
            e.add_field(name="Status", value=_status_label(status), inline=True)
            e.add_field(name="Verificat de", value=reviewer.mention, inline=True)
            if reason:
                e.add_field(name="Motiv", value=reason[:1024], inline=False)
            await msg.edit(embed=e, view=None)
        else:
            await msg.edit(content=f"{msg.content}\n\n{_status_label(status)} de {reviewer.mention}", view=None)
    except Exception:
        pass


class ApprovalView(discord.ui.View):
    def __init__(self, kind, record_id):
        super().__init__(timeout=None)
        self.kind = kind
        self.record_id = int(record_id)
        ok = discord.ui.Button(label="Acceptă", style=discord.ButtonStyle.success,
                               custom_id=f"cco_v4_accept:{kind}:{self.record_id}")
        no = discord.ui.Button(label="Respinge", style=discord.ButtonStyle.danger,
                               custom_id=f"cco_v4_reject:{kind}:{self.record_id}")
        ok.callback = self.accept
        no.callback = self.reject
        self.add_item(ok); self.add_item(no)

    async def _leadership(self, interaction):
        m = interaction.guild.get_member(interaction.user.id)
        if not m or not (has_leadership(m) or is_superuser(m.id) or m.id == owner_id()):
            await interaction.followup.send("Doar Conducerea C.C.O. poate verifica această cerere.", ephemeral=True)
            return None
        return m

    async def accept(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        reviewer = await self._leadership(interaction)
        if not reviewer: return
        try:
            if self.kind == "operational":
                row = fetchone("SELECT * FROM operational_records WHERE id=?", (self.record_id,))
                if not row or row["status"] != "pending": raise RuntimeError("Înregistrarea nu mai este în așteptare.")
                execute("UPDATE operational_records SET status='accepted',reviewer_id=?,reviewed_at=CURRENT_TIMESTAMP WHERE id=?",
                        (str(reviewer.id), self.record_id))
                audit("operational_accept", reviewer.id, row["author_id"], f"{row['kind']}#{self.record_id}")
            elif self.kind == "action_request":
                approve_action_request(self.record_id, reviewer.id)
            elif self.kind == "inactivity":
                await approve_inactivity(interaction.guild, self.record_id, reviewer.id)
            elif self.kind == "resignation":
                await accept_resignation(
                    interaction.guild, self.record_id, reviewer,
                    owner=reviewer.id == owner_id(), superuser=is_superuser(reviewer.id)
                )
            else:
                raise RuntimeError("Tip de aprobare necunoscut.")
            await edit_approval_message(interaction, "accepted", reviewer)
            await interaction.followup.send("✅ Acceptat.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"⛔ {e}", ephemeral=True)

    async def reject(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        reviewer = await self._leadership(interaction)
        if not reviewer: return
        reason = "Respins de Conducere"
        try:
            if self.kind == "operational":
                row = fetchone("SELECT * FROM operational_records WHERE id=?", (self.record_id,))
                if not row or row["status"] != "pending": raise RuntimeError("Înregistrarea nu mai este în așteptare.")
                execute("UPDATE operational_records SET status='rejected',reviewer_id=?,review_reason=?,reviewed_at=CURRENT_TIMESTAMP WHERE id=?",
                        (str(reviewer.id), reason, self.record_id))
                audit("operational_reject", reviewer.id, row["author_id"], f"{row['kind']}#{self.record_id}")
            elif self.kind == "action_request":
                reject_action_request(self.record_id, reviewer.id, reason)
            elif self.kind == "inactivity":
                await reject_inactivity(self.record_id, reviewer.id, reason)
            elif self.kind == "resignation":
                await reject_resignation(self.record_id, reviewer.id, reason)
            else:
                raise RuntimeError("Tip de aprobare necunoscut.")
            await edit_approval_message(interaction, "rejected", reviewer, reason)
            await interaction.followup.send("❌ Respins.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"⛔ {e}", ephemeral=True)


async def post_operational(guild, record_id):
    import json
    row = fetchone("SELECT * FROM operational_records WHERE id=?", (record_id,))
    if not row:
        raise RuntimeError("Înregistrare inexistentă.")
    ch = await configured_text_channel(guild, OP_CHANNELS[row["kind"]])
    data = json.loads(row["data_json"] or "{}")
    participants = json.loads(row["participants_json"] or "[]")
    titles = {"buletin": "📄 Buletin", "filaj": "👁️ Filaj", "interogatoriu": "🗣️ Interogatoriu", "actiune": "🚨 Acțiune"}
    e = discord.Embed(title=f"{titles.get(row['kind'], row['kind'])} #{row['id']}", colour=0xE5A50A)
    e.add_field(name="Postat de", value=f"<@{row['author_id']}>", inline=True)
    e.add_field(name="Status", value="🟡 ÎN AȘTEPTARE", inline=True)
    if row["subject_name"]: e.add_field(name="Persoană / Suspect", value=row["subject_name"][:1024], inline=False)
    if row["subject_cnp"]: e.add_field(name="CNP", value=row["subject_cnp"][:1024], inline=True)
    if participants: e.add_field(name="Participanți", value=" ".join(f"<@{x}>" for x in participants)[:1024], inline=False)
    for k, v in data.items():
        if v is None or v == "": continue
        e.add_field(name=str(k).replace("_", " ").title()[:256], value=str(v)[:1024], inline=False)
    if row["evidence_url"]: e.add_field(name="Dovadă", value=row["evidence_url"][:1024], inline=False)
    msg = await ch.send(embed=e, view=ApprovalView("operational", record_id))
    set_record_message(record_id, ch.id, msg.id)
    return msg


class CallsignSelectView(discord.ui.View):
    def __init__(self, options, callback_coro, requester_id):
        super().__init__(timeout=120)
        self.callback_coro = callback_coro
        self.requester_id = int(requester_id)
        select = discord.ui.Select(
            placeholder="Alege callsign-ul de Conducere",
            min_values=1, max_values=1,
            options=[discord.SelectOption(label=x, value=x) for x in options[:25]],
        )
        select.callback = self.chosen
        self.select = select
        self.add_item(select)

    async def chosen(self, interaction: discord.Interaction):
        if interaction.user.id != self.requester_id:
            return await interaction.response.send_message("Doar persoana care a pornit operația poate alege.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        try:
            text = await self.callback_coro(self.select.values[0])
            for child in self.children: child.disabled = True
            try: await interaction.message.edit(view=self)
            except Exception: pass
            await interaction.followup.send(text, ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"⛔ {e}", ephemeral=True)


async def _rank_operation(interaction, target, new_rank, action_label, first_name=None, last_name=None, internal_id=None):
    actor = interaction.guild.get_member(interaction.user.id)
    ok, reason = can_manage_target(actor, target, new_rank)
    if not ok:
        return await safe_ephemeral(interaction, f"⛔ {reason}")
    options = available_callsigns(new_rank, target.id)
    current = fetchone("SELECT rank,callsign FROM members WHERE discord_id=?", (str(target.id),))
    # V5: fiecare grad de Conducere are propriul pool de callsign-uri.
    # Păstrăm callsign-ul doar dacă gradul rămâne exact același.
    if current and current["rank"] == new_rank and current["callsign"]:
        options = [current["callsign"]]
    if not options:
        return await safe_ephemeral(interaction, f"⛔ Nu există callsign-uri libere pentru **{new_rank}**.")

    async def perform(chosen=None):
        if first_name is not None:
            row = await set_member_identity(target, first_name, last_name, internal_id, new_rank, callsign=chosen, actor_id=actor.id)
        else:
            row = await change_member_rank(target, new_rank, actor_id=actor.id, callsign=chosen)
        if actor.id == target.id and (actor.id == owner_id() or is_superuser(actor.id)):
            audit("self_elevation", actor.id, target.id, f"{current['rank'] if current else None} -> {new_rank} | {row['callsign']}")
            await send_log(interaction.guild, "log_roles", f"👑 **SELF-ELEVATION** — {actor.mention} și-a schimbat gradul în **{new_rank}** / **{row['callsign']}**.")
        else:
            audit("rank_change", actor.id, target.id, f"{current['rank'] if current else None} -> {new_rank} | {row['callsign']}")
        await send_log(interaction.guild, "log_roles", f"🔁 {target.mention} → **{new_rank}** / **{row['callsign']}** de {actor.mention}")
        return f"✅ {action_label}: {target.mention} → **{new_rank}** / **{row['callsign']}**."

    if is_leadership_rank(new_rank) and len(options) > 1 and not (current and current["rank"] == new_rank and current["callsign"] in options):
        view = CallsignSelectView(options, perform, interaction.user.id)
        if interaction.response.is_done():
            await interaction.followup.send("Sunt mai multe callsign-uri de Conducere libere. Alege unul:", view=view, ephemeral=True)
        else:
            await interaction.response.send_message("Sunt mai multe callsign-uri de Conducere libere. Alege unul:", view=view, ephemeral=True)
        return
    text = await perform(options[0])
    await safe_ephemeral(interaction, text)

# ---------- EVENTS ----------

@bot.event
async def on_ready():
    print(f"Conectat ca {bot.user}")
    init_bridge(bot, asyncio.get_running_loop())

    if GUILD_ID and (guild := bot.get_guild(GUILD_ID)):
        try:
            await capture_all_member_states(guild)
        except Exception as e:
            print("Protection snapshot warning:", e)

    for sid in [x.strip() for x in os.getenv("SUPERUSER_IDS","").split(",") if x.strip()]:
        execute("INSERT OR IGNORE INTO superusers(user_id,added_by) VALUES(?,?)", (sid, "env"))

    bot.add_view(TicketCreateView())
    bot.add_view(TicketControlView())

    # reînregistrăm butoanele Anulează după restart
    for ts in fetchall("""
        SELECT id,log_message_id FROM timesheets
        WHERE status IN ('active','closed') AND log_message_id IS NOT NULL
        ORDER BY id DESC LIMIT 500
    """):
        bot.add_view(TimesheetCancelView(ts["id"]), message_id=int(ts["log_message_id"]))

    # Reînregistrăm aprobările V4 după restart.
    for r in fetchall("SELECT id,message_id FROM operational_records WHERE status='pending' AND message_id IS NOT NULL"):
        bot.add_view(ApprovalView("operational", r["id"]), message_id=int(r["message_id"]))
    for r in fetchall("SELECT id,message_id FROM action_requests WHERE status='pending' AND message_id IS NOT NULL"):
        bot.add_view(ApprovalView("action_request", r["id"]), message_id=int(r["message_id"]))
    for r in fetchall("SELECT id,message_id FROM inactivity_requests WHERE status='pending' AND message_id IS NOT NULL"):
        bot.add_view(ApprovalView("inactivity", r["id"]), message_id=int(r["message_id"]))
    for r in fetchall("SELECT id,message_id FROM resignations WHERE status='pending' AND message_id IS NOT NULL"):
        bot.add_view(ApprovalView("resignation", r["id"]), message_id=int(r["message_id"]))

    if GUILD_ID:
        guild_obj = discord.Object(id=GUILD_ID)
        tree.copy_global_to(guild=guild_obj)
        synced = await tree.sync(guild=guild_obj)
        print(f"Comenzi sincronizate: {len(synced)}")
        print(", ".join(c.name for c in synced))
    else:
        synced = await tree.sync()
        print(f"Comenzi globale sincronizate: {len(synced)}")

    guild = bot.get_guild(GUILD_ID) if GUILD_ID else None
    if guild:
        make_snapshot(guild)

    if not security_snapshot_loop.is_running():
        security_snapshot_loop.start()
    if not cco_state_loop.is_running():
        cco_state_loop.start()

@tasks.loop(minutes=5)
async def cco_state_loop():
    guild = bot.get_guild(GUILD_ID) if GUILD_ID else None
    if guild:
        await expire_state(guild)

@tasks.loop(minutes=1)
async def security_snapshot_loop():
    guild = bot.get_guild(GUILD_ID) if GUILD_ID else None
    if not guild:
        return
    interval = max(1, int(get_security("snapshot_interval_min", "10")))
    last = fetchone(
        "SELECT created_at FROM snapshots WHERE guild_id=? AND kind='full' ORDER BY id DESC LIMIT 1",
        (str(guild.id),)
    )
    if not last:
        make_snapshot(guild)
        return
    # SQLite timestamp este UTC aproximativ; folosim epoch prin julianday
    age = fetchone("""
        SELECT (julianday('now') - julianday(created_at))*1440 AS minutes
        FROM snapshots WHERE guild_id=? AND kind='full'
        ORDER BY id DESC LIMIT 1
    """, (str(guild.id),))
    if age and float(age["minutes"] or 0) >= interval:
        make_snapshot(guild)

@bot.event
async def on_member_join(member):
    cid = get_config("welcome_channel")
    if cid and not member.bot:
        ch = member.guild.get_channel(int(cid))
        if ch and ch.is_text_based():
            await ch.send(f"👋 Bine ai venit {member.mention} în **Comandamentul Comun de Operațiuni (C.C.O.)**!")
    await security_member_join(member)

@bot.event
async def on_member_remove(member):
    # dacă era responsabil pe ticket, eliberăm ticketul
    tickets = fetchall(
        "SELECT * FROM tickets WHERE status='open' AND claimed_by=?",
        (str(member.id),)
    )
    for t in tickets:
        execute("UPDATE tickets SET claimed_by=NULL WHERE id=?", (t["id"],))
        ch = member.guild.get_channel(int(t["channel_id"]))
        if ch and t["panel_message_id"]:
            try:
                msg = await ch.fetch_message(int(t["panel_message_id"]))
                await msg.edit(
                    content=f"<@{t['creator_id']}>\nResponsabilul a părăsit serverul. Ticketul poate fi preluat din nou.",
                    view=TicketControlView()
                )
            except Exception:
                pass

    cid = get_config("leave_channel")
    if cid and not member.bot:
        ch = member.guild.get_channel(int(cid))
        if ch and ch.is_text_based():
            await ch.send(f"👋 **{member}** a ieșit din **Comandamentul Comun de Operațiuni (C.C.O.)**.")
    await security_member_remove(member)

@bot.event
async def on_member_update(before, after):
    await security_member_update(before, after)

@bot.event
async def on_guild_channel_delete(channel):
    await security_channel_delete(channel)

@bot.event
async def on_guild_role_delete(role):
    await security_role_delete(role)

@bot.event
async def on_guild_role_update(before, after):
    await security_role_update(before, after)

@bot.event
async def on_member_ban(guild, user):
    class BanObj:
        pass
    obj = BanObj()
    obj.guild = guild
    obj.user = user
    await security_ban_add(obj)

@bot.event
async def on_webhooks_update(channel):
    await security_webhooks_update(channel)

@bot.event
async def on_message(message):
    await security_message(message)
    await bot.process_commands(message)

@tree.error
async def tree_error(interaction, error):
    if isinstance(error, app_commands.CommandNotFound):
        return
    print("AppCommand error:", repr(error))
    try:
        await safe_ephemeral(interaction, f"❌ {error}")
    except Exception:
        pass

# ---------- COMMANDS ----------


@tree.command(name="panel", description="Trimite mesajul de acces către Panelul Administrativ C.C.O.")
@app_commands.guild_only()
async def panel(interaction: discord.Interaction):
    if not await require_priv(interaction):
        return

    dashboard_url = os.getenv("DASHBOARD_BASE_URL", "http://localhost:3000").strip()

    embed = discord.Embed(
        title="🖥️ Panel Administrativ C.C.O.",
        description=(
            "**Comandamentul Comun de Operațiuni (C.C.O.)**\n\n"
            "Accesează dashboard-ul administrativ pentru:\n"
            "• gestionarea membrilor și gradelor\n"
            "• pontaje și activitate\n"
            "• ticket-uri\n"
            "• callsign-uri\n"
            "• Security / Anti-Raid / Anti-Nuke\n"
            "• backup-uri și configurări\n\n"
            "Apasă butonul de mai jos pentru a deschide panelul."
        ),
        colour=0x2F81F7
    )
    embed.set_footer(text="C.C.O. • Panel Administrativ")

    await interaction.channel.send(embed=embed, view=DashboardButtonView())

    local_warning = ""
    if "localhost" in dashboard_url or "127.0.0.1" in dashboard_url:
        local_warning = (
            "\n⚠️ URL-ul panelului este local. Va funcționa doar pe calculatorul "
            "pe care rulează dashboard-ul."
        )

    await interaction.response.send_message(
        f"✅ Mesajul de acces la panel a fost trimis.{local_warning}",
        ephemeral=True
    )

@tree.command(name="setup-ticket", description="Postează panelul pentru ticket.")
@app_commands.guild_only()
async def setup_ticket(interaction: discord.Interaction):
    if not await require_priv(interaction): return
    emb = discord.Embed(
        title="🎫 Suport C.C.O.",
        description="Selectează tipul ticketului:",
        colour=0x2F81F7
    )
    await interaction.channel.send(embed=emb, view=TicketCreateView())
    await interaction.response.send_message("Panel ticket postat.", ephemeral=True)

@tree.command(name="ticket-add", description="Adaugă o persoană în ticket.")
@app_commands.guild_only()
async def ticket_add(interaction: discord.Interaction, username: discord.Member):
    if not await require_leadership(interaction): return
    ticket = fetchone("SELECT * FROM tickets WHERE channel_id=?", (str(interaction.channel.id),))
    if not ticket: return await safe_ephemeral(interaction, "Nu ești într-un ticket.")
    await interaction.channel.set_permissions(username, view_channel=True, send_messages=True, read_message_history=True)
    await safe_ephemeral(interaction, f"{username.mention} adăugat.")

@tree.command(name="ticket-remove", description="Scoate o persoană din ticket.")
@app_commands.guild_only()
async def ticket_remove(interaction: discord.Interaction, username: discord.Member):
    if not await require_leadership(interaction): return
    ticket = fetchone("SELECT * FROM tickets WHERE channel_id=?", (str(interaction.channel.id),))
    if not ticket: return await safe_ephemeral(interaction, "Nu ești într-un ticket.")
    if str(username.id) in {ticket["creator_id"], ticket["claimed_by"]}:
        return await safe_ephemeral(interaction, "Nu poți scoate creatorul sau responsabilul.")
    await interaction.channel.set_permissions(username, overwrite=None)
    await safe_ephemeral(interaction, f"{username.mention} scos.")

@tree.command(name="ticket-rename", description="Redenumește ticketul.")
@app_commands.guild_only()
async def ticket_rename(interaction: discord.Interaction, nume: str):
    if not await require_leadership(interaction): return
    await interaction.channel.edit(name=nume.lower().replace(" ","-")[:100])
    await safe_ephemeral(interaction, "Ticket redenumit.")

@tree.command(name="ticket-transfer", description="Transferă ticketul altui membru din conducere.")
@app_commands.guild_only()
async def ticket_transfer(interaction: discord.Interaction, username: discord.Member):
    ticket = fetchone("SELECT * FROM tickets WHERE channel_id=?", (str(interaction.channel.id),))
    if not ticket: return await safe_ephemeral(interaction, "Nu ești într-un ticket.")
    if str(interaction.user.id) != ticket["claimed_by"] and interaction.user.id != owner_id() and not is_superuser(interaction.user.id):
        return await safe_ephemeral(interaction, "Doar responsabilul curent poate transfera ticketul.")
    if not (has_leadership(username) or is_superuser(username.id)):
        return await safe_ephemeral(interaction, "Destinatarul trebuie să fie din conducere.")
    old = interaction.guild.get_member(int(ticket["claimed_by"])) if ticket["claimed_by"] else None
    if old: await interaction.channel.set_permissions(old, overwrite=None)
    await interaction.channel.set_permissions(username, view_channel=True, send_messages=True, read_message_history=True)
    execute("UPDATE tickets SET claimed_by=? WHERE id=?", (str(username.id), ticket["id"]))
    if ticket["panel_message_id"]:
        try:
            msg = await interaction.channel.fetch_message(int(ticket["panel_message_id"]))
            await msg.edit(
                content=f"<@{ticket['creator_id']}>\n✅ Ticket preluat de {username.mention}.",
                view=TicketControlView()
            )
        except Exception: pass
    await safe_ephemeral(interaction, "Ticket transferat.")

@tree.command(name="ticket-transcript", description="Generează transcriptul ticketului.")
@app_commands.guild_only()
async def ticket_transcript(interaction: discord.Interaction):
    if not await require_leadership(interaction): return
    ticket = fetchone("SELECT * FROM tickets WHERE channel_id=?", (str(interaction.channel.id),))
    if not ticket: return await safe_ephemeral(interaction, "Nu ești într-un ticket.")
    await interaction.response.defer(ephemeral=True)
    p = await make_transcript(interaction.channel)
    await interaction.followup.send("Transcript:", file=discord.File(p), ephemeral=True)

@tree.command(name="cerere-insigna", description="Generează / sincronizează insigna și callsign-ul.")
@app_commands.guild_only()
async def cerere_insigna(interaction: discord.Interaction, username: discord.Member, nume: str, prenume: str, id: str):
    if not await require_priv(interaction): return
    rank = get_member_rank(username)
    if not rank:
        return await safe_ephemeral(interaction, "Membrul nu are un grad C.C.O. configurat pe Discord.")
    await _rank_operation(interaction, username, rank, "Insignă creată", prenume, nume, id)

@tree.command(name="adauga-membru", description="Adaugă un membru C.C.O., îi pune gradul și un callsign liber.")
@app_commands.guild_only()
@app_commands.choices(grad=rank_choices())
async def adauga_membru(interaction: discord.Interaction, username: discord.Member, nume: str, prenume: str, id: str, grad: app_commands.Choice[str]):
    if not await require_leadership(interaction): return
    if fetchone("SELECT 1 FROM members WHERE discord_id=? AND status NOT IN ('removed','out','demisionat')", (str(username.id),)):
        return await safe_ephemeral(interaction, "Membrul există deja în baza C.C.O. Folosește /modifica-membru sau schimbarea de grad.")
    await _rank_operation(interaction, username, grad.value, "Membru adăugat", prenume, nume, id)

@tree.command(name="promoveaza-membru", description="Promovează un membru și îi atribuie callsign-ul corect.")
@app_commands.guild_only()
async def promoveaza(interaction: discord.Interaction, username: discord.Member):
    row = fetchone("SELECT rank FROM members WHERE discord_id=?", (str(username.id),))
    if not row: return await safe_ephemeral(interaction, "Membrul nu este în baza C.C.O.")
    idx = rank_index(row["rank"])
    if idx >= len(RANKS)-1: return await safe_ephemeral(interaction, "Membrul este deja la gradul maxim.")
    await _rank_operation(interaction, username, RANKS[idx+1]["name"], "Promovat")

@tree.command(name="retrogradeaza-membru", description="Retrogradează un membru și îi atribuie callsign-ul corect.")
@app_commands.guild_only()
async def retrogradeaza(interaction: discord.Interaction, username: discord.Member):
    row = fetchone("SELECT rank FROM members WHERE discord_id=?", (str(username.id),))
    if not row: return await safe_ephemeral(interaction, "Membrul nu este în baza C.C.O.")
    idx = rank_index(row["rank"])
    if idx <= 0: return await safe_ephemeral(interaction, "Membrul este deja la gradul minim.")
    await _rank_operation(interaction, username, RANKS[idx-1]["name"], "Retrogradat")

@tree.command(name="elimina-membru", description="Elimină membrul C.C.O. și îi dă kick.")
@app_commands.guild_only()
async def elimina(interaction: discord.Interaction, username: discord.Member, motiv: str):
    actor = interaction.guild.get_member(interaction.user.id)
    ok, reason = can_manage_target(actor, username)
    if not ok: return await safe_ephemeral(interaction, f"⛔ {reason}")
    await interaction.response.send_message(f"🛑 {username.mention} eliminat. Motiv: **{motiv}**")
    audit("remove_member", actor.id, username.id, motiv)
    await remove_member(username, motiv)

@tree.command(name="suspenda-membru", description="Suspendă un membru C.C.O.")
@app_commands.guild_only()
async def suspenda(interaction: discord.Interaction, username: discord.Member, motiv: str):
    actor = interaction.guild.get_member(interaction.user.id)
    ok, reason = can_manage_target(actor, username)
    if not ok: return await safe_ephemeral(interaction, f"⛔ {reason}")
    await suspend_member(username, motiv)
    audit("suspend", actor.id, username.id, motiv)
    await interaction.response.send_message(f"⏸️ {username.mention} suspendat.")

@tree.command(name="reactiveaza-membru", description="Reactivează un membru suspendat.")
@app_commands.guild_only()
async def reactiveaza(interaction: discord.Interaction, username: discord.Member):
    actor = interaction.guild.get_member(interaction.user.id)
    ok, reason = can_manage_target(actor, username)
    if not ok: return await safe_ephemeral(interaction, f"⛔ {reason}")
    await reactivate_member(username)
    audit("reactivate", actor.id, username.id, "")
    await interaction.response.send_message(f"▶️ {username.mention} reactivat.")

@tree.command(name="schimba-grad", description="Setează direct gradul și schimbă callsign-ul dacă este necesar.")
@app_commands.guild_only()
@app_commands.choices(grad=rank_choices())
async def schimba_grad(interaction: discord.Interaction, username: discord.Member, grad: app_commands.Choice[str]):
    await _rank_operation(interaction, username, grad.value, "Grad schimbat")

@tree.command(name="modifica-membru", description="Modifică nume/prenume/ID.")
@app_commands.guild_only()
async def modifica_membru(interaction: discord.Interaction, username: discord.Member, nume: str|None=None, prenume: str|None=None, id: str|None=None):
    if not await require_priv(interaction): return
    row = fetchone("SELECT * FROM members WHERE discord_id=?", (str(username.id),))
    if not row: return await safe_ephemeral(interaction, "Membrul nu există în baza de date.")
    execute("""
        UPDATE members SET first_name=?,last_name=?,internal_id=?,updated_at=CURRENT_TIMESTAMP
        WHERE discord_id=?
    """, (prenume or row["first_name"], nume or row["last_name"], id or row["internal_id"], str(username.id)))
    await sync_nickname(username)
    await interaction.response.send_message("✅ Membru actualizat.", ephemeral=True)

@tree.command(name="membru-info", description="Afișează informațiile unui membru.")
@app_commands.guild_only()
async def membru_info(interaction: discord.Interaction, username: discord.Member):
    row = fetchone("SELECT * FROM members WHERE discord_id=?", (str(username.id),))
    if not row: return await safe_ephemeral(interaction, "Nu există fișă.")
    e = discord.Embed(title=f"{row['callsign'] or '-'} {row['first_name']} {row['last_name']}", colour=0x2F81F7)
    e.add_field(name="Grad", value=row["rank"] or "-", inline=True)
    e.add_field(name="ID", value=row["internal_id"] or "-", inline=True)
    e.add_field(name="Tester", value="Da" if row["tester"] else "Nu", inline=True)
    e.add_field(name="Status", value=row["status"], inline=True)
    e.add_field(name="Pontaj total", value=fmt_min(total_minutes(username.id)), inline=True)
    await interaction.response.send_message(embed=e, ephemeral=True)

@tree.command(name="adauga-tester", description="Adaugă Tester C.C.O.")
@app_commands.guild_only()
async def adauga_tester(interaction: discord.Interaction, username: discord.Member):
    actor = interaction.guild.get_member(interaction.user.id)
    head_id = get_config("role_head_tester")
    allowed = actor.id == owner_id() or is_superuser(actor.id) or (head_id and any(r.id == int(head_id) for r in actor.roles))
    if not allowed: return await safe_ephemeral(interaction, "Doar Șef Tester sau Superuser.")
    rid = get_config("role_tester")
    if rid and (role := interaction.guild.get_role(int(rid))): await username.add_roles(role)
    execute("UPDATE members SET tester=1 WHERE discord_id=?", (str(username.id),))
    await interaction.response.send_message("✅ Tester adăugat.")

@tree.command(name="sterge-tester", description="Șterge Tester C.C.O.")
@app_commands.guild_only()
async def sterge_tester(interaction: discord.Interaction, username: discord.Member):
    actor = interaction.guild.get_member(interaction.user.id)
    head_id = get_config("role_head_tester")
    allowed = actor.id == owner_id() or is_superuser(actor.id) or (head_id and any(r.id == int(head_id) for r in actor.roles))
    if not allowed: return await safe_ephemeral(interaction, "Doar Șef Tester sau Superuser.")
    rid = get_config("role_tester")
    if rid and (role := interaction.guild.get_role(int(rid))): await username.remove_roles(role)
    execute("UPDATE members SET tester=0 WHERE discord_id=?", (str(username.id),))
    await interaction.response.send_message("✅ Tester șters.")

@tree.command(name="adauga-sef-tester", description="Adaugă Șef Tester C.C.O.")
@app_commands.guild_only()
async def adauga_sef_tester(interaction: discord.Interaction, username: discord.Member):
    if interaction.user.id != owner_id() and not is_superuser(interaction.user.id):
        return await safe_ephemeral(interaction, "Doar Superuser.")
    rid = get_config("role_head_tester")
    if rid and (role := interaction.guild.get_role(int(rid))): await username.add_roles(role)
    execute("UPDATE members SET head_tester=1 WHERE discord_id=?", (str(username.id),))
    await interaction.response.send_message("✅ Șef Tester adăugat.")

@tree.command(name="sterge-sef-tester", description="Șterge Șef Tester C.C.O.")
@app_commands.guild_only()
async def sterge_sef_tester(interaction: discord.Interaction, username: discord.Member):
    if interaction.user.id != owner_id() and not is_superuser(interaction.user.id):
        return await safe_ephemeral(interaction, "Doar Superuser.")
    rid = get_config("role_head_tester")
    if rid and (role := interaction.guild.get_role(int(rid))): await username.remove_roles(role)
    execute("UPDATE members SET head_tester=0 WHERE discord_id=?", (str(username.id),))
    await interaction.response.send_message("✅ Șef Tester șters.")

@tree.command(name="set-callsign-range", description="Setează intervalul callsign pentru un grad.")
@app_commands.guild_only()
@app_commands.choices(grad=rank_choices())
async def set_callsign_range(interaction: discord.Interaction, grad: app_commands.Choice[str], prefix: str, start: int, stop: int):
    if not await require_priv(interaction): return
    if is_leadership_rank(grad.value):
        return await safe_ephemeral(interaction, "Gradele de Conducere folosesc callsign-uri text administrate din panel, nu interval numeric.")
    if stop < start: return await safe_ephemeral(interaction, "Stop trebuie să fie >= start.")
    execute("""
        INSERT INTO callsign_ranges(rank,prefix,start_num,end_num) VALUES(?,?,?,?)
        ON CONFLICT(rank) DO UPDATE SET prefix=excluded.prefix,start_num=excluded.start_num,end_num=excluded.end_num
    """, (grad.value, prefix.upper(), start, stop))
    await interaction.response.send_message(f"✅ {grad.value}: **{prefix.upper()}-{start:03d} → {prefix.upper()}-{stop:03d}**")

@tree.command(name="schimba-callsign", description="Schimbă manual callsign-ul.")
@app_commands.guild_only()
async def schimba_callsign(interaction: discord.Interaction, username: discord.Member, callsign: str):
    if not await require_priv(interaction): return
    row = fetchone("SELECT rank FROM members WHERE discord_id=?", (str(username.id),))
    if not row: return await safe_ephemeral(interaction, "Membrul nu există în baza C.C.O.")
    try:
        cs = assign_callsign_db(username.id, row["rank"], preferred=callsign)
        await sync_nickname(username)
        await interaction.response.send_message(f"✅ Callsign schimbat în **{cs}**.")
    except Exception as e:
        await safe_ephemeral(interaction, f"⛔ {e}")

@tree.command(name="elibereaza-callsign", description="Eliberează un callsign.")
@app_commands.guild_only()
async def elibereaza_callsign(interaction: discord.Interaction, callsign: str):
    if not await require_priv(interaction): return
    cs = callsign.upper().strip()
    execute("UPDATE members SET callsign=NULL WHERE callsign=?", (cs,))
    execute("UPDATE leadership_callsigns SET assigned_to=NULL,updated_at=CURRENT_TIMESTAMP WHERE callsign=?", (cs,))
    await interaction.response.send_message("✅ Callsign eliberat.")

@tree.command(name="pontaj", description="Deschide Clock In / Clock Out.")
@app_commands.guild_only()
async def pontaj(interaction: discord.Interaction):
    await interaction.response.send_message("Pontaj C.C.O.", view=PontajView(), ephemeral=True)

@tree.command(name="pontaj-top", description="Top pontaje.")
@app_commands.guild_only()
async def pontaj_top(interaction: discord.Interaction):
    rows = top_timesheets()
    text = "\n".join(
        f"{i+1}. **{r['callsign'] or '-'} {r['first_name'] or ''} {r['last_name'] or ''}** — {fmt_min(max(0,int(r['total_minutes'] or 0)))}"
        for i,r in enumerate(rows)
    ) or "Nu există pontaje."
    await interaction.response.send_message(text, ephemeral=True)

@tree.command(name="pontaj-redu", description="Scade minute din pontaj.")
@app_commands.guild_only()
async def pontaj_redu(interaction: discord.Interaction, username: discord.Member, minute: int):
    if not await require_priv(interaction): return
    adjust_time(username.id, -abs(minute), interaction.user.id, "pontaj-redu")
    await interaction.response.send_message("✅ Pontaj actualizat.")

@tree.command(name="pontaj-adauga", description="Adaugă minute la pontaj.")
@app_commands.guild_only()
async def pontaj_adauga(interaction: discord.Interaction, username: discord.Member, minute: int):
    if not await require_priv(interaction): return
    adjust_time(username.id, abs(minute), interaction.user.id, "pontaj-adauga")
    await interaction.response.send_message("✅ Pontaj actualizat.")

@tree.command(name="pontaj-reset", description="Resetează pontajul.")
@app_commands.guild_only()
async def pontaj_reset(interaction: discord.Interaction, username: discord.Member):
    if not await require_priv(interaction): return
    reset_time(username.id, interaction.user.id)
    await interaction.response.send_message("✅ Pontaj resetat.")

@tree.command(name="forteaza-clockout", description="Închide forțat pontajul activ.")
@app_commands.guild_only()
async def forteaza_clockout(interaction: discord.Interaction, username: discord.Member):
    if not await require_priv(interaction): return
    try: ts = clock_out(username.id)
    except Exception as e: return await safe_ephemeral(interaction, str(e))
    await interaction.response.send_message(f"✅ Închis: **{ts['duration_minutes']} min**.")

@tree.command(name="lista-membri", description="Lista membrilor C.C.O.")
@app_commands.guild_only()
async def lista_membri(interaction: discord.Interaction):
    rows = fetchall("SELECT * FROM members WHERE status='active' ORDER BY callsign")
    text = "\n".join(f"**{r['callsign'] or '-'}** — {r['first_name']} {r['last_name']} — {r['rank']}" for r in rows) or "Gol."
    await interaction.response.send_message(text[:1900], ephemeral=True)

@tree.command(name="lista-conducere", description="Lista conducerii C.C.O.")
@app_commands.guild_only()
async def lista_conducere(interaction: discord.Interaction):
    rows = [r for r in fetchall("SELECT * FROM members WHERE status='active'") if rank_index(r["rank"]) >= LEADERSHIP_MIN_INDEX]
    text = "\n".join(f"**{r['callsign'] or '-'}** — {r['first_name']} {r['last_name']} — {r['rank']}" for r in rows) or "Gol."
    await interaction.response.send_message(text[:1900], ephemeral=True)

@tree.command(name="lista-testeri", description="Lista testerilor C.C.O.")
@app_commands.guild_only()
async def lista_testeri(interaction: discord.Interaction):
    rows = fetchall("SELECT * FROM members WHERE status='active' AND (tester=1 OR head_tester=1) ORDER BY head_tester DESC,callsign")
    text = "\n".join(f"**{r['callsign'] or '-'}** — {r['first_name']} {r['last_name']} — {'Șef Tester' if r['head_tester'] else 'Tester'}" for r in rows) or "Gol."
    await interaction.response.send_message(text[:1900], ephemeral=True)

@tree.command(name="istoric-membru", description="Istoricul administrativ al unui membru.")
@app_commands.guild_only()
async def istoric_membru(interaction: discord.Interaction, username: discord.Member):
    if not await require_priv(interaction): return
    rows = fetchall("SELECT * FROM audit_events WHERE target_id=? ORDER BY id DESC LIMIT 25", (str(username.id),))
    text = "\n".join(f"#{r['id']} **{r['type']}** — {r['details'] or '-'} — {r['created_at']}" for r in rows) or "Fără istoric."
    await interaction.response.send_message(text[:1900], ephemeral=True)

@tree.command(name="activitate", description="Activitatea de pontaj pe ultimele 30 zile.")
@app_commands.guild_only()
async def activitate(interaction: discord.Interaction, username: discord.Member):
    if not await require_priv(interaction): return
    row = fetchone("""
        SELECT COUNT(*) c,COALESCE(SUM(duration_minutes),0) m FROM timesheets
        WHERE user_id=? AND status='closed' AND clock_out >= datetime('now','-30 day')
    """, (str(username.id),))
    await interaction.response.send_message(
        f"{username.mention}: **{row['c']} pontaje / {fmt_min(int(row['m']))}** în 30 zile. Total: **{fmt_min(total_minutes(username.id))}**",
        ephemeral=True
    )

@tree.command(name="inactivi", description="Membrii fără pontaj recent.")
@app_commands.guild_only()
async def inactivi(interaction: discord.Interaction, zile: int=7):
    if not await require_priv(interaction): return
    rows = fetchall("""
        SELECT m.* FROM members m WHERE m.status='active'
        AND NOT EXISTS(
            SELECT 1 FROM timesheets t WHERE t.user_id=m.discord_id AND t.status='closed'
            AND t.clock_out >= datetime('now',?)
        )
    """, (f"-{max(1,zile)} day",))
    text = "\n".join(f"**{r['callsign'] or '-'}** {r['first_name']} {r['last_name']}" for r in rows) or "Niciunul."
    await interaction.response.send_message(text[:1900], ephemeral=True)

@tree.command(name="warn", description="Adaugă avertisment.")
@app_commands.guild_only()
async def warn(interaction: discord.Interaction, username: discord.Member, motiv: str):
    if not await require_priv(interaction): return
    wid = execute("INSERT INTO warnings(user_id,reason,created_by) VALUES(?,?,?)", (str(username.id), motiv, str(interaction.user.id)))
    audit("warn", interaction.user.id, username.id, motiv)
    await interaction.response.send_message(f"⚠️ Warn #{wid} adăugat.")

@tree.command(name="warnuri", description="Afișează avertismente.")
@app_commands.guild_only()
async def warnuri(interaction: discord.Interaction, username: discord.Member):
    rows = fetchall("SELECT * FROM warnings WHERE user_id=? ORDER BY id DESC", (str(username.id),))
    text = "\n".join(f"#{r['id']} — {r['reason']} — <@{r['created_by']}>" for r in rows) or "Fără warn-uri."
    await interaction.response.send_message(text[:1900], ephemeral=True)

@tree.command(name="remove-warn", description="Șterge avertisment.")
@app_commands.guild_only()
async def remove_warn(interaction: discord.Interaction, id: int):
    if not await require_priv(interaction): return
    execute("DELETE FROM warnings WHERE id=?", (id,))
    await interaction.response.send_message(f"✅ Warn #{id} șters.")

@tree.command(name="adauga-superuser", description="Adaugă Superuser.")
@app_commands.guild_only()
async def adauga_superuser(interaction: discord.Interaction, username: discord.Member):
    if interaction.user.id != owner_id():
        return await safe_ephemeral(interaction, "Doar owner-ul principal.")
    execute("INSERT OR IGNORE INTO superusers(user_id,added_by) VALUES(?,?)", (str(username.id), str(interaction.user.id)))
    await interaction.response.send_message("✅ Superuser adăugat.")

@tree.command(name="sterge-superuser", description="Șterge Superuser.")
@app_commands.guild_only()
async def sterge_superuser(interaction: discord.Interaction, username: discord.Member):
    if interaction.user.id != owner_id():
        return await safe_ephemeral(interaction, "Doar owner-ul principal.")
    execute("DELETE FROM superusers WHERE user_id=?", (str(username.id),))
    await interaction.response.send_message("✅ Superuser șters.")

@tree.command(name="whitelist-add", description="Adaugă în whitelist Security.")
@app_commands.guild_only()
async def whitelist_add(interaction: discord.Interaction, username: discord.Member, scope: str="all"):
    if not await require_priv(interaction): return
    execute("INSERT OR IGNORE INTO security_whitelist(user_id,scope) VALUES(?,?)", (str(username.id),scope))
    await interaction.response.send_message("✅ Whitelist actualizat.")

@tree.command(name="whitelist-remove", description="Scoate din whitelist Security.")
@app_commands.guild_only()
async def whitelist_remove(interaction: discord.Interaction, username: discord.Member, scope: str="all"):
    if not await require_priv(interaction): return
    execute("DELETE FROM security_whitelist WHERE user_id=? AND scope=?", (str(username.id),scope))
    await interaction.response.send_message("✅ Whitelist actualizat.")

@tree.command(name="whitelist-list", description="Arată whitelist Security.")
@app_commands.guild_only()
async def whitelist_list(interaction: discord.Interaction):
    if not await require_priv(interaction): return
    rows = fetchall("SELECT * FROM security_whitelist ORDER BY user_id,scope")
    text = "\n".join(f"<@{r['user_id']}> — `{r['scope']}`" for r in rows) or "Gol."
    await interaction.response.send_message(text[:1900], ephemeral=True)

@tree.command(name="server-save", description="Salvează snapshot complet server.")
@app_commands.guild_only()
async def server_save(interaction: discord.Interaction):
    if not await require_priv(interaction): return
    make_snapshot(interaction.guild)
    await interaction.response.send_message("💾 Snapshot salvat.")

@tree.command(name="server-backups", description="Arată ultimele snapshot-uri.")
@app_commands.guild_only()
async def server_backups(interaction: discord.Interaction):
    if not await require_priv(interaction): return
    rows = fetchall("SELECT id,created_at FROM snapshots WHERE guild_id=? ORDER BY id DESC LIMIT 10", (str(interaction.guild.id),))
    text = "\n".join(f"#{r['id']} — {r['created_at']}" for r in rows) or "Fără backup-uri."
    await interaction.response.send_message(text, ephemeral=True)

@tree.command(name="security-config", description="Modifică o setare Security.")
@app_commands.guild_only()
async def security_config(interaction: discord.Interaction, cheie: str, valoare: str):
    if not await require_priv(interaction): return
    set_security(cheie, valoare)
    await interaction.response.send_message(f"✅ `{cheie}` = `{valoare}`")

@tree.command(name="config-rol", description="Configurează un rol folosit de bot.")
@app_commands.guild_only()
async def config_rol(interaction: discord.Interaction, cheie: str, rol: discord.Role):
    if not await require_priv(interaction): return
    set_config(cheie, rol.id)
    await interaction.response.send_message(f"✅ `{cheie}` = {rol.mention}")

@tree.command(name="config-canal", description="Configurează un canal folosit de bot.")
@app_commands.guild_only()
async def config_canal(interaction: discord.Interaction, cheie: str, canal: discord.TextChannel):
    if not await require_priv(interaction): return
    set_config(cheie, canal.id)
    await interaction.response.send_message(f"✅ `{cheie}` = {canal.mention}")

@tree.command(name="config-categorie", description="Configurează o categorie folosită de bot.")
@app_commands.guild_only()
async def config_categorie(interaction: discord.Interaction, cheie: str, categorie: discord.CategoryChannel):
    if not await require_priv(interaction): return
    set_config(cheie, categorie.id)
    await interaction.response.send_message(f"✅ `{cheie}` = **{categorie.name}**")

@tree.command(name="sync-membri", description="Sincronizează membrii din baza de date cu Discord.")
@app_commands.guild_only()
async def sync_membri(interaction: discord.Interaction):
    if not await require_priv(interaction): return
    await interaction.response.defer(ephemeral=True)
    rows = fetchall("SELECT * FROM members WHERE status='active'")
    ok = 0
    for row in rows:
        m = interaction.guild.get_member(int(row["discord_id"]))
        if not m: continue
        try:
            await sync_rank_roles(m, row["rank"])
            await sync_nickname(m)
            ok += 1
        except Exception:
            pass
    await interaction.followup.send(f"✅ Sincronizați: {ok}", ephemeral=True)

@tree.command(name="backup", description="Creează backup al bazei SQLite.")
@app_commands.guild_only()
async def backup(interaction: discord.Interaction):
    if not await require_priv(interaction): return
    dst = Path("backups/db") / f"cco-{int(datetime.now().timestamp())}.sqlite"
    shutil.copy2("data/cco.sqlite", dst)
    await interaction.response.send_message("💾 Backup DB:", file=discord.File(dst), ephemeral=True)

@tree.command(name="set-status", description="Schimbă statusul botului.")
@app_commands.guild_only()
async def set_status(interaction: discord.Interaction, mesaj: str):
    if not await require_priv(interaction): return
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name=mesaj))
    await interaction.response.send_message("✅ Status schimbat.", ephemeral=True)

@tree.command(name="bot-info", description="Informații despre bot.")
@app_commands.guild_only()
async def bot_info(interaction: discord.Interaction):
    e = discord.Embed(title="C.C.O. Bot", colour=0x2F81F7)
    e.add_field(name="Ping", value=f"{round(bot.latency*1000)} ms")
    e.add_field(name="Membri DB", value=str(fetchone("SELECT COUNT(*) c FROM members")["c"]))
    e.add_field(name="Ticket-uri active", value=str(fetchone("SELECT COUNT(*) c FROM tickets WHERE status='open'")["c"]))
    await interaction.response.send_message(embed=e, ephemeral=True)

# ---------- V4 COMMANDS ----------

@tree.command(name="adauga-buletin", description="Adaugă un buletin în Activitatea Operativă.")
@app_commands.guild_only()
async def adauga_buletin(
    interaction: discord.Interaction,
    username: discord.Member,
    nume_prenume: str,
    cnp: str,
    organizatie: str,
    raport: str,
    dovada: discord.Attachment,
):
    actor = await require_cco(interaction)
    if not actor: return
    if username.id != interaction.user.id and not (has_leadership(actor) or is_superuser(actor.id) or actor.id == owner_id()):
        return await safe_ephemeral(interaction, "Poți posta buletinul doar în numele tău.")
    rid = create_operational_record(
        "buletin", username.id, subject_name=nume_prenume, subject_cnp=cnp,
        data={"organizatie": organizatie, "raport": raport}, evidence_url=dovada.url,
    )
    try:
        await post_operational(interaction.guild, rid)
        audit("buletin_add", interaction.user.id, username.id, str(rid))
        await interaction.response.send_message(f"✅ Buletin #{rid} trimis spre verificare.", ephemeral=True)
    except Exception as e:
        await safe_ephemeral(interaction, f"⛔ {e}")


@tree.command(name="adauga-filaj", description="Adaugă un filaj în Activitatea Operativă.")
@app_commands.guild_only()
async def adauga_filaj(
    interaction: discord.Interaction,
    username: discord.Member,
    participanti: str,
    locatie: str,
    raport: str,
    dovada: str,
):
    actor = await require_cco(interaction)
    if not actor: return
    if username.id != interaction.user.id and not (has_leadership(actor) or is_superuser(actor.id) or actor.id == owner_id()):
        return await safe_ephemeral(interaction, "Poți posta filajul doar în numele tău.")
    ids = parse_user_ids(participanti)
    if str(username.id) not in ids:
        ids.insert(0, str(username.id))
    rid = create_operational_record(
        "filaj", username.id, participants=ids,
        data={"locatie": locatie, "raport": raport}, evidence_url=dovada,
    )
    try:
        await post_operational(interaction.guild, rid)
        audit("filaj_add", interaction.user.id, username.id, str(rid))
        await interaction.response.send_message(f"✅ Filaj #{rid} trimis spre verificare.", ephemeral=True)
    except Exception as e:
        await safe_ephemeral(interaction, f"⛔ {e}")


@tree.command(name="adauga-interogatoriu", description="Adaugă un interogatoriu complet.")
@app_commands.guild_only()
async def adauga_interogatoriu(
    interaction: discord.Interaction,
    username: discord.Member,
    nume_suspect: str,
    cnp: str,
    poza_buletin: discord.Attachment,
    dovada: str,
    nume_organizatie: str,
    casa: str,
    cascuta: str,
    nume_lider: str,
    cnp_lider: str,
    nume_colider: str,
    cnp_colider: str,
    numar_membri: int,
    ocupatie: str,
    imbracaminte: str,
    locuri_frecventate: str,
    aliante: str,
    suplimentar: str = "",
):
    actor = await require_cco(interaction)
    if not actor: return
    if username.id != interaction.user.id and not (has_leadership(actor) or is_superuser(actor.id) or actor.id == owner_id()):
        return await safe_ephemeral(interaction, "Poți posta interogatoriul doar în numele tău.")
    data = {
        "nume_organizatie": nume_organizatie,
        "casa": casa,
        "cascuta": cascuta,
        "nume_lider": nume_lider,
        "cnp_lider": cnp_lider,
        "nume_colider": nume_colider,
        "cnp_colider": cnp_colider,
        "numar_membri": numar_membri,
        "cu_ce_se_ocupa": ocupatie,
        "imbracaminte": imbracaminte,
        "unde_isi_petrec_timpul": locuri_frecventate,
        "aliante": aliante,
        "suplimentar": suplimentar,
        "poza_buletin": poza_buletin.url,
    }
    rid = create_operational_record(
        "interogatoriu", username.id, subject_name=nume_suspect, subject_cnp=cnp,
        data=data, evidence_url=dovada,
    )
    try:
        await post_operational(interaction.guild, rid)
        audit("interogatoriu_add", interaction.user.id, username.id, str(rid))
        await interaction.response.send_message(f"✅ Interogatoriu #{rid} trimis spre verificare.", ephemeral=True)
    except Exception as e:
        await safe_ephemeral(interaction, f"⛔ {e}")


@tree.command(name="cerere-actiune", description="Cere aprobarea unei acțiuni ca Șef Serviciu Investigații.")
@app_commands.guild_only()
async def cerere_actiune(interaction: discord.Interaction, username: discord.Member, tip_actiune: str, ora: str):
    actor = await require_cco(interaction)
    if not actor: return
    if username.id != interaction.user.id:
        return await safe_ephemeral(interaction, "Cererea de acțiune trebuie făcută în numele tău.")
    row = fetchone("SELECT rank FROM members WHERE discord_id=?", (str(username.id),))
    if not row or rank_index(row["rank"]) != SSI_INDEX:
        return await safe_ephemeral(interaction, "Această comandă este pentru Șef Serviciu Investigații.")
    try:
        req_id = create_action_request(username.id, tip_actiune, ora)
        req = fetchone("SELECT * FROM action_requests WHERE id=?", (req_id,))
        ch = await configured_text_channel(interaction.guild, "log_cereri_actiune")
        e = discord.Embed(title=f"📨 Cerere Acțiune #{req_id}", colour=0xE5A50A)
        e.add_field(name="Solicitant", value=username.mention, inline=True)
        e.add_field(name="Tip acțiune", value=tip_actiune, inline=True)
        e.add_field(name="Ora solicitată", value=ora, inline=True)
        e.add_field(name="Status", value="🟡 ÎN AȘTEPTARE", inline=False)
        msg = await ch.send(embed=e, view=ApprovalView("action_request", req_id))
        execute("UPDATE action_requests SET channel_id=?,message_id=? WHERE id=?", (str(ch.id), str(msg.id), req_id))
        await interaction.response.send_message(f"✅ Cererea #{req_id} a fost trimisă Conducerii.", ephemeral=True)
    except Exception as e:
        await safe_ephemeral(interaction, f"⛔ {e}")


@tree.command(name="adauga-actiune", description="Adaugă o acțiune operativă.")
@app_commands.guild_only()
async def adauga_actiune(
    interaction: discord.Interaction,
    coordonatori: str,
    participanti: str,
    tip_actiune: str,
    ora: str,
    dovada_fisier: discord.Attachment | None = None,
    dovada_link: str | None = None,
):
    actor = await require_cco(interaction)
    if not actor: return
    db = fetchone("SELECT rank FROM members WHERE discord_id=?", (str(actor.id),))
    idx = rank_index(db["rank"] if db else None)
    request_row = None
    if idx == SSI_INDEX:
        request_row = approved_action_request(actor.id, tip_actiune)
        if not request_row:
            return await safe_ephemeral(interaction, "Nu ai o cerere de acțiune aprobată și validă în intervalul ±1 oră.")
    elif idx < LEADERSHIP_MIN_INDEX and not (is_superuser(actor.id) or actor.id == owner_id()):
        return await safe_ephemeral(interaction, "Doar Conducerea sau un Șef Serviciu Investigații cu cerere aprobată poate crea acțiunea.")

    coords = parse_user_ids(coordonatori)
    parts = parse_user_ids(participanti)
    evidence = dovada_link or (dovada_fisier.url if dovada_fisier else None)
    rid = create_operational_record(
        "actiune", actor.id, participants=list(dict.fromkeys(coords + parts)),
        data={"coordonatori": " ".join(f"<@{x}>" for x in coords), "participanti": " ".join(f"<@{x}>" for x in parts), "tip_actiune": tip_actiune, "ora": ora},
        evidence_url=evidence,
    )
    if request_row:
        execute("UPDATE action_requests SET used_record_id=? WHERE id=?", (rid, request_row["id"]))
    try:
        await post_operational(interaction.guild, rid)
        audit("actiune_add", interaction.user.id, interaction.user.id, str(rid))
        await interaction.response.send_message(f"✅ Acțiune #{rid} trimisă spre verificare.", ephemeral=True)
    except Exception as e:
        await safe_ephemeral(interaction, f"⛔ {e}")


@tree.command(name="demisie", description="Depune o cerere de demisie C.C.O.")
@app_commands.guild_only()
async def demisie(interaction: discord.Interaction, username: discord.Member, motiv: str, precizari: str, pk: bool):
    actor = await require_cco(interaction)
    if not actor: return
    if username.id != interaction.user.id and not (is_superuser(actor.id) or actor.id == owner_id()):
        return await safe_ephemeral(interaction, "Poți depune demisia doar în numele tău.")
    if fetchone("SELECT 1 FROM resignations WHERE user_id=? AND status='pending'", (str(username.id),)):
        return await safe_ephemeral(interaction, "Ai deja o cerere de demisie în așteptare.")
    req_id = execute(
        "INSERT INTO resignations(user_id,reason,notes,accepts_pk) VALUES(?,?,?,?)",
        (str(username.id), motiv, precizari, 1 if pk else 0),
    )
    try:
        ch = await configured_text_channel(interaction.guild, "log_demisie")
        e = discord.Embed(title=f"🚪 Cerere Demisie #{req_id}", colour=0xE5A50A)
        e.add_field(name="Membru", value=username.mention, inline=True)
        e.add_field(name="Motiv", value=motiv[:1024], inline=False)
        e.add_field(name="Precizări", value=(precizari or "—")[:1024], inline=False)
        e.add_field(name="Este de acord cu PK", value="DA" if pk else "NU", inline=True)
        e.add_field(name="Status", value="🟡 ÎN AȘTEPTARE", inline=True)
        msg = await ch.send(embed=e, view=ApprovalView("resignation", req_id))
        execute("UPDATE resignations SET channel_id=?,message_id=? WHERE id=?", (str(ch.id), str(msg.id), req_id))
        await interaction.response.send_message("✅ Cererea de demisie a fost trimisă.", ephemeral=True)
    except Exception as e:
        await safe_ephemeral(interaction, f"⛔ {e}")


@tree.command(name="cerere-inactivitate", description="Depune o cerere de inactivitate.")
@app_commands.guild_only()
async def cerere_inactivitate(interaction: discord.Interaction, username: discord.Member, timp: str, motiv: str):
    actor = await require_cco(interaction)
    if not actor: return
    if username.id != interaction.user.id and not (is_superuser(actor.id) or actor.id == owner_id()):
        return await safe_ephemeral(interaction, "Poți depune inactivitatea doar în numele tău.")
    try:
        days = parse_duration_days(timp)
    except Exception as e:
        return await safe_ephemeral(interaction, f"⛔ {e}")
    if fetchone("SELECT 1 FROM inactivity_requests WHERE user_id=? AND status='pending'", (str(username.id),)):
        return await safe_ephemeral(interaction, "Ai deja o cerere de inactivitate în așteptare.")
    req_id = execute(
        "INSERT INTO inactivity_requests(user_id,duration_days,reason) VALUES(?,?,?)",
        (str(username.id), days, motiv),
    )
    try:
        ch = await configured_text_channel(interaction.guild, "log_inactivitate")
        e = discord.Embed(title=f"🏖️ Cerere Inactivitate #{req_id}", colour=0xE5A50A)
        e.add_field(name="Membru", value=username.mention, inline=True)
        e.add_field(name="Perioadă", value=f"{days} zile", inline=True)
        e.add_field(name="Motiv", value=motiv[:1024], inline=False)
        e.add_field(name="Status", value="🟡 ÎN AȘTEPTARE", inline=False)
        msg = await ch.send(embed=e, view=ApprovalView("inactivity", req_id))
        execute("UPDATE inactivity_requests SET channel_id=?,message_id=? WHERE id=?", (str(ch.id), str(msg.id), req_id))
        await interaction.response.send_message("✅ Cererea de inactivitate a fost trimisă.", ephemeral=True)
    except Exception as e:
        await safe_ephemeral(interaction, f"⛔ {e}")


@tree.command(name="sanctiune", description="Aplică AV/FW unui membru. Toate sancțiunile expiră după 7 zile.")
@app_commands.guild_only()
@app_commands.choices(sanctiune=[app_commands.Choice(name="AV", value="AV"), app_commands.Choice(name="FW", value="FW")])
async def sanctiune(
    interaction: discord.Interaction,
    autor: discord.Member,
    username_sanctionat: discord.Member,
    sanctiune: app_commands.Choice[str],
    motiv: str,
    dovada_fisier: discord.Attachment | None = None,
    dovada_link: str | None = None,
):
    actor = await require_leadership(interaction)
    if not actor:
        return
    if autor.id != interaction.user.id and not (is_superuser(actor.id) or actor.id == owner_id()):
        return await safe_ephemeral(interaction, "Autorul sancțiunii trebuie să fii tu.")
    ok, reason = can_manage_target(actor, username_sanctionat)
    if not ok:
        return await safe_ephemeral(interaction, f"⛔ {reason}")

    evidence = dovada_link or (dovada_fisier.url if dovada_fisier else None)
    try:
        sid, effective_type, stage, total, out, converted = await create_sanction(
            interaction.guild,
            autor.id,
            username_sanctionat,
            sanctiune.value,
            motiv,
            evidence,
        )

        ch = await configured_text_channel(interaction.guild, "log_sanctiuni")
        title_type = f"AV → FW {stage}/{total}" if converted else f"{effective_type} {stage}/{total}"
        e = discord.Embed(title=f"⚠️ Sancțiune #{sid} — {title_type}", colour=0xD9534F)
        e.add_field(name="Sancționat", value=username_sanctionat.mention, inline=True)
        e.add_field(name="Aplicată de", value=autor.mention, inline=True)
        e.add_field(name="Durată", value="7 zile", inline=True)
        if converted:
            e.add_field(
                name="Conversie automată",
                value="Membrul avea deja AV 1/1 activ, deci noul AV a fost transformat automat în FW.",
                inline=False,
            )
        e.add_field(name="Motiv", value=motiv[:1024], inline=False)
        if evidence:
            e.add_field(name="Dovadă", value=evidence[:1024], inline=False)
        if out:
            e.add_field(
                name="OUT AUTOMAT",
                value="Membrul a ajuns la FW 3/3 și a fost eliminat automat.",
                inline=False,
            )

        msg = await ch.send(embed=e)
        execute("UPDATE sanctions SET channel_id=?,message_id=? WHERE id=?", (str(ch.id), str(msg.id), sid))

        shown = f"AV → FW {stage}/{total}" if converted else f"{effective_type} {stage}/{total}"
        await interaction.response.send_message(
            f"✅ {shown} aplicat lui {username_sanctionat.mention}. Expiră automat în 7 zile."
            + (" **OUT automat.**" if out else ""),
            ephemeral=True,
        )
    except Exception as e:
        await safe_ephemeral(interaction, f"⛔ {e}")


def start_dashboard_thread():
    threading.Thread(target=run_dashboard, daemon=True).start()
