"""Best-effort, read-only health facts. No repair or update commands are run."""
from __future__ import annotations

import datetime as dt
from http.client import HTTPException
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from urllib.parse import urlsplit

import yaml

SECTIONS = ['Backup', 'Disk', 'Plugins', 'Hermes Agent release',
            'OS and security updates', 'Memory', 'Services and other dependencies']


def _row(status, item, version='', notes='', **extra):
    return dict(status=status, item=str(item), version=str(version), notes=str(notes), **extra)


def _run(argv, timeout=10):
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                                stdin=subprocess.DEVNULL, env={**os.environ, 'GIT_OPTIONAL_LOCKS': '0', 'LC_ALL': 'C'})
        return result.returncode, result.stdout.strip()
    except FileNotFoundError:
        return 127, ''
    except (OSError, subprocess.SubprocessError):
        return 1, ''


class _NoReleaseRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward the API token to a redirect target.


def _latest(repo, with_date=False):
    endpoint = f'repos/{repo}/releases/latest'

    def release(tag, detail, published=''):
        return (tag, detail, published[:10] if isinstance(published, str) else '') if with_date else (tag, detail)
    token = os.environ.get('GITHUB_TOKEN', '').strip()
    if not token:
        rc, tag = _run(['gh', 'api', '--hostname', 'github.com', endpoint, '--jq', '[.tag_name, .published_at] | @tsv' if with_date else '.tag_name'], timeout=5)
        tag, _, date = tag.partition('\t')
        if not rc and re.fullmatch(r'v?[0-9][0-9A-Za-z.+_-]*', tag):
            return release(tag, 'via GitHub CLI', date)

    headers = {'User-Agent': 'hermes-health-report', 'Accept': 'application/vnd.github+json'}
    if token:
        headers['Authorization'] = f'Bearer {token}'
    try:
        request = urllib.request.Request(f'https://api.github.com/{endpoint}', headers=headers)
        open_url = urllib.request.build_opener(_NoReleaseRedirect()).open if token else urllib.request.urlopen
        with open_url(request, timeout=5) as response:
            data = json.loads(response.read(100000))
            tag = data.get('tag_name', '')
        if isinstance(tag, str) and re.fullmatch(r'v?[0-9][0-9A-Za-z.+_-]*', tag):
            return release(tag, 'via authenticated GitHub API' if token else 'via public GitHub API', data.get('published_at'))
        return release('', 'GitHub response has no valid release tag')
    except urllib.error.HTTPError as error:
        code, headers = error.code, error.headers
        error.close()
        if code == 429 or (code == 403 and (headers.get('X-RateLimit-Remaining') == '0' or headers.get('Retry-After'))):
            scope = 'authenticated' if token else 'unauthenticated'
            return release('', f'{scope} GitHub API rate limit reached (HTTP {code})')
        if code == 401 and token:
            return release('', 'GitHub rejected GITHUB_TOKEN (HTTP 401)')
        return release('', f'GitHub API returned HTTP {code}')
    except (TimeoutError, OSError, HTTPException):
        return release('', 'GitHub API connection failed or timed out')
    except (ValueError, AttributeError):
        return release('', 'invalid GitHub API response')


def _release_label(tag, detail):
    return f'{tag or "unavailable"} ({detail})'


def _yaml(path):
    try:
        with path.open(encoding='utf-8') as f:
            value = yaml.safe_load(f)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, yaml.YAMLError):
        return {}


def _root(home):
    home = Path(home).expanduser()
    return home.parent.parent if home.parent.name == 'profiles' else home


def _profiles(root):
    try:
        return [('main', root)] + [
            (p.name, p) for p in sorted((root / 'profiles').iterdir())
            if not p.name.startswith('.') and p.name not in ('default', 'main') and p.is_dir()
        ]
    except OSError:
        return [('main', root)]


UNAVAILABLE = 'not available on this system'


def _command_note(rc):
    return UNAVAILABLE if rc == 127 else 'check unavailable'


def _text(path):
    try:
        return Path(path).read_text(encoding='utf-8', errors='replace').strip()
    except OSError:
        return ''


