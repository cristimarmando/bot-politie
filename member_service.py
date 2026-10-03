import discord
from ..db import fetchone, fetchall, execute, get_config, audit
from ..config.ranks import RANKS, rank_index, rank_by_name, LEADERSHIP_MIN_INDEX, is_leadership_rank


def _occupied_callsigns(exclude_user_id=None):
    sql = "SELECT discord_id,callsign FROM members WHERE callsign IS NOT NULL AND status NOT IN ('removed','out','demisionat')"
    rows = fetchall(sql)
    return {
        r["callsign"].upper()
        for r in rows
        if r["callsign"] and (exclude_user_id is None or str(r["discord_id"]) != str(exclude_user_id))
    }


def normal_callsigns(rank_name):
    rng = fetchone("SELECT * FROM callsign_ranges WHERE rank=?", (rank_name,))
    if not rng:
        return []
    occupied = _occupied_callsigns()
    reserved = {r["callsign"].upper() for r in fetchall("SELECT callsign FROM callsign_reservations")}
    result = []
    for n in range(int(rng["start_num"]), int(rng["end_num"]) + 1):
        candidate = f"{rng['prefix']}-{n:03d}".upper()
        if candidate not in occupied and candidate not in reserved:
            result.append(candidate)
    return result


def leadership_callsigns_available(rank_name, exclude_user_id=None):
    occupied = _occupied_callsigns(exclude_user_id)
    rows = fetchall(
        "SELECT * FROM leadership_callsigns WHERE enabled=1 AND rank=? ORDER BY callsign COLLATE NOCASE",
        (rank_name,),
    )
    out = []
    for r in rows:
        cs = r["callsign"].strip().upper()
        assigned = r["assigned_to"]
        if assigned and str(assigned) != str(exclude_user_id):
            continue
        if cs not in occupied:
            out.append(cs)
    return out


def available_callsigns(rank_name, exclude_user_id=None):
    if is_leadership_rank(rank_name):
        return leadership_callsigns_available(rank_name, exclude_user_id)
    return normal_callsigns(rank_name)


def next_callsign(rank_name, preferred=None, exclude_user_id=None):
    available = available_callsigns(rank_name, exclude_user_id)
    if preferred:
        wanted = preferred.strip().upper()
        if wanted not in available:
            raise RuntimeError(f"Callsign-ul {wanted} nu este liber pentru {rank_name}.")
        return wanted
    if not available:
        raise RuntimeError(f"Nu mai există callsign-uri libere pentru {rank_name}.")
    if is_leadership_rank(rank_name) and len(available) > 1:
        raise RuntimeError("CALLSIGN_SELECTION_REQUIRED:" + "|".join(available))
    return available[0]


def _release_leadership_assignment(user_id, callsign=None):
    if callsign:
        execute(
            "UPDATE leadership_callsigns SET assigned_to=NULL,updated_at=CURRENT_TIMESTAMP WHERE callsign=? AND assigned_to=?",
            (callsign, str(user_id)),
        )
    else:
        execute(
            "UPDATE leadership_callsigns SET assigned_to=NULL,updated_at=CURRENT_TIMESTAMP WHERE assigned_to=?",
            (str(user_id),),
        )


def assign_callsign_db(user_id, rank_name, preferred=None):
    current = fetchone("SELECT callsign,rank FROM members WHERE discord_id=?", (str(user_id),))
    old_cs = current["callsign"] if current else None
    old_rank = current["rank"] if current else None

    # Păstrăm callsign-ul text doar dacă gradul NU s-a schimbat și callsign-ul aparține acelui grad.
    if old_cs and old_rank == rank_name and is_leadership_rank(rank_name) and not preferred:
        slot = fetchone("SELECT 1 FROM leadership_callsigns WHERE callsign=? AND rank=?", (old_cs, rank_name))
        if slot:
            execute("UPDATE leadership_callsigns SET assigned_to=? WHERE callsign=?", (str(user_id), old_cs))
            return old_cs

    cs = next_callsign(rank_name, preferred=preferred, exclude_user_id=user_id)

    if old_rank and is_leadership_rank(old_rank) and (not is_leadership_rank(rank_name) or (old_cs and old_cs.upper() != cs.upper())):
        _release_leadership_assignment(user_id, old_cs)

    if is_leadership_rank(rank_name):
        slot = fetchone("SELECT id FROM leadership_callsigns WHERE callsign=? AND rank=? AND enabled=1", (cs, rank_name))
        if not slot:
            raise RuntimeError(f"Callsign-ul {cs} nu aparține gradului {rank_name}.")
        execute(
            "UPDATE leadership_callsigns SET assigned_to=?,updated_at=CURRENT_TIMESTAMP WHERE callsign=? AND rank=?",
            (str(user_id), cs, rank_name),
        )

    execute("UPDATE members SET callsign=?,updated_at=CURRENT_TIMESTAMP WHERE discord_id=?", (cs, str(user_id)))
    return cs


