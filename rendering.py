"""Render the collected health facts as a self-contained email-safe report."""

from datetime import datetime
from html import escape
import re


SECTIONS = (
    'Backup', 'Disk', 'Plugins', 'Hermes Agent release',
    'OS and security updates', 'Memory', 'Services and other dependencies',
)
STATUS = {
    'ok': ('✅', 'OK', '#1a7f37'),
    'info': ('ℹ️', 'Info', '#0969da'),
    'warn': ('⚠️', 'Warning', '#9a6700'),
    'fail': ('❌', 'Failed', '#cf222e'),
}
RANK = {'ok': 0, 'info': 1, 'warn': 2, 'fail': 3}
MEMORY_FIELDS = ('MEMORY.md', 'USER.md', 'Memory provider')
COLOUR = {key: value[2] for key, value in STATUS.items()}
BG = {'ok': '#dafbe1', 'info': '#ddf4ff', 'warn': '#fff8c5', 'fail': '#ffebe9'}


def esc(value):
    return escape(str(value), quote=True)


def worst(rows):
    return max((row['status'] for row in rows), key=RANK.__getitem__, default='info')


def _headline(title, rows):
    if not rows:
        return 'No data available.'
    if len(rows) == 1 and rows[0]['notes'] == 'collection unavailable':
        return 'Collection unavailable.'
    if title == 'Plugins':
        outcomes = [row.get('maintenance_status') for row in rows]
        counts = (outcomes.count(state) for state in ('updated', 'failed', 'skipped'))
        sentence = '{} updated, {} failed, {} not updated.'.format(*counts)
        if not any(outcomes):
            sentence += ' No maintenance results recorded.'
        return sentence
    if title == 'Memory':
        profiles = {row['profile'] for row in rows if 'profile' in row}
        healthy = sum(row['item'] == 'Memory provider' and row['status'] == 'ok' for row in rows)
        return f'Checked {len(profiles)} profiles. Hindsight health check passed for {healthy}.'
    if title == 'Disk':
        row = next((row for row in rows if ' GB free' in row['notes']), None)
        if row:
            return f'{row["item"]}: {row["notes"]}.'
    if title == 'Services and other dependencies':
        services = [row for row in rows if row['notes'].startswith('state: ')]
        if services:
            active = sum(row['status'] == 'ok' for row in services)
            return f'{active} of {len(services)} detected or configured services active.'
        return 'No service units detected or configured. See dependency checks below.'
    if title == 'Hermes Agent release':
        row = rows[0]
        if row.get('release'):
            return f'Hermes Agent is on the {row["release"].lower()} ({row["version"]}).'
        return f'Hermes Agent: {row["version"] or "installed version unavailable"}; {row["notes"]}.'
    if title == 'OS and security updates':
        return f'APT updates: {rows[0]["notes"]}. Reboot {rows[-1]["notes"]}.'
    row = rows[0]
    return f'{row["item"]}: {row["notes"]}.'


def _profile_coverage(names, all_profiles):
    if names and names == all_profiles:
        return 'all profiles'
    missing = all_profiles - names
    if missing and len(missing) < len(names):
        return f'{len(names)} profiles (except {", ".join(sorted(missing))})'
    return ', '.join(sorted(names))


def _plugin_rows(rows, all_profiles):
    groups = {}
    for row in rows:
        groups.setdefault((row['item'], row['version']), []).append(row)
    for entries in groups.values():
        profiles = {r['profile'] for r in entries if r.get('profile')}
        notes = {}
        for row in entries:
            notes.setdefault(row['notes'], set()).add(row.get('profile', ''))
        detail = [f'{note} — {_profile_coverage(names - {""}, all_profiles)}'
                  if len(notes) > 1 and names - {''} else note for note, names in notes.items()]
        yield dict(entries[0], status=worst(entries), profile=_profile_coverage(profiles, all_profiles),
                   notes='\n'.join(detail))