def _backup(config):
    unit = config.get('backup_unit')
    if not isinstance(unit, str) or not unit:
        return [_row('info', 'Backup', notes='not configured')]
    unit = unit if unit.endswith('.service') else unit + '.service'
    rc, out = _run(['systemctl', '--user', 'show', unit, '-p', 'LoadState,Result,ExecMainStatus,ExecMainExitTimestamp'])
    if rc:
        return [_row('info' if rc == 127 else 'warn', 'Backup', notes=_command_note(rc))]
    props = dict(line.split('=', 1) for line in out.splitlines() if '=' in line)
    if props.get('LoadState') == 'not-found':
        return [_row('info', 'Backup', notes='not configured')]
    finished = props.get('ExecMainExitTimestamp') or 'unknown'
    age = None
    since = None
    try:
        fin = dt.datetime.strptime(' '.join(finished.split()[1:3]), '%Y-%m-%d %H:%M:%S').astimezone()
        age = round((dt.datetime.now().astimezone() - fin).total_seconds() / 3600, 1)
        since = int(fin.timestamp()) - 3600
    except ValueError:
        pass
    trc, timer = _run(['systemctl', '--user', 'show', unit.removesuffix('.service') + '.timer', '-p', 'NextElapseUSecRealtime,ActiveState'])
    tp = dict(line.split('=', 1) for line in timer.splitlines() if '=' in line)
    args = ['journalctl', '--user', '-u', unit, '-n', '100', '--no-pager', '-o', 'cat']
    if since is not None:
        args += ['--since', f'@{since}']
    jrc, journal = _run(args)
    # Only extract the numeric size, never render arbitrary journal text.
    uploads = re.findall(r'upload verified: (\d+) bytes', journal, re.I) if not jrc else []
    errors = sum(bool(re.search(r'\b(error|fail|denied)\b', line, re.I)) for line in journal.splitlines()) if not jrc else 0
    result, exit_status = props.get('Result'), props.get('ExecMainStatus')
    status = 'ok' if result == 'success' and exit_status == '0' and uploads and age is not None and age < 30 and not errors else 'warn'
    if (result and result != 'success') or (exit_status and exit_status != '0') or (age is not None and age >= 30):
        status = 'fail'
    return [_row(status, unit, notes=('Last backup: ' + finished if status == 'ok' else 'Check the last backup and upload below'),
                 backup=dict(finished=finished, age=age, result=result, exit_status=exit_status,
                             upload=f'{int(uploads[-1]) / 1e9:.2f} GB verified' if uploads else _command_note(jrc) if jrc else 'no upload confirmed',
                             target=config.get('backup_target'), errors=errors,
                             next_run=tp.get('NextElapseUSecRealtime') or (_command_note(trc) if trc else 'unknown'),
                             timer=tp.get('ActiveState') or (_command_note(trc) if trc else 'unknown')))]


def _smart(device):
    rc, out = _run(['smartctl', '-a', '-j', str(device)], timeout=15)
    try:
        data = json.loads(out)
        log = data['nvme_smart_health_information_log']
        smart = {key: log.get(value) for key, value in {
            'wear': 'percentage_used', 'spare': 'available_spare', 'threshold': 'available_spare_threshold',
            'media_errors': 'media_errors', 'temp': 'temperature', 'hours': 'power_on_hours',
            'shutdowns': 'unsafe_shutdowns', 'warning': 'critical_warning'}.items()}
        return dict(smart, model=data.get('model_name'))
    except (ValueError, KeyError, AttributeError, TypeError):
        pass
    # UDisks is the legacy non-root path when smartctl lacks device access.
    urc, info = _run(['udisksctl', 'info', '-b', str(device)])
    match = re.search(r"Drive:\s+'([^']+)'", info)
    if not match:
        return {'error': UNAVAILABLE if rc == urc == 127 else 'SMART check unavailable'}
    obj = match[1]
    _, drive = _run(['udisksctl', 'info', '-d', obj.rsplit('/', 1)[-1]])
    model = re.search(r'^\s+Model:\s+(.+)$', drive, re.M)
    base = ['gdbus', 'call', '--system', '--dest', 'org.freedesktop.UDisks2', '--object-path', obj, '--method']
    interface = 'org.freedesktop.UDisks2.NVMe.Controller'
    grc, raw = _run([*base, interface + '.SmartGetAttributes', '{}'])
    if grc:
        return {'error': _command_note(grc)}
    _, props = _run([*base, 'org.freedesktop.DBus.Properties.GetAll', interface])
    smart = {'model': model[1].strip() if model else None}
    for key, name in {'wear': 'percent_used', 'spare': 'avail_spare', 'threshold': 'spare_thresh',
                      'media_errors': 'media_errors', 'shutdowns': 'unsafe_shutdowns', 'hours': 'SmartPowerOnHours'}.items():
        match = re.search(rf"'{name}': <(?:byte|uint\d+) (0x[0-9a-fA-F]+|\d+)>", raw + props)
        if match:
            smart[key] = int(match[1], 16 if match[1].startswith('0x') else 10)
    temp = re.search(r"'temp_sensors': <\[uint16 (\d+)", raw)
    health = re.search(r"'SmartCriticalWarning': <(?:@as )?\[(.*?)\]>", props)
    smart['temp'] = int(temp[1]) - 273 if temp and int(temp[1]) else None
    smart['warning'] = health[1].replace("'", '') if health else None
    return smart


