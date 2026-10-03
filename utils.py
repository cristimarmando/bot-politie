import os
from datetime import datetime
from zoneinfo import ZoneInfo
from .db import get_config, is_superuser, fetchone
from .config.ranks import RANKS, rank_index, LEADERSHIP_MIN_INDEX

def owner_id():
    try:
        return int(os.getenv("OWNER_ID", "0"))
    except ValueError:
        return 0

def bucharest_now():
    return datetime.now(ZoneInfo(os.getenv("TIMEZONE", "Europe/Bucharest")))

def fmt_dt(dt):
    return dt.astimezone(ZoneInfo(os.getenv("TIMEZONE", "Europe/Bucharest"))).strftime("%H:%M")

def has_role(member, role_id):
    if not role_id:
        return False
    try:
        rid = int(role_id)
    except (TypeError, ValueError):
        return False
    return any(r.id == rid for r in member.roles)

def has_leadership(member):
    return has_role(member, get_config("role_leadership"))

def is_privileged(member):
    return member.id == owner_id() or is_superuser(member.id) or has_leadership(member)

def get_member_rank(member):
    row = fetchone("SELECT rank FROM members WHERE discord_id=?", (str(member.id),))
    if row and row["rank"]:
        return row["rank"]
    for r in RANKS:
        rid = get_config(f"rank_role_{r['key']}")
        if rid and has_role(member, rid):
            return r["name"]
    return None

def can_manage_target(actor, target, desired_rank=None):
    if not actor or not target:
        return False, "Membru invalid."

    # Ownerul și Superuserii pot administra inclusiv propriul profil/grad.
    # Orice self-action este auditat separat în main.py / panel.
    if actor.id == owner_id() or is_superuser(actor.id):
        return True, None

    if actor.id == target.id:
        return False, "Doar Ownerul/Superuserul își poate modifica propriul grad sau profil."

    if not has_leadership(actor):
        return False, "Doar Conducerea C.C.O. poate folosi această comandă."

    actor_idx = rank_index(get_member_rank(actor))
    target_idx = rank_index(get_member_rank(target))

    if actor_idx < LEADERSHIP_MIN_INDEX:
        return False, "Nu ai un grad de conducere valid."

    if target_idx > actor_idx:
        return False, "Nu poți acționa asupra unui membru cu grad superior ție."

    if desired_rank:
        desired_idx = rank_index(desired_rank)
        if desired_idx > actor_idx:
            return False, "Nu poți promova/seta un membru la un grad superior gradului tău."

    return True, None