def chip(status, text):
    return (f'<span style="display:inline-block;padding:2px 10px;border-radius:12px;font-size:13px;'
            f'font-weight:600;color:{COLOUR[status]};background:{BG[status]}">{STATUS[status][0]} {esc(text)}</span>')


def table(rows, head=None, widths=None):
    th = ''
    if head:
        th = '<tr>' + ''.join(
            f'<th scope="col" align="left" style="padding:6px 10px;border-bottom:2px solid #d0d7de;font-size:12px;'
            f'text-transform:uppercase;letter-spacing:.04em;color:#57606a{(";width:" + widths[i]) if widths and widths[i] else ""}">{h}</th>'
            for i, h in enumerate(head)) + '</tr>'
    body = ''.join('<tr>' + ''.join(f'<td valign="top" style="padding:7px 10px;border-bottom:1px solid #eaeef2;font-size:14px;overflow-wrap:anywhere">{c}</td>' for c in row) + '</tr>' for row in rows)
    layout = ';table-layout:fixed' if widths else ''
    return f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="border-collapse:collapse{layout}">{th}{body}</table>'


def _table(headings, rows):
    return table(rows, headings) if rows else '<p>No data available.</p>'


def small(text):
    return f'<div class="muted" style="font-size:12px;color:#6e7781;margin-top:8px">{text}</div>'


def _link(url, label):
    return f'<a href="{esc(url)}" style="color:#0969da">{esc(label)}</a>'


def bullets(items):
    return "<ul style='margin:6px 0 0 18px;padding:0;font-size:14px'>" + ''.join(f"<li style='margin:3px 0'>{item}</li>" for item in items) + '</ul>'


def section(num, title, status, headline, body):
    return (f'<div class="card" style="margin:22px 0 0;padding:16px 18px;border:1px solid #d0d7de;border-left:5px solid {COLOUR[status]};border-radius:8px;background:#ffffff">'
            f'<h2 style="font-size:17px;font-weight:700;color:#1f2328;margin:0 0 4px">{STATUS[status][0]} {num}. {esc(title)}</h2>'
            f'<div class="muted" style="font-size:14px;color:#57606a;margin-bottom:10px">{esc(headline)}</div>{body}</div>')


def _known(value):
    return 'unknown' if value is None or value == '' else esc(value)


def _properties(rows):
    return table(rows, widths=['32%', '68%'])


def _render_os(facts):
    sec, other = facts['security'], facts['other']
    apt = facts['apt']
    headline = ('APT update check: ' + apt + '.' if apt else
                f'{len(sec)} security update(s), {len(other)} other update(s). Report only.' if sec or other else
                'No security updates are waiting. Report only.')
    reboot = 'No'
    if facts['reboot']:
        reboot = chip('warn', 'Yes' + (': ' + ', '.join(facts['reboot_pkgs']) if facts['reboot_pkgs'] else ''))
    age = facts['age']
    body = _properties([
        ('System', f'{_known(facts["release"])} · kernel {_known(facts["kernel"])}'),
        ('Security updates', esc(apt) if apt else chip('warn' if sec else 'ok', len(sec))),
        ('Other updates', esc(apt) if apt else str(len(other))),
        ('Kernel update waiting', esc(apt) if apt else 'Yes' if facts['kernel_update'] else 'No'),
        ('Reboot required now', reboot),
        ('Automatic security updates', _known(facts['enabled'])),
        ('Package list age', f'{age} h' if age is not None else 'unknown'),
    ])
    if sec:
        body += small('<b>Security:</b> ' + esc(', '.join(sec)))
    if other:
        body += small('<b>Other:</b> ' + esc(', '.join(other)))
    body += small('<b>Snap refreshes waiting:</b> ' + esc(', '.join(facts['snaps']) or facts['snap_status']))
    if age is not None and age > 48:
        body += small('⚠️ The package list is more than 2 days old, so this list can be incomplete.')
    if sec or other or facts['kernel_update']:
        body += small('To install: <code>sudo apt update &amp;&amp; sudo apt full-upgrade</code>' +
                      (', then reboot.' if facts['kernel_update'] or facts['reboot'] else '.'))
    return headline, body


