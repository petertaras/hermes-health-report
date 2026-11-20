"""Delivery transports. All channels require explicit opt-in in delivery.

Settings: sender is a mailbox address; recipients is a list of addresses;
delivery.graph/smtp/profile are booleans, default false. Graph delivery and
reply polling use the active profile's Hermes OAuth sign-in.

SMTP settings live in smtp (host, port, security='starttls' or 'ssl', username)
and its password is EMAIL_PASSWORD from the active Hermes secret scope.
profile.target is the Hermes send destination; optional profile.command is the
Hermes executable. Profile delivery uses stdin, not a local report file.
The caller must provide action-free fallback HTML and must fence ambiguous
outcomes durably before attempting another delivery.
"""
from __future__ import annotations

import html
from html.parser import HTMLParser
import json
import os
import re
import smtplib
import ssl
import subprocess
import time
from email.message import EmailMessage
from urllib import parse

BASE = 'https://graph.microsoft.com/v1.0'
GRAPH_RESPONSE_BYTES = 1024 * 1024
GRAPH_LIST_BYTES = 4 * 1024 * 1024


def _enabled(config, key):
    return isinstance(config.get('delivery'), dict) and config['delivery'].get(key) is True


def _addresses(value):
    return isinstance(value, list) and bool(value) and all(isinstance(x, str) and x.strip() and '\n' not in x and '\r' not in x for x in value)