def _disk(config):
    rows = []
    mounts = config.get('disk_mounts', ['/', '/boot/efi'] if Path('/boot/efi').is_mount() else ['/'])
    for mount in mounts:
        try:
            usage = shutil.disk_usage(mount)
            pct = round(100 * usage.used / usage.total, 1)
            free = round(usage.free / 1e9, 1)
            rows.append(_row('fail' if pct >= 90 else 'warn' if pct >= 80 else 'ok', mount,
                             notes=f'{pct:.1f}% used; {free:.1f} GB free',
                             filesystem=(pct, round(usage.used / 1e9, 1), round(usage.total / 1e9, 1), free)))
        except (OSError, TypeError, ZeroDivisionError):
            rows.append(_row('warn', str(mount), notes='disk usage unavailable', filesystem=None))
    devices = sorted(d for d in Path('/dev').glob('nvme*n*') if re.fullmatch(r'nvme\d+n\d+', d.name))
    for device in devices:
        smart = _smart(device)
        spare, threshold = smart.get('spare'), smart.get('threshold')
        status = ('fail' if smart.get('warning') or (spare is not None and threshold is not None and spare <= threshold)
                  else 'warn' if (smart.get('wear') or 0) >= 80 or (smart.get('temp') or 0) >= 70
                  else 'info' if smart.get('warning') is None else 'ok')
        extra = {'media_errors': smart['media_errors']} if type(smart.get('media_errors')) is int else {}
        rows.append(_row(status, device, notes=smart.get('error', 'SMART'), smart=smart, **extra))
    if not devices:
        rows.append(_row('info', 'NVMe SMART', notes=UNAVAILABLE, smart={'error': UNAVAILABLE}))
    sizes = {}
    configured_paths = config.get('disk_size_paths')
    for label in configured_paths if isinstance(configured_paths, list) else []:
        if not isinstance(label, str) or not label:
            continue
        try:
            path = Path(label).expanduser()
        except (RuntimeError, ValueError):
            continue
        if path.is_dir():
            rc, out = _run(['du', '-sb', '--', str(path)], timeout=15)
            if not rc and out.split() and out.split()[0].isdigit():
                sizes[label] = round(int(out.split()[0]) / 1e9, 2)
            elif rc == 127:
                sizes[label] = UNAVAILABLE
    rc, out = _run(['docker', 'system', 'df', '--format', '{{.Type}}\t{{.Size}}\t{{.Reclaimable}}'], timeout=15)
    docker = [line.split('\t') for line in out.splitlines() if line.count('\t') == 2] if not rc else _command_note(rc)
    rows.append(_row('info', 'Disk usage details', sizes=sizes, docker_usage=docker))
    return rows


def _plugin_checks(source, home, directory):
    # Report directory installs only. Emit each result before the next network
    # call so a slow source cannot discard the entire profile's results.
    script = '''
import json, sys
from pathlib import Path
from hermes_cli.plugins_updates import (
    check_provenanced, default_fetch, default_ls_remote, plugins_provenance,
)
for plugin in plugins_provenance(Path(sys.argv[1])):
    try:
        result = check_provenanced(plugin, fetch=default_fetch, ls_remote=default_ls_remote).to_json()
    except Exception:
        result = {'name': plugin.name, 'error': True}
    print(json.dumps(result), flush=True)
'''
    python = source / 'venv' / 'bin' / 'python'
    failure = 'Hermes did not return a result for this install'
    try:
        proc = subprocess.run([str(python), '-c', script, str(directory)], cwd=source, text=True,
                              capture_output=True, timeout=15, stdin=subprocess.DEVNULL,
                              env={**os.environ, 'HERMES_HOME': str(home), 'PYTHONDONTWRITEBYTECODE': '1'})
        output = proc.stdout
        if proc.returncode:
            failure = f'Hermes check process exited with status {proc.returncode}'
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or b'').decode('utf-8', errors='replace')
        failure = 'Hermes profile check timed out after 15 seconds'
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return {}, 'could not run Hermes check process'

    updates = {}
    for line in output.splitlines():
        try:
            result = json.loads(line)
        except ValueError:
            continue
        if isinstance(result, dict) and isinstance(result.get('name'), str):
            updates[result['name']] = result
    return updates, failure


