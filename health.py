"""Profile-local report, approval and scheduler commands."""
from __future__ import annotations

import argparse
import fcntl
import getpass
import json
import os
from pathlib import Path

import socket
import sys
from uuid import uuid4

import yaml

from . import approval, delivery, maintenance, probes, rendering

NAME = 'hermes-health-report'


def read_config(home):
    try:
        data = yaml.safe_load((home / NAME / 'config.yaml').read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {}
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError('config.yaml must contain a mapping')
    return data


def state_dir(home):
    root = home / NAME
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return root


def write_html(path, content):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as output:
        output.write(content)


def report(home, config, kind, dry_run=False, output=None, *, preview_email=False):
    if preview_email and not dry_run:
        raise ValueError('--preview-email requires --dry-run')
    root = state_dir(home)
    report_id = uuid4().hex
    title = 'Hermes health report' if kind == 'health' else 'Hermes maintenance report'
    sections = probes.collect(home, config)
    history = root / 'nvme-media-errors.json'
    try:
        previous = json.loads(history.read_text(encoding='utf-8'))
        if not isinstance(previous, dict):
            previous = {}
    except (OSError, ValueError):
        previous = {}
    current = {}
    for row in sections.get('Disk', []):
        if 'media_errors' in row:
            name, count = row['item'], row['media_errors']
            current[name] = count
            old = previous.get(name)
            row['media_change'] = (f'change since previous report: +{count - old}'
                                   if type(old) is int and old <= count else 'no comparable previous sample')
            if type(old) is int and count > old and row['status'] != 'fail':
                row['status'] = 'warn'

    def document(email=False, actions=()):
        return rendering.render(config.get('instance_name') or getpass.getuser(), socket.gethostname(),
                                sections, actions, report_id, email, kind)

    if dry_run:
        output = output or root / f'{kind}-preview-{report_id}.html'
        write_html(output, document(email=preview_email))
        return str(output)
    with (root / 'delivery.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        fence = root / 'delivery-pending.json'
        if fence.exists():
            raise ValueError('Delivery outcome unresolved. Check delivery-pending.json and Sent Items.')
        graph = delivery.Graph(home, config)
        store = None
        offered = {}
        if kind == 'health' and graph.available():
            offered = maintenance.offer_actions(home, config, graph)
            if offered:
                store = approval.Store(root / 'approvals.sqlite3')
        # Write before the first external attempt. A crash must not cause a duplicate send.
        fence.write_text(json.dumps({'report_id': report_id, 'subject': title}), encoding='utf-8')
        state, sent = graph.send(title, document(True, list(offered.values())), report_id)
        if state == 'sent':
            if store and sent:
                store.activate(sent, report_id, list(offered), plan=maintenance.plan(home, config))
            result = 'graph'
        elif state == 'ambiguous':
            raise ValueError('Graph send is unresolved. No fallback or offer was activated.')
        else:
            result = delivery.fallback(home, config, title, document(True), document())
            if result == 'ambiguous':
                raise ValueError('Fallback send is unresolved. No further delivery was attempted.')
            if not result:
                output = output or root / f'{kind}-{report_id}.html'
                write_html(output, document())
                result = str(output)
        fence.unlink()
        if current:
            temp = history.with_suffix('.tmp')
            temp.write_text(json.dumps(current), encoding='utf-8')
            temp.replace(history)
        return result


def status(home, config):
    graph = delivery.Graph(home, config)
    return {'graph': graph.available(), 'graph_reply': graph.reply_available(),
            'graph_unavailable_reason': delivery.GRAPH_UNAVAILABLE_REASON,
            'kanban': maintenance.available(home, config), 'backup_configured': bool(config.get('backup_unit')),
            'scheduler': config.get('scheduler') is True, 'approvals': config.get('approvals') is True,
            'delivery_pending': (home / NAME / 'delivery-pending.json').exists()}


def setup(home, config):
    from cron import jobs
    scripts = home / 'scripts'
    scripts.mkdir(mode=0o700, exist_ok=True)
    jobs_by_name = {job['name']: job for job in jobs.list_jobs(include_disabled=True)}
    for action, schedule, enabled, no_agent in (
        ('scheduled-maintenance', '30 7 * * 6', config.get('maintenance') is True, False),
        ('report', '30 8 * * 6', True, True),
        ('poll', '*/15 * * * *', config.get('approvals') is True, True),
    ):
        filename = f'{NAME}-{action}.py'
        script = scripts / filename

        # Resolve the installed plugin relative to the wrapper, not the caller's cwd.
        wrapper = ('import runpy\nimport sys\n'
                   f'sys.argv[1:] = [{action!r}]\n'
                   f'runpy.run_path({str(home / "plugins" / NAME / "run.py")!r}, run_name="__main__")\n')
        if script.exists() and script.read_text(encoding='utf-8') != wrapper:
            raise ValueError(f'Refusing to replace existing wrapper: {filename}')
        script.write_text(wrapper, encoding='utf-8')
        job_name = f'{NAME}:{action}'
        live = enabled and config.get('scheduler') is True
        job = jobs_by_name.get(job_name)
        if job and job.get('script') != filename:
            raise ValueError(f'Refusing to reuse unrelated job: {job_name}')
        if not job:
            jobs.create_job('Read the script output. Do not run additional commands.' if not no_agent else None,
                            schedule, name=job_name, deliver='local', script=filename,
                            no_agent=no_agent, paused=not live, catch_up=False)
        elif live and not job.get('enabled'):
            jobs.resume_job(job['id'])
        elif not live and job.get('enabled'):
            jobs.pause_job(job['id'], reason='Disabled in health report config')


def setup_args(parser):
    sub = parser.add_subparsers(dest='health_command', required=True)
    report_parser = sub.add_parser('report', help='Build and deliver a report')
    report_parser.add_argument('--dry-run', action='store_true')
    report_parser.add_argument('--preview-email', action='store_true', help='Include email-only details in a dry run')
    report_parser.add_argument('--output', type=Path)
    report_parser.add_argument('--kind', choices=('health', 'maintenance'), default='health')
    for cmd in ('status', 'setup', 'poll', 'scheduled-maintenance'):
        sub.add_parser(cmd)
    sub.add_parser('update-plugins').add_argument('--card', required=True)


def handler(args):
    from hermes_constants import get_hermes_home
    home = get_hermes_home()
    try:
        config = read_config(home)
        cmd = args.health_command
        if cmd == 'report':
            print(report(home, config, args.kind, args.dry_run, args.output, preview_email=args.preview_email))
        elif cmd == 'status':
            print(json.dumps(status(home, config), indent=2))
        elif cmd == 'setup':
            setup(home, config)
        elif cmd == 'poll':
            maintenance.poll(home, config)
        elif cmd == 'scheduled-maintenance':
            maintenance.scheduled(home, config)
        elif cmd == 'update-plugins':
            maintenance.update(home, config, args.card)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f'Health report: {exc}', file=sys.stderr)
        return 1
    return 0


def main():
    parser = argparse.ArgumentParser(prog=NAME)
    setup_args(parser)
    return handler(parser.parse_args())
