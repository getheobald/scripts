#!/usr/bin/env python3
"""
morning_report.py

Texts a daily summary of one-on-one conversations with saved contacts
where the other person's last real message hasn't gotten a reply.
Their reactions are ignored; my reactions count as a reply.
"""
import glob
import os
import re
import sqlite3
import subprocess
import time
from datetime import datetime

# config
MY_HANDLE = "+12063358337" 
MIN_HOURS = 0               # only list chats that have waited at least this long
MAX_AGE_DAYS = 14           # ignore conversations that went quiet longer ago than this
INCLUDE_READ = True         # also list chats I've read but not answered
SEND_WHEN_EMPTY = True      # send an "all caught up" text when nobody is waiting
DRY_RUN = True              # print the report instead of texting it

HOME = os.path.expanduser("~")
CHAT_DB = f"{HOME}/Library/Messages/chat.db"
ADDRESSBOOK_PATTERNS = [
    f"{HOME}/Library/Application Support/AddressBook/AddressBook-v22.abcddb",
    f"{HOME}/Library/Application Support/AddressBook/Sources/*/AddressBook-v22.abcddb",
]
APPLE_EPOCH = 978307200  # 2001-01-01 00:00:00 UTC as a Unix timestamp

APPLESCRIPT = """
on run argv
    set theHandle to item 1 of argv
    set theText to item 2 of argv
    tell application "Messages"
        set svc to 1st account whose service type = iMessage
        send theText to participant theHandle of svc
    end tell
end run
"""


# helpers
def log(msg):
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}", flush=True)


def open_ro(path):
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def normalize(handle):
    """Emails lowercased; phone numbers reduced to their last 10 digits."""
    handle = (handle or "").strip().lower()
    if "@" in handle:
        return handle
    return re.sub(r"\D", "", handle)[-10:]


def apple_to_unix(d):
    return (d / 1e9 if d > 1e11 else d) + APPLE_EPOCH


def load_contacts():
    """Map normalized phone/email -> display name."""
    names = {}
    paths = [p for pat in ADDRESSBOOK_PATTERNS for p in glob.glob(pat)]
    for path in paths:
        try:
            con = open_ro(path)
            rows = con.execute("""
                SELECT p.ZFULLNUMBER, r.ZFIRSTNAME, r.ZLASTNAME, r.ZORGANIZATION
                FROM ZABCDPHONENUMBER p JOIN ZABCDRECORD r ON p.ZOWNER = r.Z_PK
                WHERE p.ZFULLNUMBER IS NOT NULL
                UNION ALL
                SELECT e.ZADDRESS, r.ZFIRSTNAME, r.ZLASTNAME, r.ZORGANIZATION
                FROM ZABCDEMAILADDRESS e JOIN ZABCDRECORD r ON e.ZOWNER = r.Z_PK
                WHERE e.ZADDRESS IS NOT NULL
            """).fetchall()
            con.close()
        except sqlite3.Error as e:
            log(f"Couldn't read contacts db {path}: {e}")
            continue
        for handle, first, last, org in rows:
            key = normalize(handle)
            if not key:
                continue
            name = " ".join(x for x in (first, last) if x) or org or handle
            names.setdefault(key, name)
    return names


def one_on_one_chats(con):
    return con.execute("""
        SELECT c.ROWID, h.id
        FROM chat c
        JOIN chat_handle_join chj ON chj.chat_id = c.ROWID
        JOIN handle h ON h.ROWID = chj.handle_id
        WHERE c.style = 45
          AND c.ROWID IN (SELECT chat_id FROM chat_handle_join
                          GROUP BY chat_id HAVING COUNT(*) = 1)
    """).fetchall()


def recent_messages(con, chat_id):
    # Their reactions are dropped; my reactions are kept (they count as a reply)
    return con.execute("""
        SELECT m.ROWID, m.is_from_me, m.is_read, m.date
        FROM message m
        JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
        WHERE cmj.chat_id = ?
          AND (m.associated_message_type = 0 OR m.is_from_me = 1)
          AND m.item_type = 0
        ORDER BY m.date DESC
        LIMIT 50
    """, (chat_id,)).fetchall()


def unanswered_streak(msgs):
    """Their messages since my last message or reaction, newest first."""
    streak = []
    for _, from_me, is_read, date in msgs:
        if from_me:
            break
        streak.append((is_read, apple_to_unix(date)))
    return streak


def ago(ts, now):
    hours = (now - ts) / 3600
    if hours < 1:
        return "<1h"
    if hours < 48:
        return f"{int(hours)}h"
    return f"{int(hours // 24)}d"


def send(handle, text):
    result = subprocess.run(
        ["osascript", "-", handle, text],
        input=APPLESCRIPT, capture_output=True, text=True,
    )
    if result.returncode != 0:
        log(f"Send failed: {result.stderr.strip()}")
        return False
    return True


# main
def main():
    contacts = load_contacts()
    if not contacts:
        log("No contacts loaded. Check Full Disk Access.")
        return

    me = normalize(MY_HANDLE)
    now = time.time()
    con = open_ro(CHAT_DB)

    # One entry per person, even if they have separate iMessage/SMS/email threads
    waiting = {}
    for chat_id, handle in one_on_one_chats(con):
        key = normalize(handle)
        if key == me or key not in contacts:
            continue

        streak = unanswered_streak(recent_messages(con, chat_id))
        if not streak:
            continue

        newest_ts = streak[0][1]
        oldest_ts = streak[-1][1]
        unread = any(is_read == 0 for is_read, _ in streak)

        if now - newest_ts > MAX_AGE_DAYS * 86400:
            continue
        if now - oldest_ts < MIN_HOURS * 3600:
            continue
        if not unread and not INCLUDE_READ:
            continue

        name = contacts[key]
        prev = waiting.get(name)
        if prev is None or oldest_ts < prev["since"]:
            waiting[name] = {"since": oldest_ts, "count": len(streak), "unread": unread}
        else:
            prev["unread"] = prev["unread"] or unread

    con.close()

    if waiting:
        people = sorted(waiting.items(), key=lambda kv: kv[1]["since"])  # longest wait first
        lines = [f"☀️ {len(people)} {'person' if len(people) == 1 else 'people'} waiting on a reply:"]
        for name, info in people:
            extras = []
            if info["count"] > 1:
                extras.append(f"{info['count']} msgs")
            if info["unread"]:
                extras.append("unread")
            suffix = f" ({', '.join(extras)})" if extras else ""
            lines.append(f"• {name}: {ago(info['since'], now)}{suffix}")
        report = "\n".join(lines)
    elif SEND_WHEN_EMPTY:
        report = "☀️ All caught up, nobody's waiting on you!"
    else:
        log("Nobody waiting; nothing sent.")
        return

    if DRY_RUN:
        print(report)
        return

    if send(MY_HANDLE, report):
        log(f"Report sent ({len(waiting)} people)")


if __name__ == "__main__":
    main()
