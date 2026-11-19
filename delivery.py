"""Delivery transports. All channels require explicit opt-in in delivery.

Settings: sender is a mailbox address; recipients is a list of addresses;
delivery.graph/smtp/profile are booleans, default false. graph_token_helper
points to a trusted Python file with a get_token() interface. Its ENV and CACHE
are scoped to the selected profile. No helper or profile fallback is used.
Mail.Send and Mail.Read are required.

SMTP settings live in smtp (host, port, security='starttls' or 'ssl', username)
and its password is EMAIL_PASSWORD in the profile .env or process environment.
profile.target is the Hermes send destination; optional profile.command is the
Hermes executable. Profile delivery uses stdin, not a local report file.
The caller must provide action-free fallback HTML and must fence ambiguous
outcomes durably before attempting another delivery.
"""
from __future__ import annotations

import html
from html.parser import HTMLParser
import importlib.util
import json
import os
from pathlib import Path
import re
import smtplib
import ssl
import subprocess
import time
from email.message import EmailMessage
from urllib import error, parse, request

BASE = 'https://graph.microsoft.com/v1.0'


def _enabled(config, key):
    return isinstance(config.get('delivery'), dict) and config['delivery'].get(key) is True


def _addresses(value):
    return isinstance(value, list) and bool(value) and all(isinstance(x, str) and x.strip() and '\n' not in x and '\r' not in x for x in value)


class Graph:
    def __init__(self, home, config):
        self.home, self.config = Path(home), config
        self.sender = config.get('sender', '')
        helper = config.get('graph_token_helper')
        self.helper = self.home / helper if isinstance(helper, str) and helper.strip() else None

    def _token(self):
        if self.helper is None or not self.helper.is_file():
            raise RuntimeError('Graph helper unavailable')
        spec = importlib.util.spec_from_file_location('_health_graph_token', self.helper)
        if spec is None or spec.loader is None:
            raise RuntimeError('Graph helper unavailable')
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        # Bind the helper to this profile before requesting a token.
        if not hasattr(mod, 'ENV') or not hasattr(mod, 'CACHE'):
            raise RuntimeError('unsupported Graph token helper')
        mod.ENV = self.home / '.env'
        mod.CACHE = self.home / 'secrets/msgraph-delegated.json'
        return mod.get_token()

    def _request(self, method, path, token, body=None):
        if not path.startswith('/') or path.startswith('//'):
            raise ValueError('invalid Graph path')
        req = request.Request(BASE + path, method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json',
                     'Prefer': 'outlook.body-content-type="text", IdType="ImmutableId"'})
        try:
            with request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                return resp.status, json.loads(raw) if raw else {}
        except error.HTTPError as exc:
            return exc.code, {}

    def _identity(self, token):
        status, obj = self._request('GET', '/me?$select=mail,userPrincipalName', token)
        return status == 200 and self.sender.lower() in {str(obj.get('mail') or '').lower(), str(obj.get('userPrincipalName') or '').lower()}

    def available(self):
        if not (_enabled(self.config, 'graph') and isinstance(self.sender, str) and self.sender and _addresses(self.config.get('recipients'))):
            return False
        try:
            return bool(self._identity(self._token()))
        except Exception:
            return False

    def reply_available(self):
        if not self.available():
            return False
        try:
            status, _ = self._request('GET', '/me/mailFolders/inbox/messages?$top=1&$select=id', self._token())
            return status == 200
        except Exception:
            return False

    def _messages(self, path, token):
        result = []
        for _ in range(20):
            status, data = self._request('GET', path, token)
            if status != 200 or not isinstance(data.get('value'), list):
                raise RuntimeError('Graph list failed')
            result.extend(data['value'])
            next_link = data.get('@odata.nextLink')
            if not next_link:
                return result
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
            token = self._token()
            if not self._identity(token):
                return 'definite', None
        except Exception:
            return 'definite', None
        msg = {'message': {'subject': tagged, 'body': {'contentType': 'HTML', 'content': html},
                           'toRecipients': [{'emailAddress': {'address': x}} for x in to]}, 'saveToSentItems': True}
        try:
            status, _ = self._request('POST', '/me/sendMail', token, msg)
        except Exception:
            return 'ambiguous', None
        # Only explicit request rejection proves the service did not accept it.
        if status != 202:
            return ('definite' if status in (400, 401, 403, 404, 405, 413, 415, 422, 429) else 'ambiguous'), None
        query = parse.urlencode({'$filter': "subject eq '" + tagged.replace("'", "''") + "'", '$select': 'subject,id,conversationId,sentDateTime,from', '$top': '100'})
        for attempt in range(4):
            try:
                messages = self._messages('/me/mailFolders/sentitems/messages?' + query, token)
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
            token = self._token()
            if not self._identity(token):
                return []
            status, sent = self._request('GET', '/me/messages/' + parse.quote(sent_id, safe='') + '?$select=id,conversationId,from,internetMessageId', token)
            addr = ((sent.get('from') or {}).get('emailAddress') or {}).get('address', '')
            anchor = sent.get('internetMessageId')
            if status != 200 or sent.get('id') != sent_id or sent.get('conversationId') != conv or addr.lower() != mailbox.lower() or not anchor:
                return []
            query = parse.urlencode({'$filter': "conversationId eq '" + conv.replace("'", "''") + "'", '$select': 'id,conversationId,from,sender,receivedDateTime,body,internetMessageHeaders', '$top': '100'})
            messages = self._messages('/me/mailFolders/inbox/messages?' + query, token)
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
        port = int(settings.get('port', 465 if security == 'ssl' else 587))
        client = (smtplib.SMTP_SSL(settings['host'], port, timeout=30, context=ssl.create_default_context())
                  if security == 'ssl' else smtplib.SMTP(settings['host'], port, timeout=30))
        try:
            if security == 'starttls': client.starttls(context=ssl.create_default_context())
            username = settings.get('username')
            if username:
                password = os.environ.get('EMAIL_PASSWORD')
                if password is None:
                    # Read only this profile's secret, without printing it.
                    from dotenv import dotenv_values
                    password = dotenv_values(Path(home) / '.env').get('EMAIL_PASSWORD')
                if not password: return None
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
