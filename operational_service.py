import json
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import discord

from ..db import execute, fetchall, fetchone, get_config, audit
from ..config.ranks import rank_index, LEADERSHIP_MIN_INDEX, SSI_INDEX
from .member_service import remove_member


def parse_user_ids(value: str):
    if not value:
        return []
    ids = re.findall(r"\d{15,22}", value)
    out = []
    for x in ids:
        if x not in out:
            out.append(x)
    return out


def parse_duration_days(value: str) -> int:
    raw = (value or "").strip().lower().replace(" ", "")
    m = re.fullmatch(r"(\d+)(d|zi|zile)?", raw)
    if not m:
        raise RuntimeError("Timp invalid. Folosește de exemplu 3d, 7d, 14d sau 30d.")
    days = int(m.group(1))
    if days < 1 or days > 365:
        raise RuntimeError("Perioada trebuie să fie între 1 și 365 zile.")
    return days


def utcnow_iso():
    return datetime.now(timezone.utc).isoformat()


def local_tz():
    import os
    return ZoneInfo(os.getenv("TIMEZONE", "Europe/Bucharest"))


def iso_from_local_clock(clock: str):
    try:
        hh, mm = [int(x) for x in clock.strip().split(":", 1)]
        if hh not in range(24) or mm not in range(60):
            raise ValueError
    except Exception:
        raise RuntimeError("Ora trebuie să fie în format HH:MM, de exemplu 15:00.")
    now = datetime.now(local_tz())
    dt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return dt


def create_operational_record(kind, author_id, *, subject_name=None, subject_cnp=None,
                              participants=None, data=None, evidence_url=None):
    return execute(
        """
        INSERT INTO operational_records(kind,author_id,subject_name,subject_cnp,participants_json,data_json,evidence_url)
        VALUES(?,?,?,?,?,?,?)
        """,
        (
            kind, str(author_id), subject_name, subject_cnp,
            json.dumps(participants or [], ensure_ascii=False),
            json.dumps(data or {}, ensure_ascii=False), evidence_url,
        ),
    )


def set_record_message(record_id, channel_id, message_id):
    execute(
        "UPDATE operational_records SET channel_id=?,message_id=? WHERE id=?",
        (str(channel_id), str(message_id), int(record_id)),
    )


def operational_counts(user_id):
    uid = str(user_id)
    result = {}
    for kind in ("buletin", "filaj", "interogatoriu", "actiune"):
        # autorul sau participantul; JSON este controlat intern, LIKE este suficient aici.
        row = fetchone(
            """
            SELECT COUNT(*) c FROM operational_records
            WHERE kind=? AND status='accepted'
              AND (author_id=? OR participants_json LIKE ?)
            """,
            (kind, uid, f'%"{uid}"%'),
        )
        result[kind] = int(row["c"] or 0)
    return result


async def add_role_from_config(member: discord.Member, key: str, reason: str):
    rid = get_config(key)
    if not rid:
        raise RuntimeError(f"Rolul `{key}` nu este configurat.")
    role = member.guild.get_role(int(rid))
    if not role:
        raise RuntimeError(f"Rolul configurat pentru `{key}` nu mai există.")
    await member.add_roles(role, reason=reason)
    return role


async def remove_role_from_config(member: discord.Member, key: str, reason: str):
    rid = get_config(key)
    if not rid:
        return
    role = member.guild.get_role(int(rid))
    if role and role in member.roles:
        await member.remove_roles(role, reason=reason)


async def sync_sanction_roles(member: discord.Member):
    # eliminăm rolurile de sancțiune și recalculăm din sancțiunile active
    for key in ("role_av_1_1", "role_fw_1_3", "role_fw_2_3", "role_fw_3_3"):
        await remove_role_from_config(member, key, "C.C.O. sincronizare sancțiuni")

    av = fetchone(
        "SELECT COUNT(*) c FROM sanctions WHERE user_id=? AND type='AV' AND status='active' AND (expires_at IS NULL OR julianday(expires_at)>julianday('now'))",
        (str(member.id),),
    )["c"]
    fw = fetchone(
        "SELECT COUNT(*) c FROM sanctions WHERE user_id=? AND type='FW' AND status='active' AND (expires_at IS NULL OR julianday(expires_at)>julianday('now'))",
        (str(member.id),),
    )["c"]

    if av:
        await add_role_from_config(member, "role_av_1_1", "C.C.O. AV 1/1")
    if fw:
        stage = min(3, int(fw))
        await add_role_from_config(member, f"role_fw_{stage}_3", f"C.C.O. FW {stage}/3")
    return int(av), int(fw)