def _plugin_check_note(update, failure):
    if update is None:
        return 'info', 'update check unavailable: ' + failure
    if update.get('needs_fixing'):
        return 'warn', 'update feed is not trusted for this install'
    if update.get('error'):
        return 'warn', 'Hermes could not check this install'
    if update.get('update_available') is True:
        return 'warn', 'update available; restart may be needed'
    reason = update.get('reason') or ''
    if update.get('update_available') is False:
        return 'ok', 'pinned; not checked for updates' if reason.startswith('pinned') else '–'
    # Hermes reasons can contain repository URLs, local paths and raw command
    # errors. Only report standard messages, not raw reasons or stderr.
    for prefix, note in (
        ('no provenance', 'no recorded install source (manual install)'),
        ('linked to', 'linked checkout; Hermes does not manage its updates'),
        ('self-cloned', 'self-cloned checkout; not adopted by Hermes'),
        ('provenance drift', 'recorded install source does not match the checkout'),
        ('catalog entry is unavailable', 'catalog entry unavailable'),
        ('removed from catalog', 'plugin removed from catalog'),
        ('feed fetch failed', 'update feed could not be fetched'),
        ('feed unparseable', 'invalid update feed'),
        ('feed declares', 'installed revision or version missing'),
        ('cannot compare', 'installed and upstream versions cannot be compared'),
        ('ls-remote failed', 'Git remote lookup failed'),
        ('no recorded source', 'no recorded install source'),
    ):
        if reason.startswith(prefix):
            return 'info', 'update check unavailable: ' + note
    return 'info', 'update check unavailable: Hermes did not determine an update status'


def _plugins(root, profiles, report_home=None):
    rows = []
    installed = {}
    source = root / 'hermes-agent'

    for name, home in profiles:
        directory = home / 'plugins'
        try:
            plugins = sorted(p for p in directory.iterdir() if p.is_dir() and not p.name.startswith('.'))
        except OSError:
            plugins = []
        if not plugins:
            continue
        by_name, failure = _plugin_checks(source, home, directory)
        for plugin in plugins:
            update = by_name.get(plugin.name)
            status, note = _plugin_check_note(update, failure)
            version = _yaml(plugin / 'plugin.yaml').get('version') or (update or {}).get('current') or ''
            display_name = plugin.name
            installed[(name, display_name)] = version
            rows.append(_row(status, display_name, version, note, profile=name))
    if not rows:
        rows.append(_row('info', 'Plugins', notes='no plugins found or update checks unavailable'))
    try:
        with ((report_home or root) / 'hermes-health-report' / 'maintenance-latest.json').open(encoding='utf-8') as f:
            maintenance = json.load(f)
    except (OSError, ValueError):
        maintenance = {}
    if not isinstance(maintenance, dict):
        maintenance = {}
    home = report_home or root
    report_profile = next((name for name, path in profiles if path == home), 'main')
    for result in maintenance.get('plugin_results') or []:
        if not isinstance(result, dict):
            continue
        state = result.get('status', 'unknown')
        name = str(result.get('name') or 'Plugin maintenance')

        # The ledger is local to the report profile. Older results can also name the profile.
        profile = result.get('home') or report_profile
        profile = Path(str(profile)).name if profile != str(root) else 'main'
        if profile == 'default':
            profile = 'main'
        version = installed.get((profile, name), result.get('to') or '')
        rows.append(_row('fail' if state == 'failed' else 'warn' if state == 'skipped' else 'info',
                         name, version, 'maintenance result: ' + str(state), profile=profile,
                         maintenance_status=state))
    if maintenance:
        rows.append(_row('warn' if maintenance.get('restart_required') else 'info', 'Maintenance restart',
                         notes='restart required' if maintenance.get('restart_required') else 'no restart reported'))
    else:
        rows.append(_row('info', 'Maintenance results', notes='not available'))
    return rows


def _chromium_stable():
    try:
        request = urllib.request.Request('https://googlechromelabs.github.io/chrome-for-testing/last-known-good-versions.json', headers={'User-Agent': 'hermes-health-report'})
        with urllib.request.urlopen(request, timeout=5) as response:
            version = json.loads(response.read(100000))['channels']['Stable']['version']
        return version if isinstance(version, str) and re.fullmatch(r'\d+(?:\.\d+)+', version) else ''
    except urllib.error.HTTPError as error:
        error.close()
        return ''
    except (OSError, ValueError, KeyError, TypeError, AttributeError, HTTPException):
        return ''


def _chrome_lag(version, stable):
    if not (version and stable):
        return ''
    lag = int(stable.split('.')[0]) - int(version.split('.')[0])
    return f'; {lag} Chrome version{"s" if lag != 1 else ""} behind' if lag > 0 else ''


