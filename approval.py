"""Durable, single-use reply approvals for Graph-delivered health reports.

The enqueue callback MUST use a stable idempotency key derived from report_id:
SQLite cannot atomically commit a transaction with an external Kanban service.
"""
from __future__ import annotations

import datetime as dt
import html
import json
import re
import sqlite3
from contextlib import contextmanager, closing
from pathlib import Path

ALLOWED_ACTIONS = frozenset({'plugins', 'restart'})


def _time(value):
    try:
        result = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        return result.astimezone(dt.timezone.utc) if result.tzinfo else None
    except (TypeError, ValueError, AttributeError):
        return None


def parse(text, actions):
    """Accept only a standalone command followed by no unquoted content."""
    if not isinstance(text, str) or len(text) > 16000 or not actions or any(a not in ALLOWED_ACTIONS for a in actions):
        return None
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    content = []
    for i, line in enumerate(lines):
        if line.startswith('>'):
            continue
        if (i + 1 < len(lines) and lines[i + 1].startswith('>')
                and re.fullmatch(r'On .+ wrote:', line, re.I)):
            continue
        content.append(line)
    if len(content) != 1:
        return None
    match = re.fullmatch(r'approved(?:[ \t]+[0-9]{1,3})*', content[0], re.I | re.ASCII)
    if not match:
        return None
    nums = [int(n) for n in content[0].split()[1:]]
    if len(set(nums)) != len(nums) or any(n < 1 or n > len(actions) for n in nums):
        return None
    chosen = [a for i, a in enumerate(actions, 1) if not nums or i in nums]
    return sorted(chosen, key=lambda a: a == 'restart')


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        with self._connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS offers (
                report_id TEXT PRIMARY KEY, mailbox TEXT NOT NULL, message_id TEXT NOT NULL,
                conversation_id TEXT NOT NULL, sent_at TEXT NOT NULL, actions TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active', card_id TEXT, plan TEXT NOT NULL DEFAULT '{}',
                owner TEXT, selected TEXT, result_state TEXT, restart_state TEXT)''')
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS active_conversation ON offers(mailbox, conversation_id) WHERE status = 'active'")
            # Do not backfill: older consumed offers may already have run.
            db.execute('''CREATE TABLE IF NOT EXISTS plugin_approvals (
                report_id TEXT PRIMARY KEY, claimed INTEGER NOT NULL DEFAULT 0)''')

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        with closing(db), db:
            yield db

    @staticmethod
    def _offer(row):
        return {'mailbox': row['mailbox'], 'id': row['message_id'],
                'conversationId': row['conversation_id'], 'sentDateTime': row['sent_at'],
                'report_id': row['report_id'], 'actions': json.loads(row['actions']),
                'plan': json.loads(row['plan'])}

    def activate(self, sent, report_id, actions, plan=None):
        if (not isinstance(report_id, str) or not report_id or
            any(not isinstance(sent.get(k), str) or not sent[k].strip() for k in ('mailbox', 'id', 'conversationId', 'sentDateTime')) or
            _time(sent['sentDateTime']) is None or not isinstance(actions, list) or not actions or
            any(a not in ALLOWED_ACTIONS for a in actions) or len(set(actions)) != len(actions)):
            raise ValueError('invalid approval offer')
        with self._connect() as db:
            db.execute("UPDATE offers SET status='superseded' WHERE mailbox=? AND conversation_id=? AND status='active'",
                       (sent['mailbox'].lower(), sent['conversationId']))
            db.execute('INSERT INTO offers (report_id, mailbox, message_id, conversation_id, sent_at, actions, plan) VALUES (?, ?, ?, ?, ?, ?, ?)',
                       (report_id, sent['mailbox'].lower(), sent['id'], sent['conversationId'], sent['sentDateTime'], json.dumps(actions), json.dumps(plan or {})))

    def active(self):
        with self._connect() as db:
            return [self._offer(row) for row in db.execute("SELECT * FROM offers WHERE status = 'active'")]


def poll(store, graph, config, enqueue):
    if config.get('approvals') is not True:
        return
    owners = {x.lower() for x in config.get('owners', []) if isinstance(x, str) and x}
    if not owners:
        return
    for initial in store.active():
        now = dt.datetime.now(dt.timezone.utc)
        sent = _time(initial['sentDateTime'])
        if sent is None or now < sent or now > sent + dt.timedelta(hours=72):
            continue
        replies = graph.replies(initial)
        def order(reply):
            return _time(reply.get('receivedDateTime')) or dt.datetime.max.replace(tzinfo=dt.timezone.utc)
        for reply in sorted(replies, key=order):
            # The deployment must authenticate this address before mail reaches Graph.
            sender = reply.get('sender')
            mailbox = reply.get('mailbox')
            when = _time(reply.get('receivedDateTime'))
            if (not isinstance(sender, str) or sender.lower() not in owners or
                sender.lower() == initial['mailbox'].lower() or
                not isinstance(mailbox, str) or mailbox.lower() != initial['mailbox'].lower() or
                reply.get('conversationId') != initial['conversationId'] or
                not isinstance(reply.get('id'), str) or not reply['id'] or reply['id'] == initial['id'] or
                when is None or not (sent < when <= now and when <= sent + dt.timedelta(hours=72))):
                continue
            selected = parse(reply.get('text'), initial['actions'])
            if not selected:
                continue
            with store._connect() as db:
                db.execute('BEGIN IMMEDIATE')
                row = db.execute("SELECT * FROM offers WHERE report_id = ? AND status = 'active'", (initial['report_id'],)).fetchone()
                if row is None:
                    break
                offer = store._offer(row)
                card_id = enqueue(offer, selected)
                if not isinstance(card_id, str) or not card_id:
                    raise ValueError('enqueue did not return a card ID')
                db.execute("UPDATE offers SET status = 'consumed', card_id = ?, owner = ?, selected = ? WHERE report_id = ?", (card_id, sender.lower(), json.dumps(selected), offer['report_id']))
                if 'plugins' in selected:
                    db.execute('INSERT INTO plugin_approvals (report_id) VALUES (?)', (offer['report_id'],))
            graph.send('Approval received', '<p>Maintenance card ' + html.escape(card_id) + ' was created.</p>', offer['report_id'], recipients=[sender])
            break
