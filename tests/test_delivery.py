import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import delivery


CFG = {'sender': 'bot@example.com', 'recipients': ['owner@example.com'], 'delivery': {'graph': True, 'smtp': True, 'profile': True}, 'profile': {'target': 'test:owner'}}
SENT = {'id': 's1', 'conversationId': 'c1', 'sentDateTime': '2026-01-01T00:00:00Z', 'subject': 'Report [health-report:abc]', 'from': {'emailAddress': {'address': 'bot@example.com'}}}


class GraphTests(unittest.TestCase):
    def setUp(self):
        self.graph = delivery.Graph('/nonexistent/profile', CFG)
        self.graph._token = Mock(return_value='test-token')
        self.graph._identity = Mock(return_value=True)

    def test_default_disabled(self):
        g = delivery.Graph('/no', {})
        self.assertFalse(g.available())
        self.assertFalse(g.reply_available())
        self.assertEqual(g.send('a', 'b', 'id'), ('definite', None))

    def test_enabled_graph_without_helper_is_unavailable(self):
        graph = delivery.Graph('/no', CFG)
        self.assertFalse(graph.available())
        self.assertEqual(graph.send('a', 'b', 'id'), ('definite', None))

    def test_configured_helper_uses_selected_profile_credentials(self):
        with TemporaryDirectory() as directory:
            home = Path(directory)
            helper = home / 'example_token.py'
            helper.write_text('ENV = CACHE = None\ndef get_token():\n    return ENV, CACHE\n', encoding='utf-8')
            for path in [helper.name, str(helper)]:
                with self.subTest(path=path):
                    graph = delivery.Graph(home, {**CFG, 'graph_token_helper': path})
                    self.assertEqual(graph._token(), (home / '.env', home / 'secrets/msgraph-delegated.json'))

    def test_send_confirmation_exact_unique(self):
        self.graph._request = Mock(return_value=(202, {}))
        self.graph._messages = Mock(return_value=[SENT])
        state, sent = self.graph.send('Report', '<b>hello</b>', 'abc')
        self.assertEqual(state, 'sent')
        self.assertEqual(sent, {'mailbox': 'bot@example.com', 'id': 's1', 'conversationId': 'c1', 'sentDateTime': SENT['sentDateTime']})
        self.assertEqual(self.graph._request.call_args.args[3]['message']['subject'], SENT['subject'])
        self.assertIn('subject+eq', self.graph._messages.call_args.args[0])

    def test_send_rejection_and_unknown(self):
        for code, state in [(400, 'definite'), (403, 'definite'), (429, 'definite'), (500, 'ambiguous'), (503, 'ambiguous')]:
            with self.subTest(code=code):
                self.graph._request = Mock(return_value=(code, {}))
                self.assertEqual(self.graph.send('Report', 'x', 'abc'), (state, None))
        self.graph._request = Mock(side_effect=TimeoutError())
        self.assertEqual(self.graph.send('Report', 'x', 'abc'), ('ambiguous', None))

    def test_duplicate_and_bad_sender_are_ambiguous(self):
        self.graph._request = Mock(return_value=(202, {}))
        self.graph._messages = Mock(return_value=[SENT, SENT])
        self.assertEqual(self.graph.send('Report', 'x', 'abc'), ('ambiguous', None))
        self.graph._messages.return_value = [{**SENT, 'from': {'emailAddress': {'address': 'wrong@example.com'}}}]
        self.assertEqual(self.graph.send('Report', 'x', 'abc'), ('ambiguous', None))

    @patch('delivery.time.sleep')
    def test_no_match_and_read_failure_are_ambiguous(self, sleep):
        self.graph._request = Mock(return_value=(202, {}))
        self.graph._messages = Mock(return_value=[{**SENT, 'subject': SENT['subject'] + 'x'}])
        self.assertEqual(self.graph.send('Report', 'x', 'abc'), ('ambiguous', None))
        self.graph._messages.side_effect = TimeoutError()
        self.assertEqual(self.graph.send('Report', 'x', 'abc'), ('ambiguous', None))

    def test_replies_require_sent_identity_thread_and_text(self):
        offer = {'mailbox': 'bot@example.com', 'id': 's1', 'conversationId': 'c1'}
        self.graph._request = Mock(return_value=(200, {**SENT, 'internetMessageId': '<original@example.com>'}))
        base = {'id': 'r1', 'conversationId': 'c1', 'receivedDateTime': 'today', 'from': {'emailAddress': {'address': 'owner@example.com'}}, 'body': {'contentType': 'text', 'content': 'approved'}}
        base['sender'] = base['from']
        base['internetMessageHeaders'] = [{'name': 'In-Reply-To', 'value': '<original@example.com>'}]
        self.graph._messages = Mock(return_value=[base, {**base, 'internetMessageHeaders': []}, {**base, 'conversationId': 'other'}, {**base, 'body': {'contentType': 'html', 'content': '<p>approved</p>'}}, {**base, 'from': SENT['from']}])
        self.assertEqual(self.graph.replies(offer), [{'sender': 'owner@example.com', 'id': 'r1', 'conversationId': 'c1', 'receivedDateTime': 'today', 'text': 'approved', 'mailbox': 'bot@example.com'}])
        self.graph._request.return_value = (200, {**SENT, 'conversationId': 'other'})
        self.assertEqual(self.graph.replies(offer), [])
        self.assertEqual(self.graph.replies({**offer, 'mailbox': 'wrong'}), [])

    def test_replies_return_the_full_body(self):
        offer = {'mailbox': 'bot@example.com', 'id': 's1', 'conversationId': 'c1'}
        anchor = '<original@example.com>'
        self.graph._request = Mock(return_value=(200, {**SENT, 'internetMessageId': anchor}))
        address = {'emailAddress': {'address': 'owner@example.com'}}
        body = 'approved\n> history\nPlease wait'
        self.graph._messages = Mock(return_value=[{
            'id': 'r1', 'conversationId': 'c1', 'receivedDateTime': 'today',
            'from': address, 'sender': address,
            'body': {'contentType': 'text', 'content': body},
            'uniqueBody': {'contentType': 'text', 'content': 'approved'},
            'internetMessageHeaders': [{'name': 'In-Reply-To', 'value': anchor}],
        }])
        self.assertEqual(self.graph.replies(offer)[0]['text'], body)
        self.assertIn(',body,', self.graph._messages.call_args.args[0].replace('%2C', ','))

    def test_preflight_failure_is_definite(self):
        self.graph._identity.return_value = False
        self.assertEqual(self.graph.send('Report', 'x', 'abc'), ('definite', None))
        self.assertEqual(self.graph.send('Report', 'x', 'bad\nid'), ('definite', None))