class Graph:
    def __init__(self, home, config):
        self.config = config
        self.sender = config.get('sender', '')

    def _request(self, method, path, body=None):
        status, raw = self._request_bytes(method, path, body)
        return status, json.loads(raw) if raw and status == 200 else {}

    def _request_bytes(self, method, path, body=None, *, remaining=None):
        from hermes_cli.plugin_provider_requests import credentialed_provider_request

        if not path.startswith('/') or path.startswith('//'):
            raise ValueError('invalid Graph path')
        limit = GRAPH_RESPONSE_BYTES if remaining is None else min(GRAPH_RESPONSE_BYTES, remaining)
        resp = credentialed_provider_request(
            'graph', method, BASE + path, json=body, timeout=30,
            headers={'Accept-Encoding': 'identity',
                     'Prefer': 'outlook.body-content-type="text", IdType="ImmutableId"'},
        )
        # Hermes has already read the full body. Never parse a partial result.
        raw = resp.body
        if len(raw) > limit:
            raise RuntimeError('Graph response byte limit exceeded')
        headers = {name.lower(): value.strip() for name, value in resp.headers.items()}
        if resp.status == 206 or 'content-range' in headers:
            raise RuntimeError('incomplete Graph response')
        if headers.get('content-encoding', 'identity').lower() != 'identity':
            raise RuntimeError('unsupported Graph response encoding')
        length = headers.get('content-length')
        if length is not None and (not re.fullmatch(r'[0-9]+', length) or int(length) != len(raw)):
            raise RuntimeError('incomplete Graph response')
        return resp.status, raw

    def _identity(self):
        status, obj = self._request('GET', '/me?$select=mail,userPrincipalName')
        return status == 200 and self.sender.lower() in {str(obj.get('mail') or '').lower(), str(obj.get('userPrincipalName') or '').lower()}

    def available(self):
        if not (_enabled(self.config, 'graph') and isinstance(self.sender, str) and self.sender and _addresses(self.config.get('recipients'))):
            return False
        try:
            return bool(self._identity())
        except Exception:
            return False

    def reply_available(self):
        if not self.available():
            return False
        try:
            status, data = self._request('GET', '/me/mailFolders/inbox/messages?$top=1&$select=id')
            return status == 200 and isinstance(data, dict) and isinstance(data.get('value'), list)
        except Exception:
            return False

    def _messages(self, path):
        result = []
        remaining = GRAPH_LIST_BYTES
        for _ in range(20):
            if remaining == 0:
                raise RuntimeError('Graph list byte limit reached')
            status, raw = self._request_bytes('GET', path, remaining=remaining)
            if status != 200:
                raise RuntimeError('Graph list failed')
            remaining -= len(raw)
            data = json.loads(raw)
            if not isinstance(data, dict) or not isinstance(data.get('value'), list):
                raise RuntimeError('Graph list failed')
            result.extend(data['value'])
            if '@odata.nextLink' not in data:
                return result
            next_link = data['@odata.nextLink']
            if not isinstance(next_link, str) or not next_link.startswith(BASE + '/'):
                raise RuntimeError('invalid Graph pagination')
            path = next_link[len(BASE):]
        raise RuntimeError('Graph pagination limit')

    def send(self, subject, html, report_id, recipients=None):
        to = self.config.get('recipients') if recipients is None else recipients
        if not (_enabled(self.config, 'graph') and isinstance(self.sender, str) and self.sender and _addresses(to) and isinstance(report_id, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', report_id) and isinstance(subject, str) and '\n' not in subject and '\r' not in subject):
            return 'definite', None
        # A unique subject equality avoids substring, prefix and ordering matches.
        tagged = f'{subject} [health-report:{report_id}]'
        try:
            if not self._identity():
                return 'definite', None
        except Exception:
            return 'definite', None
        msg = {'message': {'subject': tagged, 'body': {'contentType': 'HTML', 'content': html},
                           'toRecipients': [{'emailAddress': {'address': x}} for x in to]}, 'saveToSentItems': True}
        try:
            status, _ = self._request('POST', '/me/sendMail', msg)
        except Exception:
            return 'ambiguous', None
        # Only explicit request rejection proves the service did not accept it.
        if status != 202:
            return ('definite' if status in (400, 401, 403, 404, 405, 413, 415, 422, 429) else 'ambiguous'), None
        query = parse.urlencode({'$filter': "subject eq '" + tagged.replace("'", "''") + "'", '$select': 'subject,id,conversationId,sentDateTime,from', '$top': '100'})
        for attempt in range(4):
            try:
                messages = self._messages('/me/mailFolders/sentitems/messages?' + query)
                matches = [m for m in messages if m.get('subject') == tagged]
                if len(matches) > 1:
                    return 'ambiguous', None
                if matches:
                    m = matches[0]
                    address = ((m.get('from') or {}).get('emailAddress') or {}).get('address', '')
                    if all(m.get(k) for k in ('id', 'conversationId', 'sentDateTime')) and address.lower() == self.sender.lower():
                        return 'sent', {'mailbox': self.sender, **{k: m[k] for k in ('id', 'conversationId', 'sentDateTime')}}
                    return 'ambiguous', None
            except Exception:
                return 'ambiguous', None
            if attempt < 3:
                time.sleep(2)
        return 'ambiguous', None

    def replies(self, offer):
        # Verify the stored sent message again, not merely a supplied thread ID.
        mailbox, sent_id, conv = (offer.get(k) for k in ('mailbox', 'id', 'conversationId'))
        if not (_enabled(self.config, 'graph') and mailbox and sent_id and conv and mailbox.lower() == str(self.sender).lower()):
            return []
        try:
            if not self._identity():
                return []
            status, sent = self._request('GET', '/me/messages/' + parse.quote(sent_id, safe='') + '?$select=id,conversationId,from,internetMessageId')
            addr = ((sent.get('from') or {}).get('emailAddress') or {}).get('address', '')
            anchor = sent.get('internetMessageId')
            if status != 200 or sent.get('id') != sent_id or sent.get('conversationId') != conv or addr.lower() != mailbox.lower() or not anchor:
                return []
            query = parse.urlencode({'$filter': "conversationId eq '" + conv.replace("'", "''") + "'", '$select': 'id,conversationId,from,sender,receivedDateTime,body,internetMessageHeaders', '$top': '100'})
            messages = self._messages('/me/mailFolders/inbox/messages?' + query)
            replies = []
            for m in messages:
                sender = ((m.get('from') or {}).get('emailAddress') or {}).get('address', '')
                actual_sender = ((m.get('sender') or {}).get('emailAddress') or {}).get('address', '')
                body = m.get('body') or {}
                parents = [str(h.get('value', '')).strip() for h in m.get('internetMessageHeaders', [])
                           if str(h.get('name', '')).lower() == 'in-reply-to']
                # Address consistency is not owner authentication. See docs/approvals.md.
                if parents != [anchor] or actual_sender.lower() != sender.lower():
                    continue
                if m.get('conversationId') != conv or not all(m.get(k) for k in ('id', 'receivedDateTime')) or not sender or sender.lower() == mailbox.lower() or body.get('contentType', '').lower() != 'text' or not isinstance(body.get('content'), str):
                    continue
                replies.append({'sender': sender, 'id': m['id'], 'conversationId': conv, 'receivedDateTime': m['receivedDateTime'], 'text': body['content'], 'mailbox': mailbox})
            return replies
        except Exception:
            return []


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(); self.parts = []
    def handle_data(self, data):
        self.parts.append(data)
    def handle_starttag(self, tag, attrs):
        if tag in ('p', 'br', 'div', 'tr', 'li'):
            self.parts.append('\n')


def _plain(content):
    parser = _Text(); parser.feed(content)
    return ''.join(parser.parts).strip()


def _smtp(home, config, subject, content):
    settings = config.get('smtp') or {}
    sender, recipients = config.get('sender'), config.get('recipients')
    if not isinstance(settings, dict) or not settings.get('host') or not sender or not _addresses(recipients):
        return None
    try:
        msg = EmailMessage(); msg['From'] = sender; msg['To'] = ', '.join(recipients); msg['Subject'] = subject
        msg.set_content(_plain(content)); msg.add_alternative(content, subtype='html')
        security = settings.get('security', 'starttls')
        if security not in ('ssl', 'starttls'):
            return None
        username = settings.get('username')
        password = None
        if username:
            from agent.secret_scope import get_secret
            password = get_secret('EMAIL_PASSWORD')
            if not password:
                return None
        port = int(settings.get('port', 465 if security == 'ssl' else 587))
        client = (smtplib.SMTP_SSL(settings['host'], port, timeout=30, context=ssl.create_default_context())
                  if security == 'ssl' else smtplib.SMTP(settings['host'], port, timeout=30))
        try:
            if security == 'starttls': client.starttls(context=ssl.create_default_context())
            if username and password:
                client.login(username, password)
            try:
                refused = client.send_message(msg)
                return 'ambiguous' if refused else 'smtp'
            except smtplib.SMTPRecipientsRefused:
                return None
            except Exception:
                return 'ambiguous'
        finally:
            try: client.quit()
            except Exception: pass
    except Exception:
        return None


def fallback(home, config, subject, email_html, redacted_html):
    if _enabled(config, 'smtp'):
        result = _smtp(home, config, subject, email_html)
        if result: return result
    if _enabled(config, 'profile'):
        settings = config.get('profile') or {}
        if not isinstance(settings, dict) or not settings.get('target'):
            return None
        env = os.environ.copy(); env['HERMES_HOME'] = str(home)
        try:
            proc = subprocess.run([settings.get('command', 'hermes'), 'send', '--to', settings['target'], '--file', '-', '--json'], input=subject + '\n\n' + _plain(redacted_html), text=True, capture_output=True, timeout=60, env=env)
        except FileNotFoundError:
            return None
        except Exception:
            return 'ambiguous'
        try:
            data = json.loads(proc.stdout)
            if proc.returncode == 0 and data.get('success') is True and not data.get('skipped'):
                return 'profile'
        except (ValueError, AttributeError):
            pass
        return 'ambiguous'
    return None