async def create_sanction(guild, author_id, target: discord.Member, sanction_type, reason, evidence_url=None):
    """Aplică o sancțiune V6.

    Reguli:
    - Toate sancțiunile expiră după exact 7 zile.
    - Primul AV activ = AV 1/1.
    - Dacă membrul are deja un AV activ și primește încă un AV,
      noua sancțiune este echivalentă automat cu un FW.
    - FW-urile active progresează 1/3 -> 2/3 -> 3/3.
    - FW 3/3 = OUT automat.

    Returnează: (id, effective_type, stage, total, out, converted_from_av)
    """
    requested_type = sanction_type.upper().strip()
    if requested_type not in {"AV", "FW"}:
        raise RuntimeError("Sancțiunea trebuie să fie AV sau FW.")

    # V6: durata este fixă pentru AV și FW.
    days = 7
    now = datetime.now(timezone.utc)
    expiry = now + timedelta(days=days)

    active_av = int(fetchone(
        "SELECT COUNT(*) c FROM sanctions WHERE user_id=? AND type='AV' AND status='active' "
        "AND (expires_at IS NULL OR julianday(expires_at)>julianday('now'))",
        (str(target.id),),
    )["c"] or 0)
    active_fw = int(fetchone(
        "SELECT COUNT(*) c FROM sanctions WHERE user_id=? AND type='FW' AND status='active' "
        "AND (expires_at IS NULL OR julianday(expires_at)>julianday('now'))",
        (str(target.id),),
    )["c"] or 0)

    converted_from_av = requested_type == "AV" and active_av >= 1
    effective_type = "FW" if converted_from_av else requested_type

    if effective_type == "AV":
        stage = 1
        total = 1
        role_key = "role_av_1_1"
    else:
        stage = min(3, active_fw + 1)
        total = 3
        role_key = f"role_fw_{stage}_3"

    rid = get_config(role_key)
    role = guild.get_role(int(rid)) if rid else None
    if not role:
        raise RuntimeError(f"Rolul `{role_key}` nu este configurat sau nu mai există.")

    sid = execute(
        """
        INSERT INTO sanctions(author_id,user_id,type,requested_type,stage,total_stages,days,reason,evidence_url,expires_at)
        VALUES(?,?,?,?,?,?,?,?,?,?)
        """,
        (
            str(author_id), str(target.id), effective_type, requested_type,
            stage, total, days, reason, evidence_url, expiry.isoformat(),
        ),
    )

    await sync_sanction_roles(target)
    detail = f"{effective_type} {stage}/{total} | {reason}"
    if converted_from_av:
        detail = f"AV -> FW {stage}/{total} | {reason}"
    audit("sanction_add", author_id, target.id, detail)

    out = effective_type == "FW" and stage >= 3
    if out:
        execute(
            "UPDATE members SET status='out',out_reason=?,updated_at=CURRENT_TIMESTAMP WHERE discord_id=?",
            (f"FW 3/3: {reason}", str(target.id)),
        )
        await remove_member(target, f"OUT automat – FW 3/3: {reason}", status="out")

    return sid, effective_type, stage, total, out, converted_from_av


async def expire_state(guild: discord.Guild):
    # Sancțiuni expirate
    execute("UPDATE sanctions SET status='expired' WHERE status='active' AND expires_at IS NOT NULL AND julianday(expires_at)<=julianday('now')")
    for row in fetchall("SELECT discord_id FROM members WHERE status IN ('active','inactive','suspended')"):
        member = guild.get_member(int(row["discord_id"]))
        if member:
            try:
                await sync_sanction_roles(member)
            except Exception:
                pass

    # Inactivități expirate
    rows = fetchall(
        "SELECT discord_id FROM members WHERE status='inactive' AND inactivity_until IS NOT NULL AND julianday(inactivity_until)<=julianday('now')"
    )
    for row in rows:
        member = guild.get_member(int(row["discord_id"]))
        execute("UPDATE members SET status='active',inactivity_until=NULL,updated_at=CURRENT_TIMESTAMP WHERE discord_id=?", (row["discord_id"],))
        if member:
            await remove_role_from_config(member, "role_inactive", "C.C.O. inactivitate expirată")
        audit("inactivity_expired", None, row["discord_id"], "")


async def approve_inactivity(guild, request_id, reviewer_id):
    row = fetchone("SELECT * FROM inactivity_requests WHERE id=?", (int(request_id),))
    if not row or row["status"] != "pending":
        raise RuntimeError("Cererea nu mai este în așteptare.")
    target = guild.get_member(int(row["user_id"]))
    if not target:
        raise RuntimeError("Membrul nu mai este pe server.")
    rid = get_config("role_inactive")
    if not rid or not guild.get_role(int(rid)):
        raise RuntimeError("Rolul `role_inactive` nu este configurat sau nu mai există.")
    now = datetime.now(timezone.utc)
    end = now + timedelta(days=int(row["duration_days"]))
    execute(
        """
        UPDATE inactivity_requests SET status='accepted',reviewer_id=?,reviewed_at=CURRENT_TIMESTAMP,starts_at=?,ends_at=? WHERE id=?
        """,
        (str(reviewer_id), now.isoformat(), end.isoformat(), int(request_id)),
    )
    execute(
        "UPDATE members SET status='inactive',inactivity_until=?,updated_at=CURRENT_TIMESTAMP WHERE discord_id=?",
        (end.isoformat(), str(target.id)),
    )
    await add_role_from_config(target, "role_inactive", "C.C.O. cerere inactivitate aprobată")
    audit("inactivity_accept", reviewer_id, target.id, f"{row['duration_days']}d")
    return end


