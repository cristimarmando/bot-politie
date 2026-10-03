import csv
import io
import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import discord
import requests
from flask import (
    Flask, abort, jsonify, make_response, redirect, render_template,
    request, send_file, session
)
from waitress import serve

from cco.config.ranks import RANKS, rank_index, LEADERSHIP_MIN_INDEX, SSI_INDEX, is_leadership_rank
from cco.db import (
    audit, execute, fetchall, fetchone, get_config, get_security,
    is_superuser, is_protected_user, panel_permission, set_config, set_panel_permission,
    set_protected_user, set_security
)
from cco.panel_bridge import get_bot, is_ready as bridge_ready, run_coroutine
from cco.services.member_service import (
    demote_member, promote_member, reactivate_member, remove_member, change_member_rank,
    suspend_member, sync_nickname, sync_rank_roles, set_member_identity,
    available_callsigns, assign_callsign_db
)
from cco.services.snapshot_service import make_snapshot
from cco.services.operational_service import (
    operational_counts, create_sanction, approve_inactivity, reject_inactivity,
    accept_resignation, reject_resignation, approve_action_request, reject_action_request
)
from cco.services.timesheet_service import adjust_time, reset_time, total_minutes
from cco.utils import can_manage_target, owner_id, has_leadership

MODULES = [
    "dashboard", "members", "timesheets", "operations", "callsigns", "tickets", "testers",
    "warnings", "sanctions", "inactivity", "resignations", "security", "backups", "logs", "config", "superusers"
]


def rowdict(row):
    return dict(row) if row is not None else None


def rowsdict(rows):
    return [dict(r) for r in rows]


def now_local():
    return datetime.now(ZoneInfo(os.getenv("TIMEZONE", "Europe/Bucharest")))


def period_bounds(period, date_from=None, date_to=None):
    tz = ZoneInfo(os.getenv("TIMEZONE", "Europe/Bucharest"))
    now = datetime.now(tz)
    if period == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
    elif period == "yesterday":
        end = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start = end - timedelta(days=1)
    elif period == "week":
        start = (now - timedelta(days=6)).replace(hour=0, minute=0, second=0, microsecond=0)
        end = now + timedelta(days=1)
        end = end.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "month":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if start.month == 12:
            end = start.replace(year=start.year + 1, month=1)
        else:
            end = start.replace(month=start.month + 1)
    elif period == "last_month":
        this_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end = this_month
        start = (this_month - timedelta(days=1)).replace(day=1)
    elif period == "custom" and date_from and date_to:
        start = datetime.fromisoformat(date_from).replace(tzinfo=tz)
        end = datetime.fromisoformat(date_to).replace(tzinfo=tz) + timedelta(days=1)
    else:
        return None, None
    return start.astimezone(timezone.utc).isoformat(), end.astimezone(timezone.utc).isoformat()