def _release(root, home):
    src = root / 'hermes-agent'
    rc, current = _run(['git', '-C', str(src), 'describe', '--tags', '--abbrev=0'])
    latest, detail, *published = _latest('NousResearch/hermes-agent', with_date=True)
    _, commit = _run(['git', '-C', str(src), 'rev-parse', 'HEAD'])
    tag_rc, tags = _run(['git', '-C', str(src), 'tag', '--points-at', 'HEAD'])
    exact_tags = tags.splitlines() if not tag_rc else []
    stable_tags = [tag for tag in exact_tags if re.fullmatch(r'v\d+\.\d+\.\d+', tag)]
    installed, release = (current if not rc else ''), ''
    if latest and latest in exact_tags:
        installed, release = latest, 'Latest release'
    elif stable_tags:
        installed = max(stable_tags, key=lambda tag: tuple(map(int, tag[1:].split('.'))))
        release = 'Stable release'
    rows = [_row('ok' if release == 'Latest release' else 'info', 'Hermes Agent', installed,
                 'upstream: ' + _release_label(latest, detail), latest=latest, commit=commit,
                 published=next(iter(published), ''), release=release)]
    config = _yaml(home / 'config.yaml')
    browser = config.get('browser') if isinstance(config.get('browser'), dict) else {}
    override = browser.get('executable_path')
    path = os.path.expanduser(override.strip()) if isinstance(override, str) and override.strip() else ''
    source = 'configured browser.executable_path' if path else ''
    # Ask the installed runtime itself: pm platform detection and managed overlays must
    # not be reimplemented or guessed from the pin file.
    python = src / 'venv' / 'bin' / 'python'
    if python.exists() and src.exists():
        script = ('import json; from hermes_cli import browser_runtime as b; '
                  'p=b.chromium_executable(); '
                  's=("configured" if b._configured_executable() else "managed" if b._pm_executable() else "baked image" if b._baked_executable() else "system fallback"); '
                  'print(json.dumps([p,s]))')
        # env is passed in an isolated interpreter without changing the process environment.
        try:
            p = subprocess.run([str(python), '-c', script], cwd=src, env={**os.environ, 'HERMES_HOME': str(home), 'PYTHONDONTWRITEBYTECODE': '1'}, capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL)
            if p.returncode == 0:
                selected, source = json.loads(p.stdout.strip().splitlines()[-1])
                path = selected or ''
        except (OSError, subprocess.SubprocessError, ValueError, IndexError, TypeError):
            pass
    if not path and not source:
        baked = Path('/etc/hermes/chromium-executable-path')
        try:
            candidate = baked.read_text(encoding='utf-8-sig').strip()
            if candidate and Path(candidate).is_file():
                path, source = candidate, 'baked image'
        except OSError:
            pass
    rc, version = _run([path, '--version']) if path else (127, '')
    stable = _chromium_stable()
    match = re.search(r'\d+(?:\.\d+)+', version) if not rc else None
    outdated = bool(match and stable and tuple(map(int, match.group().split('.'))) < tuple(map(int, stable.split('.'))))
    rows[0].update(stable=stable, playwright=_latest('microsoft/playwright')[0])
    comparison = ('older than baseline' if outdated else 'at or above baseline') if match and stable else 'comparison unavailable'
    status = 'warn' if outdated or (rc and path) else 'ok' if match and stable else 'info'
    comparison += _chrome_lag(match.group() if match else '', stable)
    if rc == 127 and not match:
        comparison += '; ' + UNAVAILABLE
    rows.append(_row(status, 'Chromium', version if not rc else '', f'source: {source or "unavailable"}; Chrome for Testing Stable baseline: {stable or "unavailable"}; {comparison}'))
    cache = Path.home() / '.cache' / 'ms-playwright'
    caches = sorted(cache.glob('chromium-*'))
    for entry in caches:
        binary = entry / 'chrome-linux64/chrome'
        rc, version = _run([str(binary), '--version'])
        match = re.search(r'\d+(?:\.\d+)+', version) if not rc else None
        lag = _chrome_lag(match.group() if match else '', stable)
        rows.append(_row('warn' if lag else 'info', f'Playwright cache ({entry.name})',
                         match.group() if match else '', ('cached browser; not necessarily effective' + lag) if match else _command_note(rc)))
    if not caches:
        rows.append(_row('info', 'Playwright cache', notes='not available'))
    images = set()
    for _, profile in _profiles(root):
        cfg = _yaml(profile / 'config.yaml')
        terminal = cfg.get('terminal')
        if isinstance(terminal, dict) and isinstance(terminal.get('docker_image'), str):
            image = terminal['docker_image'].strip()
            if image:
                images.add(image)
    for image in sorted(images):
        rc, created = _run(['docker', 'image', 'inspect', '--format', '{{.Created}}', '--', image])
        chrome = re.search(r'cft(\d+)', image)
        playwright = re.search(r'playwright[^:]*:v?(\d+\.\d+\.\d+)', image)
        lag = _chrome_lag(chrome[1] if chrome else '', stable)
        description = ' · '.join(filter(None, (f'Playwright {playwright[1]}' if playwright else '',
                                                f'Chrome {chrome[1]}' if chrome else '')))
        latest_playwright = rows[0]['playwright']
        latest_version = re.fullmatch(r'v?(\d+)\.(\d+)\.(\d+)', latest_playwright or '')
        behind_playwright = bool(playwright and latest_version and
                                 tuple(map(int, playwright[1].split('.'))) < tuple(map(int, latest_version.groups())))
        notes = f'built {created[:10]}' if not rc and created else 'Docker not available' if rc == 127 else 'image not found or Docker inspect unavailable'
        if behind_playwright:
            notes += f'; Playwright latest: {latest_playwright}'
        rows.append(_row('warn' if lag or behind_playwright else 'info', f'Sandbox image {image}', description, notes + lag))
    return rows