def _render_disk(rows):
    mounts, detail = [], ''
    headline = 'Disk usage unknown.'
    for row in rows:
        name, status = row['item'], row['status']
        if 'filesystem' in row:
            if row['filesystem'] is None:
                mounts.append([esc(name), chip(status, row['notes']), '', ''])
                continue
            pct, used, size, free = row['filesystem']
            bar = ('<div style="background:#eaeef2;border-radius:4px;height:8px;width:160px;max-width:100%;'
                   'display:inline-block;vertical-align:middle">'
                   f'<div style="background:{COLOUR[status]};height:8px;border-radius:4px;width:{pct}%"></div></div>')
            mounts.append([esc(name), f'{bar}&nbsp; <b>{pct}%</b>', f'{used} / {size} GB', f'{free} GB free'])
            if name == '/':
                headline = f'System disk is {pct}% full ({free} GB free).'
        if 'smart' in row:
            smart = row['smart']
            warning = smart.get('warning')
            health = (_known(warning) if warning else 'No critical warning' if warning is not None else 'unknown')
            detail += '<div style="height:12px"></div>'
            if smart.get('error'):
                detail += _properties([('Drive', esc(name)), ('Health', chip(status, smart['error']))])
                continue
            def measure(key, unit=''):
                return _known(smart.get(key)) + (unit if smart.get(key) is not None else '')
            detail += _properties([
                ('Drive', f'{_known(smart.get("model"))} ({esc(name)})'),
                ('Health', chip(status, health) if not warning else chip(status, warning)),
                ('Wear (rated life used)', measure('wear', '%')),
                ('Spare blocks', f'{measure("spare", "%")} (alarm at {measure("threshold", "%")})'),
                ('Media errors', measure('media_errors') + ' · ' + esc(row.get('media_change', 'no comparable previous sample'))),
                ('Temperature', measure('temp', ' &deg;C')),
                ('Power-on hours', measure('hours')),
                ('Unsafe shutdowns', measure('shutdowns')),
            ])
        if 'sizes' in row:
            if row['sizes']:
                detail += small('Largest users: ' + esc(', '.join(f'{name} {size}' + (' GB' if isinstance(size, (float, int)) else '') for name, size in row['sizes'].items())))
            usage = row['docker_usage']
            detail += small('Docker: ' + esc(usage if isinstance(usage, str) else ', '.join(
                f'{kind.lower()} {size} ({reclaimable} reclaimable)' for kind, size, reclaimable in usage) or 'No data'))
    return headline, table(mounts, head=['Mount', 'Used', 'Space', 'Free']) + detail