def make_app():
    app = Flask(__name__, template_folder="../templates", static_folder="../static")
    app.secret_key = os.getenv("DASHBOARD_SECRET", "change-me")
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        MAX_CONTENT_LENGTH=4 * 1024 * 1024,
    )

    base = os.getenv("DASHBOARD_BASE_URL", "http://localhost:3000").rstrip("/")
    redirect_uri = os.getenv("DISCORD_REDIRECT_URI", f"{base}/auth/discord/callback")
    owner = str(os.getenv("OWNER_ID", ""))

    def leadership_access(user_id):
        if not bridge_ready():
            return False
        async def work():
            bot = get_bot(); guild = bot.get_guild(guild_id()) if bot else None
            if not guild: return False
            member = guild.get_member(int(user_id))
            if not member:
                try: member = await guild.fetch_member(int(user_id))
                except Exception: return False
            return has_leadership(member)
        try:
            return bool(run_coroutine(work(), timeout=8))
        except Exception:
            return False

    def is_allowed(user_id):
        return str(user_id) == owner or is_superuser(user_id) or leadership_access(user_id)

    LEADERSHIP_MODULES = {"dashboard","members","timesheets","operations","callsigns","tickets","testers","warnings","sanctions","inactivity","resignations","logs"}

    def has_module_access(user_id, module):
        if str(user_id) == owner:
            return True
        if is_superuser(user_id):
            return panel_permission(user_id, module, owner)
        return module in LEADERSHIP_MODULES and leadership_access(user_id)

    def require_login():
        if "user" not in session:
            abort(401)

    def require_module(module):
        require_login()
        uid = session["user"]["id"]
        if not has_module_access(uid, module):
            abort(403)

    def require_owner():
        require_login()
        if str(session["user"]["id"]) != owner:
            abort(403)

    def require_superuser():
        require_login()
        uid = session["user"]["id"]
        if str(uid) != owner and not is_superuser(uid):
            abort(403)

    def csrf_token():
        token = session.get("csrf")
        if not token:
            token = secrets.token_urlsafe(32)
            session["csrf"] = token
        return token

    def verify_csrf():
        sent = request.headers.get("X-CSRF-Token") or request.form.get("csrf")
        if not sent or sent != session.get("csrf"):
            abort(400, "CSRF invalid")

    def json_ok(**kwargs):
        return jsonify({"ok": True, **kwargs})

    def json_error(message, status=400):
        return jsonify({"ok": False, "error": str(message)}), status

    def guild_id():
        return int(os.getenv("GUILD_ID", "0") or 0)

    async def guild_member_pair(actor_id, target_id):
        bot = get_bot()
        guild = bot.get_guild(guild_id()) if bot else None
        if not guild:
            raise RuntimeError("Serverul Discord nu este disponibil.")
        actor = guild.get_member(int(actor_id)) or await guild.fetch_member(int(actor_id))
        target = guild.get_member(int(target_id)) or await guild.fetch_member(int(target_id))
        return guild, actor, target

    def require_critical_confirmation(data):
        if str((data or {}).get("confirm") or "").strip().upper() != "CONFIRM":
            raise RuntimeError("CONFIRM_REQUIRED")

    @app.before_request
    def panel_session_security():
        # OAuth/login/public assets rămân accesibile.
        if request.path.startswith("/static/") or request.path.startswith("/auth/discord") or request.path == "/health":
            return None
        if "user" not in session:
            return None
        now_ts = int(datetime.now(timezone.utc).timestamp())
        timeout_min = int(get_security("panel_session_minutes", "30") or 30)
        last = int(session.get("last_active", now_ts))
        if now_ts - last > timeout_min * 60:
            uid = session.get("user", {}).get("id")
            audit("panel_session_expired", uid, uid, f"timeout={timeout_min}m")
            session.clear()
            return redirect("/auth/discord")
        uid = session["user"]["id"]
        if not is_allowed(uid):
            audit("panel_session_revoked", uid, uid, "lost access")
            session.clear()
            return redirect("/auth/discord")
        session["last_active"] = now_ts

    @app.context_processor
    def inject_common():
        return {"csrf_token": csrf_token() if "user" in session else ""}

    @app.get("/auth/discord")
    def auth_discord():
        state = secrets.token_urlsafe(24)
        session["oauth_state"] = state
        from urllib.parse import urlencode
        q = urlencode({
            "client_id": os.getenv("CLIENT_ID"),
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": "identify",
            "state": state,
        })
        return redirect(f"https://discord.com/oauth2/authorize?{q}")

    @app.get("/auth/discord/callback")
    def callback():
        if request.args.get("state") != session.get("oauth_state"):
            abort(400, "OAuth state invalid")
        code = request.args.get("code")
        if not code:
            abort(400, "Lipsește codul OAuth2")

        token_res = requests.post(
            "https://discord.com/api/oauth2/token",
            data={
                "client_id": os.getenv("CLIENT_ID"),
                "client_secret": os.getenv("DISCORD_CLIENT_SECRET"),
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=10,
        )
        if not token_res.ok:
            return f"OAuth token error: {token_res.status_code} {token_res.text}", 500
        token = token_res.json()

        me_res = requests.get(
            "https://discord.com/api/users/@me",
            headers={"Authorization": f"Bearer {token['access_token']}"},
            timeout=10,
        )
        if not me_res.ok:
            return f"OAuth user error: {me_res.status_code} {me_res.text}", 500
        me = me_res.json()
        if not is_allowed(me["id"]):
            abort(403)

        session.clear()
        session["user"] = {
            "id": me["id"],
            "username": me["username"],
            "avatar": me.get("avatar"),
            "is_owner": str(me["id"]) == owner,
            "is_superuser": is_superuser(me["id"]),
            "is_leadership": leadership_access(me["id"]),
        }
        session["csrf"] = secrets.token_urlsafe(32)
        session["last_active"] = int(datetime.now(timezone.utc).timestamp())
        execute(
            "INSERT INTO panel_login_events(user_id,username,ip) VALUES(?,?,?)",
            (str(me["id"]), me["username"], request.headers.get("X-Forwarded-For", request.remote_addr)),
        )
        audit("panel_login", me["id"], me["id"], request.remote_addr or "")
        return redirect("/")

    @app.get("/logout")
    def logout():
        session.clear()
        return redirect("/")

    @app.get("/")
    def home():
        if "user" not in session:
            return redirect("/auth/discord")
        perms = {m: has_module_access(session["user"]["id"], m) for m in MODULES}
        return render_template(
            "dashboard.html",
            user=session["user"],
            permissions=perms,
            ranks=RANKS,
            csrf_token=csrf_token(),
        )

    # ---------- OVERVIEW ----------
    @app.get("/api/overview")
    def api_overview():
        require_module("dashboard")
        today_start, today_end = period_bounds("today")
        week_start, week_end = period_bounds("week")

        stats = {
            "members": fetchone("SELECT COUNT(*) c FROM members WHERE status='active'")["c"],
            "leadership": sum(1 for r in fetchall("SELECT rank FROM members WHERE status='active'") if rank_index(r["rank"]) >= 6),
            "testers": fetchone("SELECT COUNT(*) c FROM members WHERE status='active' AND (tester=1 OR head_tester=1)")["c"],
            "tickets": fetchone("SELECT COUNT(*) c FROM tickets WHERE status='open'")["c"],
            "active_timesheets": fetchone("SELECT COUNT(*) c FROM timesheets WHERE status='active'")["c"],
            "security_today": fetchone("SELECT COUNT(*) c FROM audit_events WHERE type LIKE 'security_%' AND created_at >= date('now')")["c"],
        }
        stats["minutes_today"] = fetchone(
            "SELECT COALESCE(SUM(duration_minutes),0) m FROM timesheets WHERE status='closed' AND clock_in>=? AND clock_in<?",
            (today_start, today_end),
        )["m"] or 0
        stats["minutes_week"] = fetchone(
            "SELECT COALESCE(SUM(duration_minutes),0) m FROM timesheets WHERE status='closed' AND clock_in>=? AND clock_in<?",
            (week_start, week_end),
        )["m"] or 0

        # Grafic 7 zile
        labels, values = [], []
        local = now_local().replace(hour=0, minute=0, second=0, microsecond=0)
        for i in range(6, -1, -1):
            day = local - timedelta(days=i)
            nxt = day + timedelta(days=1)
            s, e = day.astimezone(timezone.utc).isoformat(), nxt.astimezone(timezone.utc).isoformat()
            minutes = fetchone(
                "SELECT COALESCE(SUM(duration_minutes),0) m FROM timesheets WHERE status='closed' AND clock_in>=? AND clock_in<?",
                (s, e),
            )["m"] or 0
            labels.append(day.strftime("%d.%m"))
            values.append(round(minutes / 60, 2))

        recent = rowsdict(fetchall("SELECT * FROM audit_events ORDER BY id DESC LIMIT 12"))
        return jsonify({"stats": stats, "chart": {"labels": labels, "values": values}, "recent": recent})

    # ---------- MEMBERS ----------
    @app.get("/api/members")
    def api_members():
        require_module("members")
        q = request.args.get("q", "").strip()
        status = request.args.get("status", "all")
        rank = request.args.get("rank", "all")
        tester = request.args.get("tester", "all")
        clauses, params = [], []
        if q:
            clauses.append("(discord_id LIKE ? OR first_name LIKE ? OR last_name LIKE ? OR internal_id LIKE ? OR callsign LIKE ?)")
            like = f"%{q}%"
            params += [like, like, like, like, like]
        if status != "all":
            clauses.append("status=?"); params.append(status)
        if rank != "all":
            clauses.append("rank=?"); params.append(rank)
        if tester == "yes":
            clauses.append("(tester=1 OR head_tester=1)")
        if tester == "no":
            clauses.append("tester=0 AND head_tester=0")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = rowsdict(fetchall(f"SELECT * FROM members{where} ORDER BY status,callsign,first_name LIMIT 500", params))
        return jsonify(rows)

    @app.get("/api/member/<user_id>")
    def api_member(user_id):
        require_module("members")
        period = request.args.get("period", "week")
        start, end = period_bounds(period, request.args.get("from"), request.args.get("to"))
        member = rowdict(fetchone("SELECT * FROM members WHERE discord_id=?", (user_id,)))
        if not member:
            return json_error("Membru inexistent", 404)
        params = [user_id]
        clause = "user_id=?"
        if start and end:
            clause += " AND clock_in>=? AND clock_in<?"; params += [start, end]
        times = rowsdict(fetchall(f"SELECT * FROM timesheets WHERE {clause} ORDER BY id DESC LIMIT 250", params))
        warns = rowsdict(fetchall("SELECT * FROM warnings WHERE user_id=? ORDER BY id DESC", (user_id,)))
        history = rowsdict(fetchall("SELECT * FROM audit_events WHERE target_id=? ORDER BY id DESC LIMIT 50", (user_id,)))
        tickets = rowsdict(fetchall("SELECT * FROM tickets WHERE creator_id=? OR claimed_by=? ORDER BY id DESC LIMIT 50", (user_id, user_id)))
        closed = [t for t in times if t["status"] == "closed"]
        period_minutes = sum(int(t.get("duration_minutes") or 0) for t in closed)
        stats = {
            "period_minutes": period_minutes,
            "period_count": len(closed),
            "average_minutes": round(period_minutes / len(closed)) if closed else 0,
            "longest_minutes": max([int(t.get("duration_minutes") or 0) for t in closed], default=0),
            "total_minutes": total_minutes(user_id),
            "cancelled": sum(1 for t in times if t["status"] == "cancelled"),
        }
        op_counts = operational_counts(user_id)
        operations = rowsdict(fetchall("""
            SELECT * FROM operational_records
            WHERE author_id=? OR participants_json LIKE ?
            ORDER BY id DESC LIMIT 200
        """, (user_id, f'%"{user_id}"%')))
        sanctions = rowsdict(fetchall("SELECT * FROM sanctions WHERE user_id=? ORDER BY id DESC LIMIT 100", (user_id,)))
        inactivity = rowsdict(fetchall("SELECT * FROM inactivity_requests WHERE user_id=? ORDER BY id DESC LIMIT 50", (user_id,)))
        resignations = rowsdict(fetchall("SELECT * FROM resignations WHERE user_id=? ORDER BY id DESC LIMIT 50", (user_id,)))
        stats.update({
            "buletine": op_counts.get("buletin", 0),
            "filaje": op_counts.get("filaj", 0),
            "interogatorii": op_counts.get("interogatoriu", 0),
            "actiuni": op_counts.get("actiune", 0),
        })
        return jsonify({
            "member": member, "timesheets": times, "warnings": warns, "history": history,
            "tickets": tickets, "stats": stats, "operations": operations,
            "sanctions": sanctions, "inactivity": inactivity, "resignations": resignations,
        })

    @app.post("/api/member/<user_id>/action")
    def api_member_action(user_id):
        require_module("members")
        verify_csrf()
        data = request.get_json(silent=True) or {}
        action = data.get("action")
        actor_id = session["user"]["id"]

        async def work():
            guild, actor, target = await guild_member_pair(actor_id, user_id)
            desired = data.get("rank") if action == "set_rank" else None
            is_self_admin = actor.id == target.id and (actor.id == owner_id() or is_superuser(actor.id))
            if action in {"promote", "demote", "set_rank"}:
                row = fetchone("SELECT rank FROM members WHERE discord_id=?", (user_id,))
                if not row: raise RuntimeError("Membrul nu este în baza de date.")
                if action == "promote":
                    idx = rank_index(row["rank"])
                    if idx+1 >= len(RANKS): raise RuntimeError("Membrul este deja la gradul maxim.")
                    desired_rank = RANKS[idx+1]["name"]
                elif action == "demote":
                    idx = rank_index(row["rank"])
                    if idx <= 0: raise RuntimeError("Membrul este deja la gradul minim.")
                    desired_rank = RANKS[idx-1]["name"]
                else:
                    desired_rank = desired
                    if not any(r["name"] == desired_rank for r in RANKS): raise RuntimeError("Grad invalid")
                ok, reason = can_manage_target(actor, target, desired_rank)
                if not ok: raise RuntimeError(reason)
                selected = data.get("callsign") or None
                available = available_callsigns(desired_rank, user_id)
                current = fetchone("SELECT rank,callsign FROM members WHERE discord_id=?", (user_id,))
                if current and current["rank"] == desired_rank and current["callsign"]:
                    selected = current["callsign"]
                elif not selected:
                    if not available: raise RuntimeError(f"Nu există callsign-uri libere pentru {desired_rank}.")
                    if is_leadership_rank(desired_rank) and len(available) > 1:
                        raise RuntimeError("SELECT_CALLSIGN:" + "|".join(available))
                    selected = available[0]
                result = await change_member_rank(target, desired_rank, actor_id=actor_id, callsign=selected)
                if is_self_admin:
                    audit("self_elevation", actor_id, user_id, f"panel:{current['rank'] if current else None}->{desired_rank}|{result['callsign']}")
                audit(f"panel_{action}", actor_id, user_id, f"{desired_rank}|{result['callsign']}")
                return {"rank": desired_rank, "callsign": result["callsign"]}
            if action == "suspend":
                ok, reason = can_manage_target(actor, target)
                if not ok: raise RuntimeError(reason)
                reason_text = data.get("reason", "Suspendare din panel")
                await suspend_member(target, reason_text)
                audit("panel_suspend", actor_id, user_id, reason_text)
                return "suspended"
            if action == "reactivate":
                ok, reason = can_manage_target(actor, target)
                if not ok: raise RuntimeError(reason)
                await reactivate_member(target)
                audit("panel_reactivate", actor_id, user_id, "")
                return "active"
            if action == "remove":
                ok, reason = can_manage_target(actor, target)
                if not ok: raise RuntimeError(reason)
                reason_text = data.get("reason", "Eliminat din panel")
                audit("panel_remove", actor_id, user_id, reason_text)
                await remove_member(target, reason_text)
                return "removed"
            if action in {"add_tester", "remove_tester"}:
                role_id = get_config("role_tester")
                role = guild.get_role(int(role_id)) if role_id else None
                if not role: raise RuntimeError("Rolul Tester C.C.O. nu este configurat.")
                if action == "add_tester":
                    await target.add_roles(role, reason=f"Panel by {actor}")
                    execute("UPDATE members SET tester=1 WHERE discord_id=?", (user_id,))
                else:
                    await target.remove_roles(role, reason=f"Panel by {actor}")
                    execute("UPDATE members SET tester=0 WHERE discord_id=?", (user_id,))
                audit(f"panel_{action}", actor_id, user_id, "")
                return action
            if action == "force_clockout":
                from cco.services.timesheet_service import clock_out
                ts = clock_out(user_id)
                audit("panel_force_clockout", actor_id, user_id, str(ts["id"]))
                return ts["duration_minutes"]
            raise RuntimeError("Acțiune Discord necunoscută")

        try:
            if action == "remove":
                require_critical_confirmation(data)
            if action in {"promote", "demote", "set_rank"} and str(user_id) == str(actor_id):
                require_critical_confirmation(data)
            if action in {"promote", "demote", "set_rank", "suspend", "reactivate", "remove", "add_tester", "remove_tester", "force_clockout"}:
                result = run_coroutine(work())
                return json_ok(result=result)
            if action == "warn":
                reason = (data.get("reason") or "").strip()
                if not reason: return json_error("Motivul este obligatoriu")
                wid = execute("INSERT INTO warnings(user_id,reason,created_by) VALUES(?,?,?)", (user_id, reason, actor_id))
                audit("panel_warn", actor_id, user_id, reason)
                return json_ok(id=wid)
            if action == "time_add":
                mins = abs(int(data.get("minutes", 0)))
                adjust_time(user_id, mins, actor_id, "Panel: adăugare")
                audit("panel_time_add", actor_id, user_id, str(mins))
                return json_ok()
            if action == "time_sub":
                mins = abs(int(data.get("minutes", 0)))
                adjust_time(user_id, -mins, actor_id, "Panel: reducere")
                audit("panel_time_sub", actor_id, user_id, str(mins))
                return json_ok()
            if action == "time_reset":
                reset_time(user_id, actor_id)
                audit("panel_time_reset", actor_id, user_id, "")
                return json_ok()
            if action == "edit_identity":
                first = data.get("first_name", "").strip()
                last = data.get("last_name", "").strip()
                internal_id = data.get("internal_id", "").strip()
                if not all([first,last,internal_id]): return json_error("Nume, prenume și ID sunt obligatorii")
                execute("UPDATE members SET first_name=?,last_name=?,internal_id=?,updated_at=CURRENT_TIMESTAMP WHERE discord_id=?", (first,last,internal_id,user_id))
                async def nick():
                    bot = get_bot(); guild = bot.get_guild(guild_id())
                    target = guild.get_member(int(user_id)) if guild else None
                    if target: await sync_nickname(target)
                if bridge_ready(): run_coroutine(nick())
                audit("panel_edit_identity", actor_id, user_id, f"{first} {last} | {internal_id}")
                return json_ok()
            return json_error("Acțiune necunoscută")
        except Exception as e:
            return json_error(e, 400)

    # ---------- V4 MEMBER CREATE / CALLSIGN AVAILABILITY ----------
    @app.get("/api/callsigns/available")
    def api_callsigns_available():
        require_module("callsigns")
        rank = request.args.get("rank", "")
        if not any(r["name"] == rank for r in RANKS):
            return json_error("Grad invalid")
        return jsonify({"rank": rank, "leadership": is_leadership_rank(rank), "callsigns": available_callsigns(rank)})

    @app.post("/api/members/add")
    def api_member_add():
        require_module("members"); verify_csrf()
        data = request.get_json(silent=True) or {}
        user_id = str(data.get("user_id") or "").strip()
        first = str(data.get("first_name") or "").strip()
        last = str(data.get("last_name") or "").strip()
        internal_id = str(data.get("internal_id") or "").strip()
        rank = str(data.get("rank") or "").strip()
        selected = str(data.get("callsign") or "").strip() or None
        if not all([user_id, first, last, internal_id, rank]): return json_error("Completează toate câmpurile.")
        if not any(r["name"] == rank for r in RANKS): return json_error("Grad invalid")
        if fetchone("SELECT 1 FROM members WHERE discord_id=? AND status NOT IN ('removed','out','demisionat')", (user_id,)):
            return json_error("Membrul există deja în baza C.C.O.")
        actor_id = session["user"]["id"]

        async def work():
            guild, actor, target = await guild_member_pair(actor_id, user_id)
            ok, reason = can_manage_target(actor, target, rank)
            if not ok: raise RuntimeError(reason)
            options = available_callsigns(rank, user_id)
            chosen = selected
            if not chosen:
                if not options: raise RuntimeError(f"Nu există callsign-uri libere pentru {rank}.")
                if is_leadership_rank(rank) and len(options) > 1:
                    raise RuntimeError("SELECT_CALLSIGN:" + "|".join(options))
                chosen = options[0]
            row = await set_member_identity(target, first, last, internal_id, rank, callsign=chosen, actor_id=actor_id)
            return {"user_id": user_id, "callsign": row["callsign"], "rank": row["rank"]}
        try:
            return json_ok(result=run_coroutine(work()))
        except Exception as e:
            return json_error(e)

    # ---------- V5 LEADERSHIP CALLSIGNS PER GRAD (SUPERUSER ONLY) ----------
    @app.get("/api/leadership-callsigns")
    def api_leadership_callsigns():
        require_module("callsigns")
        rows = rowsdict(fetchall("""
            SELECT lc.*,m.first_name,m.last_name,m.rank AS member_rank
            FROM leadership_callsigns lc
            LEFT JOIN members m ON m.discord_id=lc.assigned_to
            ORDER BY CASE lc.rank
                WHEN 'Coordonator Operațional' THEN 1
                WHEN 'Comandant Operațional' THEN 2
                WHEN 'Director Adjunct C.C.O.' THEN 3
                WHEN 'Director General C.C.O.' THEN 4
                ELSE 99 END, lc.callsign COLLATE NOCASE
        """))
        return jsonify(rows)

    @app.post("/api/leadership-callsigns")
    def api_leadership_callsigns_add():
        require_superuser(); verify_csrf()
        data = request.get_json(silent=True) or {}
        cs = str(data.get("callsign") or "").strip().upper()
        rank = str(data.get("rank") or "").strip()
        if not cs: return json_error("Callsign lipsă")
        if not is_leadership_rank(rank): return json_error("Alege un grad de Conducere valid")
        if len(cs) > 32: return json_error("Callsign prea lung")
        if fetchone("SELECT 1 FROM leadership_callsigns WHERE callsign=? COLLATE NOCASE", (cs,)):
            return json_error("Callsign existent")
        execute("INSERT INTO leadership_callsigns(callsign,rank,note,created_by) VALUES(?,?,?,?)",
                (cs, rank, data.get("note", ""), session["user"]["id"]))
        audit("panel_lead_callsign_add", session["user"]["id"], None, f"{rank}|{cs}")
        return json_ok()

    @app.post("/api/leadership-callsigns/<int:slot_id>/edit")
    def api_leadership_callsigns_edit(slot_id):
        require_superuser(); verify_csrf()
        data = request.get_json(silent=True) or {}
        cs = str(data.get("callsign") or "").strip().upper()
        rank = str(data.get("rank") or "").strip()
        if not cs: return json_error("Callsign lipsă")
        if not is_leadership_rank(rank): return json_error("Alege un grad de Conducere valid")
        row = fetchone("SELECT * FROM leadership_callsigns WHERE id=?", (slot_id,))
        if not row: return json_error("Callsign inexistent", 404)
        if row["assigned_to"] and row["rank"] != rank:
            return json_error("Nu poți muta pe alt grad un callsign atribuit. Eliberează-l mai întâi.")
        conflict = fetchone("SELECT 1 FROM leadership_callsigns WHERE callsign=? COLLATE NOCASE AND id<>?", (cs, slot_id))
        if conflict: return json_error("Există deja acest callsign")
        old = row["callsign"]
        execute("UPDATE leadership_callsigns SET callsign=?,rank=?,note=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (cs, rank, data.get("note", row["note"] or ""), slot_id))
        if row["assigned_to"]:
            execute("UPDATE members SET callsign=?,updated_at=CURRENT_TIMESTAMP WHERE discord_id=?", (cs, row["assigned_to"]))
            if bridge_ready():
                async def nick():
                    bot = get_bot(); guild = bot.get_guild(guild_id()); m = guild.get_member(int(row["assigned_to"])) if guild else None
                    if m: await sync_nickname(m)
                run_coroutine(nick())
        audit("panel_lead_callsign_edit", session["user"]["id"], row["assigned_to"], f"{old}->{cs}|{rank}")
        return json_ok()

    @app.post("/api/leadership-callsigns/<int:slot_id>/delete")
    def api_leadership_callsigns_delete(slot_id):
        require_superuser(); verify_csrf()
        data = request.get_json(silent=True) or {}
        require_critical_confirmation(data)
        row = fetchone("SELECT * FROM leadership_callsigns WHERE id=?", (slot_id,))
        if not row: return json_error("Callsign inexistent", 404)
        if row["assigned_to"]: return json_error("Nu poți șterge un callsign care este atribuit.")
        execute("DELETE FROM leadership_callsigns WHERE id=?", (slot_id,))
        audit("panel_lead_callsign_delete", session["user"]["id"], None, f"{row['rank']}|{row['callsign']}")
        return json_ok()

    # ---------- V4 ACTIVITATE OPERAȚIONALĂ ----------
    @app.get("/api/operations")
    def api_operations():
        require_module("operations")
        kind=request.args.get("kind","all"); status=request.args.get("status","all"); uid=request.args.get("user_id","").strip()
        clauses=[]; params=[]
        if kind!="all": clauses.append("kind=?"); params.append(kind)
        if status!="all": clauses.append("status=?"); params.append(status)
        if uid:
            clauses.append("(author_id=? OR participants_json LIKE ?)"); params += [uid, f'%"{uid}"%']
        where=" WHERE "+" AND ".join(clauses) if clauses else ""
        rows=rowsdict(fetchall(f"SELECT * FROM operational_records{where} ORDER BY id DESC LIMIT 1000", params))
        return jsonify(rows)

    @app.post("/api/operations/bulk-review")
    def api_operations_bulk_review():
        require_module("operations"); verify_csrf()
        data=request.get_json(silent=True) or {}; ids=data.get("ids") or []; action=data.get("action"); reason=str(data.get("reason") or "")
        if action not in {"accept","reject"}: return json_error("Acțiune invalidă")
        if not ids: return json_error("Nu ai selectat înregistrări")
        status="accepted" if action=="accept" else "rejected"
        actor=session["user"]["id"]
        changed=[]
        for rid in ids[:200]:
            row=fetchone("SELECT * FROM operational_records WHERE id=?", (int(rid),))
            if not row or row["status"]!="pending": continue
            execute("UPDATE operational_records SET status=?,reviewer_id=?,review_reason=?,reviewed_at=CURRENT_TIMESTAMP WHERE id=?",
                    (status,actor,reason,int(rid)))
            audit(f"panel_operational_{action}",actor,row["author_id"],f"{row['kind']}#{rid}")
            changed.append(int(rid))
        return json_ok(changed=changed)

    @app.get("/api/action-requests")
    def api_action_requests():
        require_module("operations")
        return jsonify(rowsdict(fetchall("SELECT * FROM action_requests ORDER BY id DESC LIMIT 500")))

    @app.post("/api/action-requests/<int:req_id>/review")
    def api_action_request_review(req_id):
        require_module("operations"); verify_csrf()
        data=request.get_json(silent=True) or {}; action=data.get("action"); reason=data.get("reason","")
        try:
            if action=="accept": approve_action_request(req_id,session["user"]["id"])
            elif action=="reject": reject_action_request(req_id,session["user"]["id"],reason)
            else: return json_error("Acțiune invalidă")
            return json_ok()
        except Exception as e: return json_error(e)

    # ---------- V4 INACTIVITY / RESIGNATIONS / SANCTIONS ----------
    @app.get("/api/inactivity")
    def api_inactivity():
        require_module("inactivity")
        return jsonify(rowsdict(fetchall("""
            SELECT i.*,m.first_name,m.last_name,m.callsign,m.rank FROM inactivity_requests i
            LEFT JOIN members m ON m.discord_id=i.user_id ORDER BY i.id DESC LIMIT 500
        """)))

    @app.post("/api/inactivity/<int:req_id>/review")
    def api_inactivity_review(req_id):
        require_module("inactivity"); verify_csrf()
        data=request.get_json(silent=True) or {}; action=data.get("action"); reason=data.get("reason",""); actor=session["user"]["id"]
        async def work():
            bot=get_bot(); guild=bot.get_guild(guild_id())
            if not guild: raise RuntimeError("Guild indisponibil")
            if action=="accept":
                end=await approve_inactivity(guild,req_id,actor); return end.isoformat()
            if action=="reject":
                await reject_inactivity(req_id,actor,reason); return "rejected"
            raise RuntimeError("Acțiune invalidă")
        try: return json_ok(result=run_coroutine(work()))
        except Exception as e: return json_error(e)

    @app.get("/api/resignations")
    def api_resignations():
        require_module("resignations")
        return jsonify(rowsdict(fetchall("""
            SELECT r.*,m.first_name,m.last_name,m.callsign,m.rank FROM resignations r
            LEFT JOIN members m ON m.discord_id=r.user_id ORDER BY r.id DESC LIMIT 500
        """)))

    @app.post("/api/resignations/<int:req_id>/review")
    def api_resignation_review(req_id):
        require_module("resignations"); verify_csrf()
        data=request.get_json(silent=True) or {}; action=data.get("action"); reason=data.get("reason",""); actor_id=session["user"]["id"]
        async def work():
            bot=get_bot(); guild=bot.get_guild(guild_id())
            if not guild: raise RuntimeError("Guild indisponibil")
            reviewer=guild.get_member(int(actor_id)) or await guild.fetch_member(int(actor_id))
            if action=="accept":
                await accept_resignation(guild,req_id,reviewer,owner=str(actor_id)==owner,superuser=is_superuser(actor_id)); return "accepted"
            if action=="reject":
                await reject_resignation(req_id,actor_id,reason); return "rejected"
            raise RuntimeError("Acțiune invalidă")
        try: return json_ok(result=run_coroutine(work()))
        except Exception as e: return json_error(e)

    @app.get("/api/sanctions")
    def api_sanctions():
        require_module("sanctions")
        return jsonify(rowsdict(fetchall("""
            SELECT s.*,m.first_name,m.last_name,m.callsign,m.rank FROM sanctions s
            LEFT JOIN members m ON m.discord_id=s.user_id ORDER BY s.id DESC LIMIT 1000
        """)))


    # ---------- TIMESHEETS ----------
    @app.get("/api/timesheets")
    def api_timesheets():
        require_module("timesheets")
        period = request.args.get("period", "week")
        user_id = request.args.get("user_id", "").strip()
        status = request.args.get("status", "all")
        start, end = period_bounds(period, request.args.get("from"), request.args.get("to"))
        clauses, params = [], []
        if start and end:
            clauses.append("t.clock_in>=? AND t.clock_in<?"); params += [start,end]
        if user_id:
            clauses.append("t.user_id=?"); params.append(user_id)
        if status != "all":
            clauses.append("t.status=?"); params.append(status)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = rowsdict(fetchall(f"""
            SELECT t.*,m.first_name,m.last_name,m.callsign,m.rank
            FROM timesheets t LEFT JOIN members m ON m.discord_id=t.user_id
            {where} ORDER BY t.id DESC LIMIT 1000
        """, params))
        total = sum(int(r.get("duration_minutes") or 0) for r in rows if r["status"] == "closed")
        return jsonify({"rows": rows, "total_minutes": total})

    @app.get("/export/timesheets.csv")
    def export_timesheets():
        require_module("timesheets")
        rows = fetchall("""
            SELECT t.*,m.first_name,m.last_name,m.callsign,m.rank
            FROM timesheets t LEFT JOIN members m ON m.discord_id=t.user_id
            ORDER BY t.id DESC
        """)
        sio = io.StringIO()
        w = csv.writer(sio)
        w.writerow(["Callsign","Nume","Grad","Clock In","Clock Out","Minute","Status","Anulat de"])
        for r in rows:
            w.writerow([r["callsign"], f"{r['first_name'] or ''} {r['last_name'] or ''}".strip(), r["rank"], r["clock_in"], r["clock_out"], r["duration_minutes"], r["status"], r["cancelled_by"]])
        resp = make_response(sio.getvalue())
        resp.headers["Content-Type"] = "text/csv; charset=utf-8"
        resp.headers["Content-Disposition"] = "attachment; filename=pontaje-cco.csv"
        return resp

    # ---------- CALLSIGNS ----------
    @app.get("/api/callsigns")
    def api_callsigns():
        require_module("callsigns")
        ranges = rowsdict(fetchall("SELECT * FROM callsign_ranges ORDER BY start_num"))
        members = {r["callsign"]: dict(r) for r in fetchall("SELECT discord_id,first_name,last_name,rank,callsign FROM members WHERE callsign IS NOT NULL")}
        reservations = {r["callsign"]: dict(r) for r in fetchall("SELECT * FROM callsign_reservations")}
        for rng in ranges:
            slots = []
            for n in range(rng["start_num"], rng["end_num"] + 1):
                cs = f"{rng['prefix']}-{n:03d}"
                state = "free"
                payload = None
                if cs in members:
                    state = "used"; payload = members[cs]
                elif cs in reservations:
                    state = "reserved"; payload = reservations[cs]
                slots.append({"callsign": cs, "state": state, "data": payload})
            rng["slots"] = slots
            rng["used"] = sum(1 for s in slots if s["state"] == "used")
            rng["reserved"] = sum(1 for s in slots if s["state"] == "reserved")
            rng["free"] = sum(1 for s in slots if s["state"] == "free")
        return jsonify(ranges)

    @app.post("/api/callsigns/range")
    def api_callsign_range():
        require_module("callsigns")
        verify_csrf()
        data = request.get_json(silent=True) or {}
        rank = data.get("rank")
        prefix = (data.get("prefix") or "").upper().strip()
        start = int(data.get("start", 0)); stop = int(data.get("stop", 0))
        if not rank or not prefix or start < 1 or stop < start:
            return json_error("Date interval invalide")
        execute("""
            INSERT INTO callsign_ranges(rank,prefix,start_num,end_num) VALUES(?,?,?,?)
            ON CONFLICT(rank) DO UPDATE SET prefix=excluded.prefix,start_num=excluded.start_num,end_num=excluded.end_num
        """, (rank,prefix,start,stop))
        audit("panel_callsign_range", session["user"]["id"], None, f"{rank}:{prefix}-{start:03d}-{stop:03d}")
        return json_ok()

    @app.post("/api/callsigns/assign")
    def api_callsign_assign():
        require_module("callsigns"); verify_csrf()
        data=request.get_json(silent=True) or {}; user_id=str(data.get("user_id") or ""); cs=str(data.get("callsign") or "").strip().upper()
        row=fetchone("SELECT rank FROM members WHERE discord_id=?", (user_id,))
        if not row: return json_error("Membrul nu există în baza C.C.O.")
        try:
            assigned=assign_callsign_db(user_id,row["rank"],preferred=cs)
            async def nick():
                bot=get_bot(); guild=bot.get_guild(guild_id()); target=guild.get_member(int(user_id)) if guild else None
                if target: await sync_nickname(target)
            if bridge_ready(): run_coroutine(nick())
            audit("panel_callsign_assign",session["user"]["id"],user_id,assigned)
            return json_ok(callsign=assigned)
        except Exception as e: return json_error(e)

    @app.post("/api/callsigns/release")
    def api_callsign_release():
        require_module("callsigns")
        verify_csrf()
        data=request.get_json(silent=True) or {}; cs=(data.get("callsign") or "").upper().strip()
        row=fetchone("SELECT discord_id,first_name,last_name,internal_id FROM members WHERE callsign=?", (cs,))
        execute("UPDATE members SET callsign=NULL,updated_at=CURRENT_TIMESTAMP WHERE callsign=?", (cs,))
        execute("UPDATE leadership_callsigns SET assigned_to=NULL,updated_at=CURRENT_TIMESTAMP WHERE callsign=?", (cs,))
        if row and bridge_ready():
            async def clear_nick():
                bot=get_bot(); guild=bot.get_guild(guild_id())
                target=guild.get_member(int(row["discord_id"])) if guild else None
                if target:
                    try:
                        await target.edit(nick=f"{row['first_name']} {row['last_name']} | {row['internal_id']}", reason="Callsign eliberat din panel")
                    except Exception:
                        pass
            run_coroutine(clear_nick())
        audit("panel_callsign_release", session["user"]["id"], row["discord_id"] if row else None, cs)
        return json_ok()

    @app.post("/api/callsigns/reserve")
    def api_callsign_reserve():
        require_module("callsigns")
        verify_csrf()
        data=request.get_json(silent=True) or {}; cs=(data.get("callsign") or "").upper().strip()
        if not cs: return json_error("Callsign lipsă")
        if fetchone("SELECT 1 FROM members WHERE callsign=?", (cs,)): return json_error("Callsign ocupat")
        execute("""
            INSERT INTO callsign_reservations(callsign,rank,reserved_for,note,created_by)
            VALUES(?,?,?,?,?)
            ON CONFLICT(callsign) DO UPDATE SET reserved_for=excluded.reserved_for,note=excluded.note,created_by=excluded.created_by
        """, (cs,data.get("rank"),str(data.get("reserved_for") or ""),data.get("note",""),session["user"]["id"]))
        return json_ok()

    # ---------- TICKETS / WARNINGS / TESTERS ----------
    @app.get("/api/tickets")
    def api_tickets():
        require_module("tickets")
        status=request.args.get("status","all")
        rows = fetchall("SELECT * FROM tickets" + (" WHERE status=?" if status!="all" else "") + " ORDER BY id DESC LIMIT 500", ((status,) if status!="all" else ()))
        return jsonify(rowsdict(rows))

    @app.get("/ticket/<int:ticket_id>/transcript")
    def ticket_transcript(ticket_id):
        require_module("tickets")
        row=fetchone("SELECT channel_id FROM tickets WHERE id=?", (ticket_id,))
        if not row: abort(404)
        p=Path("backups/tickets")/f"{row['channel_id']}.txt"
        if not p.exists(): abort(404)
        return send_file(p, as_attachment=True, download_name=f"ticket-{ticket_id}.txt")

    @app.get("/api/warnings")
    def api_warnings():
        require_module("warnings")
        return jsonify(rowsdict(fetchall("""
            SELECT w.*,m.first_name,m.last_name,m.callsign
            FROM warnings w LEFT JOIN members m ON m.discord_id=w.user_id
            ORDER BY w.id DESC LIMIT 500
        """)))

    @app.post("/api/warnings/<int:warn_id>/delete")
    def api_warning_delete(warn_id):
        require_module("warnings"); verify_csrf()
        execute("DELETE FROM warnings WHERE id=?", (warn_id,))
        audit("panel_warn_delete", session["user"]["id"], None, str(warn_id))
        return json_ok()

    @app.get("/api/testers")
    def api_testers():
        require_module("testers")
        return jsonify(rowsdict(fetchall("SELECT * FROM members WHERE tester=1 OR head_tester=1 ORDER BY head_tester DESC,callsign")))

    # ---------- SECURITY ----------
    @app.get("/api/security")
    def api_security():
        require_module("security")
        settings = {r["key"]: r["value"] for r in fetchall("SELECT key,value FROM security_settings ORDER BY key")}
        whitelist = rowsdict(fetchall("SELECT * FROM security_whitelist ORDER BY user_id,scope"))
        protected = rowsdict(fetchall("SELECT * FROM protected_users ORDER BY added_at DESC"))
        return jsonify({"settings": settings, "whitelist": whitelist, "protected_users": protected})

    @app.post("/api/security")
    def api_security_save():
        require_module("security"); verify_csrf()
        data=request.get_json(silent=True) or {}
        for key,value in data.items(): set_security(key,value)
        audit("panel_security_update", session["user"]["id"], None, ",".join(data.keys()))
        return json_ok()

    @app.post("/api/security/whitelist")
    def api_whitelist():
        require_module("security"); verify_csrf()
        data=request.get_json(silent=True) or {}; action=data.get("action"); uid=str(data.get("user_id") or ""); scope=data.get("scope") or "all"
        if not uid: return json_error("User ID lipsă")
        if action=="add": execute("INSERT OR IGNORE INTO security_whitelist(user_id,scope) VALUES(?,?)", (uid,scope))
        elif action=="remove": execute("DELETE FROM security_whitelist WHERE user_id=? AND scope=?", (uid,scope))
        else: return json_error("Acțiune invalidă")
        return json_ok()

    @app.post("/api/security/protected-users")
    def api_protected_users():
        require_superuser(); verify_csrf()
        data = request.get_json(silent=True) or {}
        uid = str(data.get("user_id") or "").strip()
        action = data.get("action")
        if not uid: return json_error("User ID lipsă")
        if action == "add":
            set_protected_user(uid, True, str(data.get("note") or ""), session["user"]["id"])
        elif action == "remove":
            if uid == owner: return json_error("Ownerul rămâne protejat permanent.")
            set_protected_user(uid, False)
        else:
            return json_error("Acțiune invalidă")
        audit(f"panel_protected_user_{action}", session["user"]["id"], uid, str(data.get("note") or ""))
        return json_ok()

    # ---------- BACKUPS ----------
    @app.get("/api/backups")
    def api_backups():
        require_module("backups")
        rows=rowsdict(fetchall("SELECT id,kind,guild_id,created_at,data FROM snapshots ORDER BY id DESC LIMIT 100"))
        for r in rows:
            try:
                data=json.loads(r.pop("data")); r["roles"]=len(data.get("roles",[])); r["channels"]=len(data.get("channels",[]))
            except Exception:
                r.pop("data",None); r["roles"]=0; r["channels"]=0
        return jsonify(rows)

    @app.post("/api/backups/create")
    def api_backup_create():
        require_module("backups"); verify_csrf()
        data = request.get_json(silent=True) or {}
        require_critical_confirmation(data)
        async def work():
            bot=get_bot(); guild=bot.get_guild(guild_id())
            if not guild: raise RuntimeError("Guild indisponibil")
            make_snapshot(guild)
        try: run_coroutine(work()); return json_ok()
        except Exception as e: return json_error(e)

    # ---------- LOGS ----------
    @app.get("/api/logs")
    def api_logs():
        require_module("logs")
        q=request.args.get("q","").strip(); typ=request.args.get("type","all")
        clauses=[]; params=[]
        if q:
            clauses.append("(actor_id LIKE ? OR target_id LIKE ? OR details LIKE ?)"); like=f"%{q}%"; params += [like,like,like]
        if typ!="all": clauses.append("type=?"); params.append(typ)
        where=" WHERE "+" AND ".join(clauses) if clauses else ""
        rows=rowsdict(fetchall(f"SELECT * FROM audit_events{where} ORDER BY id DESC LIMIT 1000", params))
        types=[r["type"] for r in fetchall("SELECT DISTINCT type FROM audit_events ORDER BY type")]
        return jsonify({"rows":rows,"types":types})

    # ---------- CONFIG ----------
    @app.get("/api/config")
    def api_config():
        require_module("config")
        return jsonify({"config": {r["key"]:r["value"] for r in fetchall("SELECT key,value FROM config ORDER BY key")}})

    @app.post("/api/config")
    def api_config_save():
        require_module("config"); verify_csrf()
        data=request.get_json(silent=True) or {}
        for k,v in data.items(): set_config(k,v)
        audit("panel_config_update", session["user"]["id"], None, ",".join(data.keys()))
        return json_ok()

    @app.get("/api/discord-meta")
    def api_discord_meta():
        require_module("config")
        async def work():
            bot=get_bot(); guild=bot.get_guild(guild_id())
            if not guild: raise RuntimeError("Guild indisponibil")
            return {
                "roles": [{"id":str(r.id),"name":r.name} for r in guild.roles if not r.is_default()],
                "channels": [{"id":str(c.id),"name":c.name,"type":str(c.type)} for c in guild.channels if not isinstance(c, discord.CategoryChannel)],
                "categories": [{"id":str(c.id),"name":c.name} for c in guild.categories],
            }
        try: return jsonify(run_coroutine(work()))
        except Exception as e: return json_error(e,503)

    # ---------- SUPERUSERS ----------
    @app.get("/api/superusers")
    def api_superusers():
        require_module("superusers")
        rows=rowsdict(fetchall("SELECT * FROM superusers ORDER BY added_at DESC"))
        for r in rows:
            r["permissions"]={m:panel_permission(r["user_id"],m,owner) for m in MODULES}
            last=fetchone("SELECT created_at,username FROM panel_login_events WHERE user_id=? ORDER BY id DESC LIMIT 1", (r["user_id"],))
            r["last_login"]=rowdict(last)
        return jsonify(rows)

    @app.post("/api/superusers")
    def api_superusers_change():
        require_owner(); verify_csrf()
        data=request.get_json(silent=True) or {}; action=data.get("action"); uid=str(data.get("user_id") or "")
        if not uid: return json_error("User ID lipsă")
        if action=="add": execute("INSERT OR IGNORE INTO superusers(user_id,added_by) VALUES(?,?)", (uid,session["user"]["id"]))
        elif action=="remove":
            execute("DELETE FROM superusers WHERE user_id=?", (uid,)); execute("DELETE FROM panel_permissions WHERE user_id=?", (uid,))
        else: return json_error("Acțiune invalidă")
        audit(f"panel_superuser_{action}",session["user"]["id"],uid,"")
        return json_ok()

    @app.post("/api/superusers/<user_id>/permissions")
    def api_superuser_permissions(user_id):
        require_owner(); verify_csrf()
        data=request.get_json(silent=True) or {}
        for module,allowed in data.items():
            if module in MODULES: set_panel_permission(user_id,module,bool(allowed),session["user"]["id"])
        return json_ok()

    @app.get("/health")
    def health():
        return jsonify({"ok":True,"discord_bridge":bridge_ready()})

    return app


def run_dashboard():
    app = make_app()
    serve(
        app,
        host=os.getenv("DASHBOARD_HOST", "127.0.0.1"),
        port=int(os.getenv("DASHBOARD_PORT", "3000")),
        threads=8,
    )