async def sync_rank_roles(member: discord.Member, rank_name: str):
    idx = rank_index(rank_name)
    if idx < 0:
        raise RuntimeError("Grad invalid.")

    all_rank_ids = []
    for r in RANKS:
        rid = get_config(f"rank_role_{r['key']}")
        if rid:
            all_rank_ids.append(int(rid))

    to_remove = [r for r in member.roles if r.id in all_rank_ids]
    if to_remove:
        await member.remove_roles(*to_remove, reason="C.C.O. sincronizare grad")

    rank = rank_by_name(rank_name)
    rid = get_config(f"rank_role_{rank['key']}")
    if not rid:
        raise RuntimeError(f"Rolul Discord pentru {rank_name} nu este configurat.")
    role = member.guild.get_role(int(rid))
    if not role:
        raise RuntimeError(f"Rolul Discord pentru {rank_name} nu mai există.")
    await member.add_roles(role, reason="C.C.O. grad")

    cco_id = get_config("role_cco")
    if cco_id and (cco_role := member.guild.get_role(int(cco_id))):
        await member.add_roles(cco_role, reason="C.C.O. membru")

    lead_id = get_config("role_leadership")
    if lead_id and (lead_role := member.guild.get_role(int(lead_id))):
        if idx >= LEADERSHIP_MIN_INDEX:
            await member.add_roles(lead_role, reason="C.C.O. conducere")
        else:
            await member.remove_roles(lead_role, reason="C.C.O. conducere sync")


async def sync_nickname(member):
    row = fetchone("SELECT * FROM members WHERE discord_id=?", (str(member.id),))
    if not row:
        return
    if row["callsign"]:
        nick = f"[{row['callsign']}] {row['first_name']} {row['last_name']} | {row['internal_id']}"
    else:
        nick = f"{row['first_name']} {row['last_name']} | {row['internal_id']}"
    try:
        await member.edit(nick=nick[:32], reason="C.C.O. nickname sync")
    except discord.Forbidden:
        pass


async def set_member_identity(member, first_name, last_name, internal_id, rank_name, callsign=None, actor_id=None):
    if rank_index(rank_name) < 0:
        raise RuntimeError("Grad invalid.")

    row = fetchone("SELECT * FROM members WHERE discord_id=?", (str(member.id),))
    old_rank = row["rank"] if row else None

    execute("""
        INSERT INTO members(discord_id,first_name,last_name,internal_id,rank,status,updated_at)
        VALUES(?,?,?,?,?,'active',CURRENT_TIMESTAMP)
        ON CONFLICT(discord_id) DO UPDATE SET
            first_name=excluded.first_name,
            last_name=excluded.last_name,
            internal_id=excluded.internal_id,
            rank=excluded.rank,
            status='active',
            suspended_reason=NULL,
            inactivity_until=NULL,
            out_reason=NULL,
            updated_at=CURRENT_TIMESTAMP
    """, (str(member.id), first_name, last_name, internal_id, rank_name))

    # Pentru creare / schimbare de grad atribuim callsign conform noului grad.
    current_after = fetchone("SELECT callsign FROM members WHERE discord_id=?", (str(member.id),))
    must_reassign = not row or old_rank != rank_name or not current_after["callsign"]
    if must_reassign:
        cs = assign_callsign_db(member.id, rank_name, preferred=callsign)
    elif callsign and callsign.upper() != current_after["callsign"].upper():
        cs = assign_callsign_db(member.id, rank_name, preferred=callsign)
    else:
        cs = current_after["callsign"]

    await sync_rank_roles(member, rank_name)
    await sync_nickname(member)
    execute(
        "INSERT INTO member_rank_history(user_id,old_rank,new_rank,changed_by) VALUES(?,?,?,?)",
        (str(member.id), old_rank, rank_name, str(actor_id) if actor_id else str(member.id)),
    )
    audit("member_identity", actor_id or member.id, member.id, f"{rank_name}|{cs}")
    return fetchone("SELECT * FROM members WHERE discord_id=?", (str(member.id),))