def _system_detail(title, rows):
    if not rows:
        return None
    first = rows[0]
    if title == 'OS and security updates' and 'os_updates' in first:
        return _render_os(first['os_updates'])
    if title == 'Disk' and any('filesystem' in row or 'smart' in row for row in rows):
        return _render_disk(rows)
    if title == 'Backup' and 'backup' in first:
        fact = first['backup']
        age = f' ({fact["age"]} h ago)' if fact['age'] is not None else ''
        body = _properties([
            ('Last run', _known(fact['finished']) + age),
            ('Result', f'{_known(fact["result"])} (exit {_known(fact["exit_status"])})'),
            ('Upload', f'{_known(fact["upload"])} &rarr; {_known(fact["target"])}'),
            ('Next run', _known(fact['next_run'])), ('Timer', _known(fact['timer'])),
        ])
        if fact['errors']:
            body += small(f'{fact["errors"]} log line(s) mention errors. Check the backup journal.')
        return first['notes'] + '.', body
    if title == 'Hermes Agent release' and 'latest' in first:
        releases_url = 'https://github.com/NousResearch/hermes-agent/releases'
        upstream = first['notes'].removeprefix('upstream: ')
        latest = first['latest']
        latest_value = esc(upstream)
        if re.fullmatch(r'v\d+\.\d+\.\d+', latest) and upstream.startswith(latest):
            latest_value = _link(f'{releases_url}/tag/{latest}', latest) + esc(upstream[len(latest):])
        installed = _known(first['version'])
        if first.get('release'):
            installed += ' · ' + esc(first['release'])
        body = _properties([
            ('Installed', installed + (f' ({esc(first["commit"])})' if first['commit'] else '')),
            (_link(releases_url, 'Latest release'), latest_value +
             (f' &middot; {esc(first["published"])}' if first['published'] else '')),
        ])
        body += '<h3 style="font-size:15px;margin:14px 0 6px">Chromium and Playwright</h3>'
        body += small('Latest Chrome stable: ' + _known(first['stable']) + ' · Latest Playwright: ' + _known(first['playwright']))
        body += table([(chip(row['status'], STATUS[row['status']][1]), esc(row['item']),
                        _known(row['version']), esc(row['notes'])) for row in rows[1:]],
                      ['Status', 'Component', 'Version', 'Notes'], ['16%', '26%', '23%', '35%'])
        return _headline(title, rows), body
    if title == 'Services and other dependencies' and any('dependency' in row for row in rows):
        return _headline(title, rows), _properties([
            (esc(row['item']), _link(row['service_url'], row['service_url']) if row.get('service_url')
             else chip(row['status'], row['notes'].removeprefix('state: ')) if row['notes'].startswith('state: ')
             else ' · '.join(esc(value) for value in (row['version'], row['notes']) if value)) for row in rows])
    return None


def _memory_cell(fact):
    notes = str(fact.get('notes', ''))
    enabled = fact.get('enabled')
    if enabled is None:
        match = re.search(r'\b(on|off|enabled|disabled)\b', notes, re.I)
        enabled = match.group(1).lower() in ('on', 'enabled') if match else None
    percent = fact.get('percent_used')
    if percent is None:
        match = re.search(r'(\d+(?:\.\d+)?)\s*%', notes)
        percent = match.group(1) if match else None
    symbol = '✅' if enabled is True else '⛔' if enabled is False else '–'
    return f'{symbol} {esc(percent)}%' if percent is not None else f'{symbol} –'


def _memory_table(rows, email):
    profiles = {}
    other = []
    vaults = []
    hindsight = []
    for row in rows:
        item = row['item']
        if row.get('vault') or row.get('vault_path'):
            if not row.get('vault_path') and row['notes'] == 'not configured':
                continue
            name = esc(item)
            if email and row.get('vault_path'):
                name += f'<br><span class="muted" style="font-size:12px;color:#6e7781">{esc(row["vault_path"])}</span>'
            count = row.get('documents')
            vaults.append((name, esc(count) if count is not None else '–',
                           esc(row.get('last_change') or row['notes'] or '–')))
        elif 'hindsight_web' in row:
            url = esc(row['hindsight_web'])
            hindsight.append(('Web console', f'<a href="{url}" style="color:#0969da;font-weight:600">{url}</a>'
                              if url else '–'))
            hindsight.append(('API', chip(row['status'], row['notes'])))
        elif row.get('profile') and item in MEMORY_FIELDS:
            profiles.setdefault(row['profile'], {})[item] = row
        else:
            other.append(row)
    body = []
    for profile, fields in profiles.items():
        cells = [esc(profile)]
        for field in MEMORY_FIELDS:
            fact = fields.get(field)
            if not fact:
                cells.append('Unavailable')
            elif field == 'Memory provider':
                symbol = STATUS[fact['status']][0]
                cells.append(f'{symbol} {esc(fact["version"])}' if fact['version'] else '–')
            else:
                cells.append(_memory_cell(fact))
        body.append(cells)
    content = _table(('Profile', 'MEMORY.md', 'USER.md', 'Provider'), body) if body else ''
    if other:
        content += _detail_table('Memory', other, email)
    if hindsight:
        content += '<h3>Hindsight memory</h3>' + _properties(hindsight)
    return content + '<h3>Vaults</h3>' + (
        table(vaults, ('Name', 'Documents', 'Last change')) if vaults else '<p>–</p>')


