from datetime import datetime, timezone
from ..db import fetchone, fetchall, execute

def clock_in(user_id):
    active = fetchone("SELECT * FROM timesheets WHERE user_id=? AND status='active'", (str(user_id),))
    if active:
        raise RuntimeError("Ai deja un pontaj activ.")
    now = datetime.now(timezone.utc).isoformat()
    tid = execute(
        "INSERT INTO timesheets(user_id,clock_in,status) VALUES(?,?,'active')",
        (str(user_id), now)
    )
    return fetchone("SELECT * FROM timesheets WHERE id=?", (tid,))

def set_log_message(ts_id, channel_id, message_id):
    execute(
        "UPDATE timesheets SET log_channel_id=?,log_message_id=? WHERE id=?",
        (str(channel_id), str(message_id), int(ts_id))
    )

def clock_out(user_id):
    active = fetchone(
        "SELECT * FROM timesheets WHERE user_id=? AND status='active' ORDER BY id DESC LIMIT 1",
        (str(user_id),)
    )
    if not active:
        raise RuntimeError("Nu ai niciun pontaj activ.")
    end = datetime.now(timezone.utc)
    start = datetime.fromisoformat(active["clock_in"])
    mins = max(0, int((end-start).total_seconds() // 60))
    execute(
        "UPDATE timesheets SET clock_out=?,duration_minutes=?,status='closed' WHERE id=?",
        (end.isoformat(), mins, active["id"])
    )
    return fetchone("SELECT * FROM timesheets WHERE id=?", (active["id"],))

def cancel_timesheet(ts_id, by_user_id):
    execute(
        "UPDATE timesheets SET status='cancelled',cancelled_by=? WHERE id=?",
        (str(by_user_id), int(ts_id))
    )
    return fetchone("SELECT * FROM timesheets WHERE id=?", (int(ts_id),))

def adjust_time(user_id, minutes, by_user_id, reason):
    execute(
        "INSERT INTO timesheet_adjustments(user_id,minutes,reason,changed_by) VALUES(?,?,?,?)",
        (str(user_id), int(minutes), reason, str(by_user_id))
    )

def total_minutes(user_id):
    row = fetchone(
        "SELECT COALESCE(SUM(duration_minutes),0) total FROM timesheets WHERE user_id=? AND status='closed'",
        (str(user_id),)
    )
    adj = fetchone(
        "SELECT COALESCE(SUM(minutes),0) total FROM timesheet_adjustments WHERE user_id=?",
        (str(user_id),)
    )
    return max(0, int(row["total"] or 0) + int(adj["total"] or 0))

def reset_time(user_id, by_user_id):
    adjust_time(user_id, -total_minutes(user_id), by_user_id, "Reset pontaj")

def top_timesheets(limit=10):
    return fetchall("""
        SELECT m.discord_id,m.first_name,m.last_name,m.callsign,
        COALESCE(SUM(CASE WHEN t.status='closed' THEN t.duration_minutes ELSE 0 END),0)
        + COALESCE((SELECT SUM(a.minutes) FROM timesheet_adjustments a WHERE a.user_id=m.discord_id),0) total_minutes
        FROM members m
        LEFT JOIN timesheets t ON t.user_id=m.discord_id
        WHERE m.status='active'
        GROUP BY m.discord_id
        ORDER BY total_minutes DESC
        LIMIT ?
    """, (int(limit),))