def _os_updates():
    info = dict(line.split('=', 1) for line in _text('/etc/os-release').splitlines() if '=' in line)
    rc, out = _run(['apt-get', '-s', '-o', 'Debug::NoLocking=1', 'dist-upgrade'], timeout=30)
    security, other = set(), set()
    if not rc:
        for line in out.splitlines():
            match = re.match(r'^Inst (\S+) (?:\[[^\]]+\] )?\(([^)]*)', line)
            if match:
                (security if '-security' in match[2] else other).add(match[1])
    other -= security
    kernel_update = any(name.startswith(('linux-image', 'linux-generic', 'linux-headers')) for name in security | other)
    reboot = Path('/var/run/reboot-required').exists()
    try:
        newest = max(p.stat().st_mtime for p in Path('/var/lib/apt/lists').iterdir() if p.is_file())
        age = round((dt.datetime.now().timestamp() - newest) / 3600, 1)
    except (OSError, ValueError):
        age = None
    urc, enabled = _run(['systemctl', 'is-enabled', 'unattended-upgrades.service'])
    src, snaps = _run(['snap', 'refresh', '--list'], timeout=20)
    refreshes = [line.split()[0] for line in snaps.splitlines()[1:] if line.strip()] if not src else []
    # Some snap versions report an empty list on stderr with a nonzero exit.
    apt = _command_note(rc) if rc else None
    status = 'warn' if security or reboot or kernel_update else 'info' if other or apt or refreshes else 'ok'
    return [_row(status, 'APT updates', os_updates=dict(
        release=info.get('PRETTY_NAME', '').strip('"'), kernel=os.uname().release,
        apt=apt, security=sorted(security), other=sorted(other), kernel_update=kernel_update,
        reboot=reboot, reboot_pkgs=_text('/var/run/reboot-required.pkgs').split(), age=age,
        enabled=enabled or _command_note(urc), snaps=refreshes,
        snap_status=_command_note(src) if src else 'None waiting'))]


def _memory_env(home, key):
    """Read a single non-secret setting without exporting or reporting .env contents."""
    try:
        with (home / '.env').open(encoding='utf-8') as f:
            for line in f:
                match = re.match(r'^\s*(?:export\s+)?' + re.escape(key) + r'\s*=\s*(.*?)\s*$', line)
                if match:
                    return match.group(1).strip().strip('"\'')
    except (OSError, UnicodeError):
        pass
    return ''


def _memory_health(home, cache):
    cfg = {}
    for path in (home / 'hindsight' / 'config.json', Path.home() / '.hindsight' / 'config.json'):
        try:
            with path.open(encoding='utf-8') as f:
                cfg = json.load(f)
            if isinstance(cfg, dict):
                break
        except (OSError, ValueError, UnicodeError):
            pass
    if not isinstance(cfg, dict):
        cfg = {}
    local = cfg.get('mode') in ('local_embedded', 'local_external')
    default = 'http://localhost:8888' if local else 'https://api.hindsight.vectorize.io'
    return _hindsight_api_health(cfg.get('api_url') or _memory_env(home, 'HINDSIGHT_API_URL') or default, cache)