def _detail_table(title, rows, email):
    body = []
    for row in rows:
        symbol, text, _ = STATUS[row['status']]
        item = esc(row['item'])
        if title == 'Plugins':
            item = f'<span style="font-weight:600">{item}</span>'
        if row.get('profile') and (title == 'Plugins' or email):
            item += f'<br><span style="font-size:12px">{esc(row["profile"])}</span>'
        notes = esc(row['notes']).replace('\n', '<br>')
        if email and row.get('vault_path'):
            notes += f'<br><span style="font-size:12px">{esc(row["vault_path"])}</span>'
        version = esc(row['version'])
        if title == 'Plugins':
            label = esc(row['version'][:12]) + '…' if len(row['version']) > 16 else version
            version = f'<div title="{version}" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:100%">{label}</div>'
        body.append((chip(row['status'], text), item, version, notes))
    if title == 'Plugins' and body:
        return table(body, ('Result', 'Plugin', 'Version', 'Notes'), ('17%', '23%', '18%', ''))
    return _table(('Result', 'Item', 'Version', 'Notes'), body)


def _cta_block(actions):
    actions = [action for action in actions if isinstance(action, str) and action.strip()]
    if not actions:
        return ('<div class="card" style="margin:22px 0 0;padding:16px 18px;border:1px solid #d0d7de;'
                'border-radius:8px;background:#ffffff;font-size:14px">✅&nbsp; <b>Nothing for you to approve in this report.</b></div>')
    items = [(f'<b style="color:rgb(22,30,63)">{i}</b>', esc(action)) for i, action in enumerate(actions, 1)]
    return ('<div class="card" style="margin:22px 0 0;padding:16px 18px;border:2px solid rgb(22,30,63);border-radius:8px;background:#f5f7ff">'
            '<h2 style="font-size:17px;color:rgb(22,30,63);margin:0 0 4px">&#9881;&#65039;&nbsp; What I can do for you</h2>'
            '<div class="muted" style="font-size:14px;color:#57606a;margin-bottom:10px">'
            'The maintenance worker runs only the actions you approve. Linux and Hermes Agent updates stay with you.</div>'
            + table(items, widths=['6%', '94%']) +
            '<div style="margin-top:12px;font-size:14px">Reply <b>approved</b> to approve all actions. '
            'To approve only some, reply <b>approved</b> followed by their numbers.</div></div>')


