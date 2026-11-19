import datetime as dt

from approval import Store, parse, poll


def test_parser_strict():
    assert parse('approved 2 1', ['restart', 'plugins']) == ['plugins', 'restart']
    assert parse('approved\n\nOn Tuesday wrote:\n> no', ['plugins']) == ['plugins']
    for text in ['not approved', 'approved?', 'approved 3', 'approved 0', '> approved', 'please approved', 'approved but wait', 'approved\nno']:
        assert parse(text, ['plugins']) is None


def test_poll_consumes_once(tmp_path):
    now = dt.datetime.now(dt.timezone.utc)
    sent = dict(mailbox='bot@example.com', id='s1', conversationId='c1', sentDateTime=(now - dt.timedelta(minutes=1)).isoformat())
    store = Store(tmp_path / 'db.sqlite')
    store.activate(sent, 'report1', ['restart', 'plugins'])
    class Graph:
        def replies(self, offer):
            return [dict(sender='owner@example.com', mailbox='bot@example.com', id='r1', conversationId='c1', receivedDateTime=now.isoformat(), text='approved')]
        def send(self, *args, **kwargs):
            self.confirmed = (args, kwargs)
    graph = Graph()
    calls = []
    def enqueue(offer, selected):
        calls.append((offer, selected))
        return 'card1'
    poll(store, graph, {'approvals': True, 'owners': ['owner@example.com']}, enqueue)
    poll(Store(tmp_path / 'db.sqlite'), graph, {'approvals': True, 'owners': ['owner@example.com']}, enqueue)
    assert len(calls) == 1
    assert calls[0][1] == ['plugins', 'restart']
    assert store.active() == []
    assert graph.confirmed[1]['recipients'] == ['owner@example.com']


def test_invalid_replies_and_disabled(tmp_path):
    now = dt.datetime.now(dt.timezone.utc)
    sent = dict(mailbox='bot@example.com', id='sent', conversationId='thread', sentDateTime=(now - dt.timedelta(hours=1)).isoformat())
    store = Store(tmp_path / 'invalid.db')
    store.activate(sent, 'report', ['plugins'])
    valid = dict(sender='owner@example.com', mailbox='bot@example.com', id='reply', conversationId='thread', receivedDateTime=now.isoformat(), text='approved')
    changes = [{'sender': 'bad@example.com'}, {'sender': 'bot@example.com'}, {'mailbox': 'other@example.com'}, {'conversationId': 'other'}, {'id': 'sent'}, {'receivedDateTime': sent['sentDateTime']}, {'receivedDateTime': (now + dt.timedelta(hours=1)).isoformat()}, {'text': '> approved'}, {'text': 'approved?'}]
    class Graph:
        def replies(self, offer): return [dict(valid, **change) for change in changes]
        def send(self, *args, **kwargs): raise AssertionError('must not send')
    def enqueue(*args): raise AssertionError('must not enqueue')
    poll(store, Graph(), {'approvals': True, 'owners': ['owner@example.com', 'bot@example.com']}, enqueue)
    poll(store, Graph(), {'approvals': False, 'owners': ['owner@example.com']}, enqueue)
    assert len(store.active()) == 1


def test_parallel_poll_only_creates_one(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier, Lock
    import time
    now = dt.datetime.now(dt.timezone.utc)
    db = tmp_path / 'concurrent.db'
    store = Store(db)
    store.activate(dict(mailbox='bot', id='sent', conversationId='thread', sentDateTime=(now-dt.timedelta(seconds=1)).isoformat()), 'r', ['plugins'])
    barrier, lock, calls = Barrier(2), Lock(), []
    class Graph:
        def replies(self, offer):
            barrier.wait(timeout=5)
            return [dict(sender='owner', mailbox='bot', id='reply', conversationId='thread', receivedDateTime=now.isoformat(), text='approved')]
        def send(self, *args, **kwargs): pass
    def enqueue(offer, selected):
        with lock: calls.append(offer['report_id'])
        time.sleep(.05)
        return 'card'
    def run(_): poll(Store(db), Graph(), {'approvals': True, 'owners': ['owner']}, enqueue)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(run, range(2)))
    assert calls == ['r']
    assert not store.active()


def test_invalid_offers_fail_closed(tmp_path):
    store = Store(tmp_path / 'bad.db')
    sent = dict(mailbox='bot', id='sent', conversationId='thread', sentDateTime=dt.datetime.now(dt.timezone.utc).isoformat())
    for actions in [[], ['os'], ['plugins', 'plugins']]:
        try: store.activate(sent, 'r', actions)
        except ValueError: pass
        else: raise AssertionError('invalid offer accepted')
    assert not store.active()