def _hindsight_api_health(url, cache):
    if not isinstance(url, str) or not url.startswith(('http://', 'https://')):
        return 'info', 'health unavailable; endpoint not configured'
    endpoint = url.rstrip('/') + '/health'
    if endpoint not in cache:
        try:
            request = urllib.request.Request(endpoint, headers={'User-Agent': 'hermes-health-report'})
            with urllib.request.urlopen(request, timeout=5) as response:
                ok = 200 <= response.status < 300
                data = json.loads(response.read(4096)) if ok else None
            healthy = isinstance(data, dict) and data.get('status') in ('healthy', 'ok')
            cache[endpoint] = ('ok', 'healthy') if healthy else ('warn', 'health check did not pass')
        except urllib.error.HTTPError as error:
            error.close()
            cache[endpoint] = ('warn', 'health unavailable')
        except (OSError, ValueError, HTTPException):
            cache[endpoint] = ('warn', 'health unavailable')
    return cache[endpoint]


def _report_url(value):
    if not isinstance(value, str) or not value or any(c.isspace() or c in '<>"\\' for c in value):
        return ''
    try:
        parts = urlsplit(value)
        if (parts.scheme in ('http', 'https') and parts.hostname and parts.username is None
                and parts.password is None and not parts.query and not parts.fragment
                and (parts.port is None or parts.port > 0)):
            return value
    except ValueError:
        pass
    return ''


def _memory(profiles, config):
    rows = []
    health_cache = {}
    for name, home in profiles:
        cfg = _yaml(home / 'config.yaml')
        memory = cfg.get('memory') if isinstance(cfg.get('memory'), dict) else {}
        for filename, enabled_key, limit_key, default in [('MEMORY.md', 'memory_enabled', 'memory_char_limit', 2200), ('USER.md', 'user_profile_enabled', 'user_char_limit', 1375)]:
            enabled = memory.get(enabled_key, True)
            path = home / 'memories' / filename
            limit = memory.get(limit_key, default)
            try:
                # Count only; do not retain or return contents.
                with path.open(encoding='utf-8') as f:
                    length = sum(len(chunk) for chunk in iter(lambda: f.read(65536), ''))
                pct = f'{100 * length / limit:.1f}%' if isinstance(limit, (int, float)) and limit > 0 else 'limit unavailable'
                notes = f'{"on" if enabled else "off"}; {pct} of character limit'
            except (OSError, UnicodeError):
                notes = f'{"on" if enabled else "off"}; file unavailable'
            rows.append(_row('info', filename, notes=notes, profile=name))
        provider = memory.get('provider')
        status, notes = _memory_health(home, health_cache) if provider == 'hindsight' else ('info', 'health not available')
        rows.append(_row(status, 'Memory provider', str(provider) if isinstance(provider, str) else '', notes, profile=name))
    hindsight = config.get('hindsight')
    if isinstance(hindsight, dict):
        console = _report_url(hindsight.get('web_url'))
        api = _report_url(hindsight.get('api_url'))
        status, notes = _hindsight_api_health(api, health_cache)
        rows.append(_row(status, 'Hindsight memory', notes=notes, hindsight_web=console))
    vaults = config.get('vaults', [])
    vaults = [v for v in vaults if isinstance(v, dict) and isinstance(v.get('path'), str) and v['path'].strip()] if isinstance(vaults, list) else []
    if not vaults:
        seen = set()
        for name, home in profiles:
            value = _memory_env(home, 'OBSIDIAN_VAULT_PATH')
            if value and value not in seen:
                seen.add(value)
                vaults.append({'path': value})
    if not vaults:
        rows.append(_row('info', 'Vault', notes='not configured', vault=True))
    report_zone = dt.datetime.now().astimezone().tzinfo
    for vault in vaults:
        path = Path(vault['path']).expanduser()
        count, newest, stamp = 0, 0.0, None
        try:
            if not path.is_dir():
                raise OSError('missing vault')
            for base, dirs, files in os.walk(path, followlinks=False):
                dirs[:] = [d for d in dirs if not (Path(base) / d).is_symlink()]
                for filename in files:
                    item = Path(base) / filename
                    if item.suffix.lower() != '.md' or item.is_symlink():
                        continue
                    try:
                        stat = item.stat()
                        count += 1
                        newest = max(newest, stat.st_mtime)
                    except OSError:
                        continue
            if count:
                stamp = dt.datetime.fromtimestamp(newest, report_zone).strftime('%Y-%m-%d %H:%M:%S')
            notes = ''
            status = 'info'
        except OSError:
            count, notes, status = None, 'vault unavailable', 'warn'
        rows.append(_row(status, str(vault.get('name') or path.name or 'Vault'), notes=notes,
                         vault=True, vault_path=str(path), documents=count, last_change=stamp))
    return rows