class FallbackTests(unittest.TestCase):
    def test_default_off(self):
        self.assertIsNone(delivery.fallback('/x', {}, 's', 'private', 'redacted'))

    @patch('delivery.subprocess.run')
    @patch('delivery._smtp', return_value='ambiguous')
    def test_ambiguous_stops(self, smtp, run):
        self.assertEqual(delivery.fallback('/x', CFG, 's', 'private', 'redacted'), 'ambiguous')
        run.assert_not_called()

    @patch('delivery.subprocess.run')
    @patch('delivery._smtp', return_value=None)
    def test_profile_uses_redacted_stdin(self, smtp, run):
        run.return_value = subprocess.CompletedProcess([], 0, '{"success": true}', '')
        self.assertEqual(delivery.fallback('/x', CFG, 'subject', 'private', '<p>redacted</p>'), 'profile')
        self.assertNotIn('private', run.call_args.kwargs['input'])
        self.assertIn('redacted', run.call_args.kwargs['input'])
        self.assertEqual(run.call_args.kwargs['env']['HERMES_HOME'], '/x')

    @patch('delivery.subprocess.run', side_effect=TimeoutError())
    @patch('delivery._smtp', return_value=None)
    def test_profile_timeout_is_ambiguous(self, smtp, run):
        self.assertEqual(delivery.fallback('/x', CFG, 's', 'a', 'b'), 'ambiguous')

    @patch('delivery.smtplib.SMTP')
    def test_smtp_submission_unknown(self, smtp):
        smtp.return_value.send_message.side_effect = TimeoutError()
        cfg = {**CFG, 'smtp': {'host': 'smtp.example.com'}, 'delivery': {'smtp': True}}
        self.assertEqual(delivery.fallback('/x', cfg, 's', '<p>a</p>', 'b'), 'ambiguous')

    @patch('delivery.smtplib.SMTP')
    def test_smtp_success(self, smtp):
        smtp.return_value.send_message.return_value = {}
        cfg = {**CFG, 'smtp': {'host': 'smtp.example.com'}, 'delivery': {'smtp': True}}
        self.assertEqual(delivery.fallback('/x', cfg, 's', '<p>a</p>', 'b'), 'smtp')
        smtp.return_value.starttls.assert_called_once()


if __name__ == '__main__':
    unittest.main()
