import json
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

import delivery


CFG = {'sender': 'bot@example.com', 'recipients': ['owner@example.com'], 'delivery': {'graph': True, 'smtp': True, 'profile': True}, 'profile': {'target': 'test:owner'}}
SENT = {'id': 's1', 'conversationId': 'c1', 'sentDateTime': '2026-01-01T00:00:00Z', 'subject': 'Report [health-report:abc]', 'from': {'emailAddress': {'address': 'bot@example.com'}}}
OFFER = {'mailbox': 'bot@example.com', 'id': 's1', 'conversationId': 'c1'}
ANCHOR = '<original@example.com>'


def response(data=None, *, status=200, headers=None, raw=None):
    body = json.dumps(data).encode() if raw is None else raw
    return SimpleNamespace(status=status, body=body, headers=headers or {})


def provider_patch(provider):
    return patch.dict(sys.modules, {
        'hermes_cli': SimpleNamespace(),
        'hermes_cli.plugin_provider_requests': SimpleNamespace(credentialed_provider_request=provider),
    })


class GraphTests(unittest.TestCase):
    def setUp(self):
        self.graph = delivery.Graph('/nonexistent/profile', CFG)
        self.graph._identity = Mock(return_value=True)

    def test_default_disabled(self):
        g = delivery.Graph('/no', {})
        self.assertFalse(g.available())
        self.assertFalse(g.reply_available())
        self.assertEqual(g.send('a', 'b', 'id'), ('definite', None))

    def test_provider_failure_is_unavailable(self):
        provider = Mock(side_effect=RuntimeError('no credentials'))
        graph = delivery.Graph('/no', CFG)
        with provider_patch(provider):
            self.assertFalse(graph.available())
            self.assertEqual(graph.send('a', 'b', 'id'), ('definite', None))

    def test_legacy_helper_is_not_executed(self):
        provider = Mock(side_effect=RuntimeError('no credentials'))
        with TemporaryDirectory() as directory, provider_patch(provider):
            home = Path(directory)
            helper = home / 'example_token.py'
            marker = home / 'ran'
            helper.write_text(f'from pathlib import Path\nPath({str(marker)!r}).touch()\n', encoding='utf-8')
            for path in [helper.name, str(helper)]:
                with self.subTest(path=path):
                    graph = delivery.Graph(home, {**CFG, 'graph_token_helper': path})
                    self.assertFalse(graph.available())
                    self.assertFalse(graph.reply_available())
                    self.assertEqual(graph.send('Report', 'body', 'abc'), ('definite', None))
                    self.assertEqual(graph.replies(OFFER), [])
            self.assertFalse(marker.exists())

    def test_credentialed_route_and_identity(self):
        provider = Mock(return_value=response({'mail': 'BOT@example.com'}))
        graph = delivery.Graph('/no', CFG)
        with provider_patch(provider):
            self.assertTrue(graph.available())
        provider.assert_called_once_with('graph', 'GET', delivery.BASE + '/me?$select=mail,userPrincipalName',
            json=None, headers={'Accept-Encoding': 'identity', 'Prefer': 'outlook.body-content-type="text", IdType="ImmutableId"'}, timeout=30)

    def test_credentialed_route_sends_json_body(self):
        provider = Mock(return_value=response(raw=b'', status=202))
        body = {'message': {'subject': 'hello'}}
        with provider_patch(provider):
            self.assertEqual(self.graph._request('POST', '/me/sendMail', body), (202, {}))
        self.assertEqual(provider.call_args.args, ('graph', 'POST', delivery.BASE + '/me/sendMail'))
        self.assertEqual(provider.call_args.kwargs['json'], body)

    def test_send_confirmation_exact_unique(self):
        self.graph._request = Mock(return_value=(202, {}))
        self.graph._messages = Mock(return_value=[SENT])
        state, sent = self.graph.send('Report', '<b>hello</b>', 'abc')
        self.assertEqual(state, 'sent')
        self.assertEqual(sent, {'mailbox': 'bot@example.com', 'id': 's1', 'conversationId': 'c1', 'sentDateTime': SENT['sentDateTime']})
        self.assertEqual(self.graph._request.call_args.args[2]['message']['subject'], SENT['subject'])
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

    def test_oversized_response_is_not_parsed(self):
        provider = Mock(return_value=response({'value': ['data' * 32]}))
        with provider_patch(provider), patch('delivery.GRAPH_RESPONSE_BYTES', 64), patch('delivery.json.loads') as loads:
            with self.assertRaises(RuntimeError):
                self.graph._request('GET', '/me/messages')
        loads.assert_not_called()

    def test_incomplete_or_encoded_response_is_not_parsed(self):
        raw = b'{"value": []}'
        cases = [
            (200, {'content-length': str(len(raw) + 1)}),
            (200, {'Content-Length': str(len(raw) - 1)}),
            (200, {'content-length': 'invalid'}),
            (206, {}),
            (200, {'cOnTeNt-RaNgE': 'bytes 0-3/10'}),
            (200, {'Content-Encoding': 'gzip'}),
        ]
        for status, headers in cases:
            with self.subTest(status=status, headers=headers):
                provider = Mock(return_value=response(raw=raw, status=status, headers=headers))
                with provider_patch(provider), patch('delivery.json.loads') as loads:
                    with self.assertRaises(RuntimeError):
                        self.graph._request('GET', '/me/messages')
                loads.assert_not_called()

    def test_complete_identity_response_is_parsed(self):
        raw = b'{"value": []}'
        provider = Mock(return_value=response(raw=raw, headers={'content-length': str(len(raw)), 'Content-Encoding': 'identity'}))
        with provider_patch(provider):
            self.assertEqual(self.graph._request('GET', '/me/messages'), (200, {'value': []}))

    def test_malformed_json_and_provider_error_propagate(self):
        provider = Mock(return_value=response(raw=b'{"value":'))
        with provider_patch(provider), self.assertRaises(json.JSONDecodeError):
            self.graph._request('GET', '/me/messages')
        provider.side_effect = RuntimeError('transport failed')
        with provider_patch(provider), self.assertRaisesRegex(RuntimeError, 'transport failed'):
            self.graph._request('GET', '/me/messages')

    def test_reply_budget_discards_partial_collection(self):
        address = {'emailAddress': {'address': 'owner@example.com'}}
        reply = {
            'id': 'r1', 'conversationId': 'c1', 'receivedDateTime': 'today',
            'from': address, 'sender': address,
            'body': {'contentType': 'text', 'content': 'approved'},
            'internetMessageHeaders': [{'name': 'In-Reply-To', 'value': ANCHOR}],
        }
        page = {'value': [reply], '@odata.nextLink': delivery.BASE + '/next'}
        budget = len(json.dumps(page).encode()) + len(json.dumps({'value': []}).encode())
        for allowance, expected in [(budget, ['approved']), (budget - 1, [])]:
            with self.subTest(allowance=allowance):
                provider = Mock(side_effect=[response({**SENT, 'internetMessageId': ANCHOR}), response(page), response({'value': []})])
                with provider_patch(provider), patch('delivery.GRAPH_LIST_BYTES', allowance):
                    self.assertEqual([r['text'] for r in self.graph.replies(OFFER)], expected)
                self.assertEqual(provider.call_count, 3)

    def test_reply_incomplete_json_discards_partial_collection(self):
        address = {'emailAddress': {'address': 'owner@example.com'}}
        reply = {'id': 'r1', 'conversationId': 'c1', 'receivedDateTime': 'today',
                 'from': address, 'sender': address,
                 'body': {'contentType': 'text', 'content': 'approved'},
                 'internetMessageHeaders': [{'name': 'In-Reply-To', 'value': ANCHOR}]}
        first = {'value': [reply], '@odata.nextLink': delivery.BASE + '/next'}
        provider = Mock(side_effect=[response({**SENT, 'internetMessageId': ANCHOR}), response(first), response(raw=b'{"value":')])
        with provider_patch(provider):
            self.assertEqual(self.graph.replies(OFFER), [])
        self.assertEqual(provider.call_count, 3)

    def test_preflight_failure_is_definite(self):
        self.graph._identity.return_value = False
        self.assertEqual(self.graph.send('Report', 'x', 'abc'), ('definite', None))
        self.assertEqual(self.graph.send('Report', 'x', 'bad\nid'), ('definite', None))


class FallbackTests(unittest.TestCase):
    @patch('delivery.smtplib.SMTP')
    def test_smtp_uses_only_the_scoped_password(self, smtp):
        smtp.return_value.send_message.return_value = {}
        cfg = {**CFG, 'smtp': {'host': 'smtp.example.com', 'username': 'bot'}, 'delivery': {'smtp': True}}
        get_secret = Mock(return_value='scoped-fixture')
        with patch.dict(sys.modules, {'agent.secret_scope': SimpleNamespace(get_secret=get_secret)}), \
                patch.dict(delivery.os.environ, {'EMAIL_PASSWORD': 'unscoped-fixture'}):
            self.assertEqual(delivery.fallback('/x', cfg, 's', '<p>a</p>', 'b'), 'smtp')
            smtp.return_value.login.assert_called_once_with('bot', 'scoped-fixture')
            get_secret.assert_called_once_with('EMAIL_PASSWORD')
            smtp.reset_mock()
            get_secret.return_value = None
            self.assertIsNone(delivery.fallback('/x', cfg, 's', '<p>a</p>', 'b'))
            smtp.assert_not_called()

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