def _services(config):
    rows = []
    units = config.get('services')
    units = [unit for unit in units if isinstance(unit, str) and unit.strip()] if isinstance(units, list) else []
    if not units:
        for unit in ('hermes-gateway.service', 'hermes-serve.service'):
            for scope, label in ((['--user'], 'user'), ([], 'system')):
                rc, output = _run(['systemctl', *scope, 'show', unit, '-p', 'LoadState,ActiveState'])
                props = dict(line.split('=', 1) for line in output.splitlines() if '=' in line)
                if not rc and props.get('LoadState') in ('loaded', 'masked'):
                    state = props.get('ActiveState', 'unavailable')
                    rows.append(_row('ok' if state == 'active' else 'warn', unit,
                                     notes=f'state: {state} ({label})'))
    if not units:
        for scope in (['--user'], []):
            rc, out = _run(['systemctl', *scope, 'list-units', 'cloudflared*.service', '--all', '--plain', '--no-legend'])
            if not rc:
                for line in out.splitlines():
                    columns = line.split()
                    if len(columns) >= 3 and columns[1] == 'loaded':
                        unit, _, state = columns[:3]
                        rows.append(_row('ok' if state == 'active' else 'warn', unit, notes='state: ' + state))
    for unit in units:
        rc, state = _run(['systemctl', '--user', 'is-active', unit])
        if rc:
            rc2, state2 = _run(['systemctl', 'is-active', unit])
            if not rc2:
                rc, state = rc2, state2
        rows.append(_row('ok' if not rc and state == 'active' else 'warn', unit, notes='state: ' + (state if state in ('active', 'inactive', 'failed', 'activating', 'deactivating') else 'unavailable')))
    for probe in config.get('probes', []):
        if not isinstance(probe, dict) or not isinstance(probe.get('argv'), list) or not probe['argv'] or not all(isinstance(x, str) for x in probe['argv']):
            rows.append(_row('warn', 'Configured probe', notes='invalid argv'))
            continue
        rc, _ = _run(probe['argv'])
        rows.append(_row('ok' if not rc else 'warn', str(probe.get('name') or 'Configured probe'), notes='passed' if not rc else 'failed or unavailable'))
    if not rows and not shutil.which('systemctl'):
        rows.append(_row('info', 'Service units', notes=UNAVAILABLE))
    dashboard_url = _report_url(config.get('dashboard_url'))
    if dashboard_url:
        rows.append(_row('info', 'Hermes dashboard', service_url=dashboard_url, dependency=True))
    crc, out = _run(['cloudflared', '--version'])
    match = re.search(r'version (\S+)', out) if not crc else None
    latest_cloud = _latest('cloudflare/cloudflared')[0]
    installed_cloud = re.search(r'\d+(?:\.\d+)+', match[1]) if match else None
    upstream_cloud = re.search(r'\d+(?:\.\d+)+', latest_cloud)
    update = installed_cloud and upstream_cloud and tuple(map(int, upstream_cloud[0].split('.'))) > tuple(map(int, installed_cloud[0].split('.')))
    rows.append(_row('info' if update or not match else 'ok', 'cloudflared (webhook tunnel)',
                     match[1] if match else _command_note(crc),
                     'latest: ' + (latest_cloud or 'unavailable') + (' available' if update else ''), dependency=True))
    drc, docker = _run(['docker', 'version', '--format', '{{.Server.Version}}'])
    rows.append(_row('info' if drc else 'ok', 'Docker engine', docker if not drc else _command_note(drc), dependency=True))
    rc, out = _run(['tailscale', 'version'])
    installed = out.splitlines()[0] if not rc and out else ''
    latest, detail = _latest('tailscale/tailscale')
    left = re.search(r'\d+(?:\.\d+)+', installed)
    right = re.search(r'\d+(?:\.\d+)+', latest)
    if left and right:
        current_version = tuple(map(int, left.group().split('.')))
        latest_version = tuple(map(int, right.group().split('.')))
        available = latest_version > current_version
        comparison = 'update available' if available else 'no update available'
    else:
        available = False
        comparison = 'comparison unavailable'
    rows.append(_row('warn' if available else 'info', 'Tailscale', installed or _command_note(rc), 'latest stable: ' + _release_label(latest, detail) + '; ' + comparison + '; restart may interrupt remote access'))
    return rows


def collect(home: Path, config: dict) -> dict:
    """Collect partial results even when a platform or dependency is unavailable."""
    home = Path(home)
    root = _root(home)
    config = config if isinstance(config, dict) else {}
    profiles = _profiles(root)
    collectors = [lambda: _backup(config), lambda: _disk(config), lambda: _plugins(root, profiles, home),
                  lambda: _release(root, home), _os_updates, lambda: _memory(profiles, config), lambda: _services(config)]
    result = {}
    for section, collector in zip(SECTIONS, collectors):
        try:
            result[section] = collector()
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            result[section] = [_row('warn', section, notes='collection unavailable')]
    return result