async def reject_inactivity(request_id, reviewer_id, reason=""):
    row = fetchone("SELECT * FROM inactivity_requests WHERE id=?", (int(request_id),))
    if not row or row["status"] != "pending":
        raise RuntimeError("Cererea nu mai este în așteptare.")
    execute(
        "UPDATE inactivity_requests SET status='rejected',reviewer_id=?,review_reason=?,reviewed_at=CURRENT_TIMESTAMP WHERE id=?",
        (str(reviewer_id), reason, int(request_id)),
    )
    audit("inactivity_reject", reviewer_id, row["user_id"], reason)


async def accept_resignation(guild, request_id, reviewer: discord.Member, owner=False, superuser=False):
    row = fetchone("SELECT * FROM resignations WHERE id=?", (int(request_id),))
    if not row or row["status"] != "pending":
        raise RuntimeError("Cererea nu mai este în așteptare.")
    target = guild.get_member(int(row["user_id"]))
    if not target:
        raise RuntimeError("Membrul nu mai este pe server.")
    target_db = fetchone("SELECT rank FROM members WHERE discord_id=?", (row["user_id"],))
    reviewer_db = fetchone("SELECT rank FROM members WHERE discord_id=?", (str(reviewer.id),))
    target_idx = rank_index(target_db["rank"] if target_db else None)
    reviewer_idx = rank_index(reviewer_db["rank"] if reviewer_db else None)
    if target_idx >= LEADERSHIP_MIN_INDEX and not (owner or superuser) and reviewer_idx <= target_idx:
        raise RuntimeError("Demisia unui membru din Conducere poate fi acceptată doar de un grad superior.")

    execute(
        "UPDATE resignations SET status='accepted',reviewer_id=?,reviewed_at=CURRENT_TIMESTAMP WHERE id=?",
        (str(reviewer.id), int(request_id)),
    )
    audit("resignation_accept", reviewer.id, target.id, row["reason"])
    await remove_member(target, f"Demisie acceptată: {row['reason']}", status="demisionat")


async def reject_resignation(request_id, reviewer_id, reason=""):
    row = fetchone("SELECT * FROM resignations WHERE id=?", (int(request_id),))
    if not row or row["status"] != "pending":
        raise RuntimeError("Cererea nu mai este în așteptare.")
    execute(
        "UPDATE resignations SET status='rejected',reviewer_id=?,review_reason=?,reviewed_at=CURRENT_TIMESTAMP WHERE id=?",
        (str(reviewer_id), reason, int(request_id)),
    )
    audit("resignation_reject", reviewer_id, row["user_id"], reason)


def approved_action_request(user_id, action_type, now=None):
    now = now or datetime.now(timezone.utc)
    rows = fetchall(
        """
        SELECT * FROM action_requests
        WHERE requester_id=? AND action_type=? AND status='accepted' AND used_record_id IS NULL
        ORDER BY id DESC
        """,
        (str(user_id), action_type),
    )
    for r in rows:
        try:
            start = datetime.fromisoformat(r["valid_from"])
            end = datetime.fromisoformat(r["valid_until"])
            if start <= now <= end:
                return r
        except Exception:
            continue
    return None


def create_action_request(user_id, action_type, requested_clock):
    requested_local = iso_from_local_clock(requested_clock)
    requested_utc = requested_local.astimezone(timezone.utc)
    return execute(
        "INSERT INTO action_requests(requester_id,action_type,requested_at) VALUES(?,?,?)",
        (str(user_id), action_type, requested_utc.isoformat()),
    )


def approve_action_request(request_id, reviewer_id):
    row = fetchone("SELECT * FROM action_requests WHERE id=?", (int(request_id),))
    if not row or row["status"] != "pending":
        raise RuntimeError("Cererea nu mai este în așteptare.")
    requested = datetime.fromisoformat(row["requested_at"])
    start = requested - timedelta(hours=1)
    end = requested + timedelta(hours=1)
    execute(
        """
        UPDATE action_requests SET status='accepted',reviewer_id=?,valid_from=?,valid_until=? WHERE id=?
        """,
        (str(reviewer_id), start.isoformat(), end.isoformat(), int(request_id)),
    )
    audit("action_request_accept", reviewer_id, row["requester_id"], f"{row['action_type']} | {row['requested_at']}")
    return start, end


def reject_action_request(request_id, reviewer_id, reason=""):
    row = fetchone("SELECT * FROM action_requests WHERE id=?", (int(request_id),))
    if not row or row["status"] != "pending":
        raise RuntimeError("Cererea nu mai este în așteptare.")
    execute(
        "UPDATE action_requests SET status='rejected',reviewer_id=?,review_reason=? WHERE id=?",
        (str(reviewer_id), reason, int(request_id)),
    )
    audit("action_request_reject", reviewer_id, row["requester_id"], reason)
