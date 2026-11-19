import datetime as dt
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from run import load

maintenance = load().maintenance
REPORT = 'a' * 32
CARD = 't_test'


@pytest.fixture
def approved(tmp_path, monkeypatch):
    home = tmp_path
    root = home / maintenance.NAME
    root.mkdir()
    config = {'approvals': True, 'kanban_board': 'test', 'maintenance_profile': 'worker',
              'owners': ['owner@example.test'], 'update_plugins': ['example-plugin']}
    store = maintenance.approval.Store(root / 'approvals.sqlite3')
    now = dt.datetime.now(dt.timezone.utc)
    sent = dict(mailbox='bot@example.test', id='s1', conversationId='c1',
                sentDateTime=(now - dt.timedelta(minutes=1)).isoformat())
    store.activate(sent, REPORT, ['plugins'], maintenance.plan(home, config))
    graph = Mock()
    graph.replies.return_value = [dict(sender='owner@example.test', mailbox=sent['mailbox'],
                                      id='r1', conversationId='c1', receivedDateTime=now.isoformat(),
                                      text='approved')]
    maintenance.approval.poll(store, graph, config, lambda *_: CARD)

    board = home / 'board.sqlite3'
    with sqlite3.connect(board) as db:
        db.execute('CREATE TABLE tasks (id TEXT, status TEXT, assignee TEXT, idempotency_key TEXT, '
                   'current_run_id INTEGER, claim_lock TEXT, claim_expires INTEGER)')
        db.execute('INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?)',
                   (CARD, 'running', 'worker', f'{maintenance.NAME}:{REPORT}', 7, 'claim', int(time.time()) + 60))

    def get_task(db, task_id):
        db.row_factory = sqlite3.Row
        row = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
        return SimpleNamespace(**dict(row)) if row else None

    monkeypatch.setattr(maintenance, '_db', lambda _: (SimpleNamespace(get_task=get_task), board, 'test'))
    monkeypatch.setenv('HERMES_KANBAN_TASK', CARD)
    monkeypatch.setenv('HERMES_KANBAN_BOARD', 'test')
    monkeypatch.setenv('HERMES_KANBAN_DB', str(board))
    monkeypatch.setenv('HERMES_KANBAN_RUN_ID', '7')
    monkeypatch.setenv('HERMES_KANBAN_CLAIM_LOCK', 'claim')
    updates = Mock(return_value=[])
    monkeypatch.setattr(maintenance, '_update_plugins', updates)
    return home, config, board, updates


@pytest.mark.parametrize('change', [
    "UPDATE tasks SET status='done'",
    "UPDATE tasks SET assignee='changed'",
    "UPDATE tasks SET idempotency_key='other'",
    "UPDATE tasks SET current_run_id=8",
    "UPDATE tasks SET claim_lock='changed'",
    "UPDATE tasks SET claim_expires=1",
    "DELETE FROM tasks",
])
def test_requires_live_matching_card(approved, change):
    home, config, board, updates = approved
    with sqlite3.connect(board) as db:
        db.execute(change)
    with pytest.raises(ValueError):
        maintenance.update(home, config, REPORT)
    updates.assert_not_called()


def test_claim_only_one_concurrent_update(approved):
    home, config, _, updates = approved
    start = Barrier(2, timeout=5)

    def attempt(_):
        start.wait()
        try:
            maintenance.update(home, config, REPORT)
            return 'updated'
        except ValueError:
            return 'refused'

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(attempt, range(2))) == ['refused', 'updated']
    with pytest.raises(ValueError):
        maintenance.update(home, config, REPORT)
    updates.assert_called_once_with(home, ['example-plugin'])


def test_failed_update_does_not_release_claim(approved):
    home, config, _, updates = approved
    updates.return_value = [{'name': 'example-plugin', 'status': 'failed'}]
    with pytest.raises(ValueError, match='At least one plugin update failed'):
        maintenance.update(home, config, REPORT)
    with pytest.raises(ValueError):
        maintenance.update(home, config, REPORT)
    updates.assert_called_once()


def test_other_worker_board_is_refused(approved, monkeypatch):
    home, config, _, updates = approved
    monkeypatch.setenv('HERMES_KANBAN_BOARD', 'other')
    with pytest.raises(ValueError):
        maintenance.update(home, config, REPORT)
    updates.assert_not_called()


def test_pre_upgrade_consumed_offer_is_not_eligible(approved):
    home, config, _, updates = approved
    store = maintenance.approval.Store(home / maintenance.NAME / 'approvals.sqlite3')
    with store._connect() as db:
        db.execute('DROP TABLE plugin_approvals')
    with pytest.raises(ValueError, match='unavailable or already claimed'):
        maintenance.update(home, config, REPORT)
    updates.assert_not_called()


def test_card_cannot_change_while_approval_claim_commits(approved, monkeypatch):
    home, config, board, _ = approved
    store_path = home / maintenance.NAME / 'approvals.sqlite3'
    connect = sqlite3.connect
    commits = []

    def trace(sql):
        if sql == 'COMMIT':
            with connect(board, timeout=0) as other:
                try:
                    other.execute("UPDATE tasks SET status='done'")
                except sqlite3.OperationalError as exc:
                    commits.append(exc.sqlite_errorcode)
                else:
                    commits.append(None)

    def connect_traced(path, *args, **kwargs):
        db = connect(path, *args, **kwargs)
        if path == store_path:
            db.set_trace_callback(trace)
        return db

    monkeypatch.setattr(sqlite3, 'connect', connect_traced)
    maintenance.update(home, config, REPORT)
    assert commits == [sqlite3.SQLITE_BUSY]