async def change_member_rank(member, new_rank, actor_id=None, callsign=None):
    row = fetchone("SELECT * FROM members WHERE discord_id=?", (str(member.id),))
    if not row:
        raise RuntimeError("Membrul nu are fișă C.C.O.")
    old_rank = row["rank"]
    if old_rank == new_rank:
        return row

    # calculăm / validăm callsign înainte să schimbăm rolurile
    cs = assign_callsign_db(member.id, new_rank, preferred=callsign)
    execute("UPDATE members SET rank=?,updated_at=CURRENT_TIMESTAMP WHERE discord_id=?", (new_rank, str(member.id)))
    await sync_rank_roles(member, new_rank)
    await sync_nickname(member)
    execute(
        "INSERT INTO member_rank_history(user_id,old_rank,new_rank,changed_by) VALUES(?,?,?,?)",
        (str(member.id), old_rank, new_rank, str(actor_id) if actor_id else None),
    )
    audit("rank_change", actor_id, member.id, f"{old_rank} -> {new_rank} | {cs}")
    return fetchone("SELECT * FROM members WHERE discord_id=?", (str(member.id),))


async def promote_member(member, actor_id=None, callsign=None):
    row = fetchone("SELECT * FROM members WHERE discord_id=?", (str(member.id),))
    if not row:
        raise RuntimeError("Membrul nu are fișă C.C.O.")
    idx = rank_index(row["rank"])
    if idx >= len(RANKS)-1:
        raise RuntimeError("Membrul este deja la gradul maxim.")
    return await change_member_rank(member, RANKS[idx+1]["name"], actor_id=actor_id, callsign=callsign)


async def demote_member(member, actor_id=None, callsign=None):
    row = fetchone("SELECT * FROM members WHERE discord_id=?", (str(member.id),))
    if not row:
        raise RuntimeError("Membrul nu are fișă C.C.O.")
    idx = rank_index(row["rank"])
    if idx <= 0:
        raise RuntimeError("Membrul este deja la gradul minim.")
    return await change_member_rank(member, RANKS[idx-1]["name"], actor_id=actor_id, callsign=callsign)


async def suspend_member(member, reason):
    ids = [get_config("role_cco"), get_config("role_leadership")]
    ids += [get_config(f"rank_role_{r['key']}") for r in RANKS]
    ids = {int(x) for x in ids if x}
    roles = [r for r in member.roles if r.id in ids]
    if roles:
        await member.remove_roles(*roles, reason=f"C.C.O. suspendare: {reason}")
    execute(
        "UPDATE members SET status='suspended',suspended_reason=?,updated_at=CURRENT_TIMESTAMP WHERE discord_id=?",
        (reason, str(member.id))
    )


async def reactivate_member(member):
    row = fetchone("SELECT * FROM members WHERE discord_id=?", (str(member.id),))
    if not row:
        raise RuntimeError("Membrul nu există în baza de date.")
    execute(
        "UPDATE members SET status='active',suspended_reason=NULL,updated_at=CURRENT_TIMESTAMP WHERE discord_id=?",
        (str(member.id),)
    )
    await sync_rank_roles(member, row["rank"])
    await sync_nickname(member)


async def remove_cco_roles(member, reason="C.C.O. OUT"):
    ids = [
        get_config("role_cco"), get_config("role_leadership"), get_config("role_tester"),
        get_config("role_head_tester"), get_config("role_inactive"), get_config("role_av_1_1"),
        get_config("role_fw_1_3"), get_config("role_fw_2_3"), get_config("role_fw_3_3"),
    ]
    ids += [get_config(f"rank_role_{r['key']}") for r in RANKS]
    ids = {int(x) for x in ids if x}
    roles = [r for r in member.roles if r.id in ids]
    if roles:
        await member.remove_roles(*roles, reason=reason)


async def remove_member(member, reason, status="removed"):
    row = fetchone("SELECT callsign,rank FROM members WHERE discord_id=?", (str(member.id),))
    if row and row["rank"] and is_leadership_rank(row["rank"]):
        _release_leadership_assignment(member.id, row["callsign"])
    await remove_cco_roles(member, reason)
    execute(
        "UPDATE members SET status=?,callsign=NULL,out_reason=?,updated_at=CURRENT_TIMESTAMP WHERE discord_id=?",
        (status, reason, str(member.id)),
    )
    await member.kick(reason=reason)
