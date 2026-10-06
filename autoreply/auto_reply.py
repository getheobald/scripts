#!/usr/bin/env python3
"""
auto_reply.py

Sends an automated text to a saved contact when:
  - the chat is one-on-one (no group chats)
  - the sender is in your Contacts (no unknown numbers)
  - their latest real message (reactions ignored) has gone unanswered for WAIT_HOURS
  - the chat is still unread

Designed to run every 30 minutes via launchd
Leave DRY_RUN = True until log output looks right
"""
import glob
import json
import os
import re
import sqlite3
import subprocess
import time
from datetime import datetime

# config

REPLY_TEXT = (
    "[Automated] So sorry I missed your text! Please feel free to ping me "
    "again so I see it, or pick up the phone and give me a call!!!"
)
WAIT_HOURS = 24      # measured from the FIRST message in the unanswered streak
MAX_AGE_DAYS = 7     # skip chats whose latest message is older than this
                     # (stops the first run from texting people about 2023 messages)
DRY_RUN = True       # set to False to actually send

HOME = os.path.expanduser("~")
CHAT_DB = f"{HOME}/Library/Messages/chat.db"
ADDRESSBOOK_PATTERNS = [
    f"{HOME}/Library/Application Support/AddressBook/AddressBook-v22.abcddb",
    f"{HOME}/Library/Application Support/AddressBook/Sources/*/AddressBook-v22.abcddb",
]
STATE_FILE = f"{HOME}/.auto_reply_state.json"
APPLE_EPOCH = 978307200  # 2001-01-01 00:00:00 UTC as a Unix timestamp

APPLESCRIPT = """
on run argv
    set theGuid to item 1 of argv
    set theHandle to item 2 of argv
    set theText to item 3 of argv
    tell application "Messages"
        try
            send theText to chat id theGuid
        on error
            set svc to 1st account whose service type = iMessage
            send theText to participant theHandle of svc
        end try
    end tell
end run
"""


# helpers
def log(msg):
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}", flush=True)


def open_ro(path):
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def normalize(handle):
    """Emails lowercased; phone numbers reduced to their last 10 digits,
    so '+1 (617) 555-1234' and '6175551234' match."""
    handle = handle.strip().lower()
    if "@" in handle:
        return handle
    return re.sub(r"\D", "", handle)[-10:]


def apple_to_unix(d):
    # macOS uses nanoseconds
    return (d / 1e9 if d > 1e11 else d) + APPLE_EPOCH


def load_contacts():
    known = set()
    paths = [p for pat in ADDRESSBOOK_PATTERNS for p in glob.glob(pat)]
    for path in paths:
        try:
            con = open_ro(path)
            for (num,) in con.execute(
                "SELECT ZFULLNUMBER FROM ZABCDPHONENUMBER WHERE ZFULLNUMBER IS NOT NULL"
            ):
                known.add(normalize(num))
            for (addr,) in con.execute(
                "SELECT ZADDRESS FROM ZABCDEMAILADDRESS WHERE ZADDRESS IS NOT NULL"
            ):
                known.add(normalize(addr))
            con.close()
        except sqlite3.Error as e:
            log(f"Couldn't read contacts db {path}: {e}")
    known.discard("")
    return known


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def one_on_one_chats(con):
    # style 45 = one-on-one chat; also require exactly one participant
    return con.execute("""
        SELECT c.ROWID, c.guid, h.id
        FROM chat c
        JOIN chat_handle_join chj ON chj.chat_id = c.ROWID
        JOIN handle h ON h.ROWID = chj.handle_id
        WHERE c.style = 45
          AND c.ROWID IN (SELECT chat_id FROM chat_handle_join
                          GROUP BY chat_id HAVING COUNT(*) = 1)
    """).fetchall()


def recent_messages(con, chat_id):
    # Their reactions are dropped (associated_message_type != 0 and not from me),
    # but my reactions are kept, so reacting to their text counts as a reply
    # item_type = 0 drops system events (renames, participant changes, etc)
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
    """Their messages since your last real message, newest first."""
    streak = []
    for rowid, from_me, is_read, date in msgs:
        if from_me:
            break
        streak.append((rowid, is_read, apple_to_unix(date)))
    return streak


def send(guid, handle, text):
    result = subprocess.run(
        ["osascript", "-", guid, handle, text],
        input=APPLESCRIPT, capture_output=True, text=True,
    )
    if result.returncode != 0:
        log(f"  Send failed: {result.stderr.strip()}")
        return False
    return True


# main
def main():
    contacts = load_contacts()
    if not contacts:
        log("No contacts loaded. Check Full Disk Access. Exiting so nobody gets texted by mistake.")
        return

    state = load_state()
    now = time.time()
    con = open_ro(CHAT_DB)

    for chat_id, guid, handle in one_on_one_chats(con):
        if normalize(handle) not in contacts:
            continue

        streak = unanswered_streak(recent_messages(con, chat_id))
        if not streak:
            continue  # last real message was mine, or chat is empty

        newest_rowid, _, newest_ts = streak[0]
        oldest_ts = streak[-1][2]

        if not any(is_read == 0 for _, is_read, _ in streak):
            continue  # chat has been read
        if now - newest_ts > MAX_AGE_DAYS * 86400:
            continue  # stale conversation
        if now - oldest_ts < WAIT_HOURS * 3600:
            continue  # hasn't been 24 hours yet
        if state.get(guid) == newest_rowid:
            continue  # already auto-replied to this streak

        waited = (now - oldest_ts) / 3600
        log(f"{handle}: {len(streak)} unread message(s), waiting {waited:.1f}h")

        if DRY_RUN:
            log("  [dry run] would send auto-reply")
            continue

        if send(guid, handle, REPLY_TEXT):
            log("  Auto-reply sent")
            state[guid] = newest_rowid
            save_state(state)

    con.close()


if __name__ == "__main__":
    main()