def render(instance, hostname, sections, actions=(), report_id='', email=False, kind='health') -> str:
    """Render a report. Callers control whether an approval offer is supplied."""
    if kind not in ('health', 'maintenance'):
        raise ValueError('kind must be health or maintenance')
    unknown = set(sections) - set(SECTIONS)
    if unknown:
        raise ValueError('Unknown report sections: ' + ', '.join(sorted(unknown)))
    for rows in sections.values():
        for row in rows:
            if row['status'] not in STATUS:
                raise ValueError('Unknown status: ' + str(row['status']))

    summary = []
    blocks = []
    affected = []
    statuses = []
    all_profiles = {row['profile'] for row in sections.get('Memory', [])
                    if row['item'] in MEMORY_FIELDS and row.get('profile')}
    for number, title in enumerate(SECTIONS, 1):
        rows = sections.get(title, [])
        status = worst(rows)
        icon = STATUS[status][0]
        statuses.append(status)
        if RANK[status] >= RANK['warn']:
            affected.append(title)
        detail = _system_detail(title, rows) if title not in ('Memory', 'Plugins') else None
        headline = detail[0] if detail is not None else _headline(title, rows)
        summary.append(f'<tr><td valign="top" style="padding:7px 10px;border-bottom:1px solid #eaeef2;font-size:14px">{icon}</td>'
                       f'<td valign="top" style="padding:7px 10px;border-bottom:1px solid #eaeef2;font-size:14px"><b>{esc(title)}</b></td>'
                       f'<td valign="top" style="padding:7px 10px;border-bottom:1px solid #eaeef2;font-size:14px">{esc(headline)}</td></tr>')
        if title == 'Memory':
            body = _memory_table(rows, email)
        elif detail is not None:
            body = detail[1]
        else:
            body = _detail_table(title, _plugin_rows(rows, all_profiles) if title == 'Plugins' else rows, email)
        blocks.append(section(number, title, status, headline, body))
    overall = max(statuses, key=RANK.__getitem__)
    verdict = {'ok': 'All good. Nothing needs your attention.',
               'info': 'All good. Some items are for your information.',
               'warn': 'Mostly good. Some items need a decision from you.',
               'fail': 'Action needed. Something failed.'}[overall]
    if affected:
        verdict += ' See: ' + ', '.join(affected) + '.'
    banner = (f'<div class="verdict {overall}" style="margin-top:16px;padding:14px 18px;border-radius:8px;'
              f'background:{BG[overall]};color:{COLOUR[overall]};font-size:16px;font-weight:700">'
              f'{STATUS[overall][0]}&nbsp; {esc(verdict)}</div>')
    approval = _cta_block(actions)
    now = datetime.now().astimezone()
    date = now.strftime('%a %d %b %Y')
    zone = now.tzname() or now.strftime('UTC%z')
    lead = ' · Maintenance results' if kind == 'maintenance' else ''
    return (f'<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="color-scheme" content="light dark">'
            '<meta name="supported-color-schemes" content="light dark">'
            '<style>@media (prefers-color-scheme:dark){body,.page{background:#101522!important;color:#f0f2f7!important}'
            '.card{background:#202838!important;color:#f0f2f7!important}'
            '.card h2,.card h3,.card th,.muted,.footer{color:#ccd5e3!important}'
            '.verdict.fail{background:#532428!important;color:#ffffff!important}}</style></head>'
            '<body style="margin:0;background:#f6f8fa;color:#1f2328;font-family:Segoe UI,-apple-system,Helvetica,Arial,sans-serif">'
            '<div class="page" style="max-width:760px;margin:0 auto;padding:24px 16px">'
            '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" '
            'style="background:rgb(22,30,63);color:#ffffff;border-radius:10px"><tr><td style="padding:20px 22px">'
            f'<div style="font-size:13px;letter-spacing:.08em;text-transform:uppercase;color:#aab4c0">{esc(instance)}</div>'
            '<h1 style="font-size:24px;margin:4px 0 0">Hermes health report</h1>'
            f'<div style="font-size:14px;color:#c9d1d9;margin-top:2px">{date}{lead}</div></td>'
            f'<td align="right" valign="top" style="padding:20px 22px;font-size:13px;color:#c9d1d9">{esc(hostname)}</td></tr></table>'
            f'{banner}<div class="card" style="margin-top:16px;padding:6px 8px;background:#ffffff;color:#1f2328;'
            'border:1px solid #d0d7de;border-radius:8px"><table aria-label="Summary" role="presentation" width="100%" cellspacing="0" cellpadding="0" '
            'style="border-collapse:collapse">' + ''.join(summary) + '</table></div>' + ''.join(blocks) + approval +
            '<div class="footer" style="margin-top:22px;font-size:12px;color:#6e7781;line-height:1.6">✅ good &nbsp; ℹ️ for your information &nbsp; ⚠️ needs a decision &nbsp; ❌ failed or broken'
            '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" '
            'style="font-size:12px;color:inherit;line-height:1.6"><tr>'
            f'<td>Report ID: {esc(report_id)}</td><td align="right">Dates and times in {esc(zone)}</td>'
            '</tr></table>Hermes health report.</div></div></body></html>')
