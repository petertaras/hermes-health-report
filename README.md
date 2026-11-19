# hermes-health-report

A Hermes Agent instance runs unattended for most of the week. Backups can fail, disks fill, plugins drift behind their upstreams and security updates wait, and none of that is visible until something breaks. Checking it by hand means logging in and running the same commands each time.

## Purpose

This plugin collects health facts from the host and from every Hermes profile it can see, and writes them as one HTML report. It can email the report through delegated Microsoft Graph mail, fall back to SMTP or to a Hermes send target, or write a local HTML file. Delivery is off until you turn it on.

A report that goes out by Graph mail can offer a small set of maintenance actions. The mailbox owner replies `approved` to the report, and the plugin creates one Kanban card for the work. Only two actions can ever be offered: updating an allowlisted set of plugins, and restarting one approved Hermes user service after the card completes. Hermes Agent releases, OS packages and Tailscale are reported only. The plugin never updates or restarts them on a mail approval.

Scheduling and maintenance are also opt-in. Nothing runs on a timer until you create the jobs and enable them in the config.

> [!WARNING]
> **Preview.** This is version 0.1.0, an early preview. Expect breaking changes. Issues and pull requests are welcome.

## Requirements

- Linux with systemd. The collectors use `systemctl`, `journalctl` and APT, and they report a partial result when a tool is missing.
- Hermes Agent 0.23.2 or later.
- Optional: Microsoft Graph mail needs a delegated token helper in the profile that provides `get_token()` and reads its credentials from profile-local files. The plugin points the helper at the profile `.env` file and a profile-local token cache, and it never falls back to another profile. The mailbox needs delegated permission to identify the user, send mail and read messages, and the configured sender must belong to that mailbox.
- Optional: SMTP needs a mail account, and the Kanban approval flow needs an existing board and a profile to do the maintenance work.
- Optional: release lookups use `GITHUB_TOKEN` from the report process environment, or a GitHub CLI sign-in for the report user. Without either, the plugin uses the public GitHub API and reports the reason when a lookup fails. Do not put a token in the report config.

The collectors also use tools such as `smartctl` or `udisksctl` for NVMe health, `docker` for container disk use, `gh` for release lookups and `systemd-run` for a delayed restart. Each missing tool degrades one part of the report. It does not stop the rest.

## How it works

`hermes health-report report` collects seven sections and renders one self-contained HTML page:

1. **Backup.** The configured user service, its timer, the last result and exit status, the size of the last verified upload found in the journal, and the next run.
2. **Disk.** Mount usage in GB, NVMe SMART data including wear, spare blocks, media errors and temperature, and the sizes of the largest local directories and Docker usage. Media error counts are compared with the previous report.
3. **Plugins.** One row per directory-installed plugin in each profile, with the installed version and whether Hermes reports an update. Linked and pinned installs are reported as not managed or not checked. The latest recorded maintenance result is included when one exists.
4. **Hermes Agent release.** Installed version against the latest upstream release, the effective Chromium build and where it came from, Playwright caches and sandbox images, each compared with the Chrome for Testing Stable baseline where possible.
5. **OS and security updates.** APT security and other update counts as separate lists, whether a kernel update or a reboot is waiting, the age of the package lists, unattended-upgrades status and any Snap refreshes waiting. Report only.
6. **Memory.** For each profile, `MEMORY.md` and `USER.md` sizes against their character limits, and the memory provider. When a Hindsight endpoint is configured, its `/health` result. Configured vaults are listed with document counts and the newest modification time. Memory file contents are never read into the report.
7. **Services and other dependencies.** Configured systemd units, or detected Hermes gateway and Desktop backend units when none are configured, plus any `cloudflared` units found. The report also records Docker, `cloudflared` and Tailscale versions against their upstreams. Tailscale is reported with a note that a restart can interrupt remote access.

Every collector runs even when another one fails. A collector that throws returns a warning row for its section, so a missing dependency gives you a partial report with the gap marked, rather than no report at all.

The `--kind maintenance` variant collects the same sections and marks the report as a maintenance report, so the Plugins section can show the results recorded by the last maintenance run. It offers no actions.

## Delivery

A dry run writes a private HTML preview and does nothing else. It does not send mail, create an approval offer or run maintenance.

A live run tries delivery in this order:

1. **Graph.** Delegated mail from the configured sender. The subject carries the report ID, and the plugin confirms the exact report in Sent Items.
2. **SMTP.** Used only when the Graph send fails in a way that proves the message was not accepted, or Graph is unavailable.
3. **Profile.** A `hermes send` target, given the report text on standard input.
4. **Local.** A private HTML file in the profile report directory.

Vault paths appear only in the email version. The local file and the default preview omit them, and `--preview-email` produces the email version as a private preview.

If a send result is uncertain, the plugin stops. It records the report ID in `delivery-pending.json` and does not try a fallback, because a fallback could send the report twice. Check Sent Items or the provider, and clear that file only when you know what happened.

## Approvals in brief

Actions are offered only in a Graph report, only when approvals are enabled, only to a configured owner who is among the recipients, and only when reply access, the Kanban board and the maintenance profile are all available. The offer becomes active only when the exact report ID is confirmed in Sent Items. Fallback reports offer no actions.

An owner replies from their own mailbox within 72 hours. `approved` on a line by itself approves every listed action, and `approved 2` approves only action 2. Anything else creates nothing. See [Actions and approval rules](docs/approvals.md) for the full rules.

One accepted reply creates at most one Kanban card for the maintenance profile, with an idempotency key derived from the report ID. The card tells the worker which approved command to run and nothing else. Email text is never passed to the worker as instructions. When the card completes, a hook schedules the approved restart, if any, for two minutes after completion, and the poll job emails the result to the approving owner.

## Settings

Settings live in `hermes-health-report/config.yaml` inside the selected profile directory. No credentials belong in this file. SMTP passwords go in the profile `.env` file, and Graph credentials stay with the token helper.

| Setting | Meaning |
|---|---|
| `instance_name` | Name shown at the top of the report. Defaults to the Hermes Unix user. |
| `sender` | Mailbox address that sends reports. |
| `recipients` | List of addresses that receive reports. |
| `owners` | List of addresses allowed to approve actions by reply. |
| `scheduler`, `maintenance`, `approvals` | Master switches for the scheduled jobs, the maintenance job and reply approvals. All default to off. |
| `delivery.graph`, `delivery.smtp`, `delivery.profile` | Enable each delivery channel. All default to off. |
| `graph_token_helper` | Trusted Python token helper. Use an absolute path or a path relative to the report profile. No default. |
| `smtp.host`, `smtp.port`, `smtp.security`, `smtp.username` | SMTP server. `security` is `ssl` or `starttls`. The password is `EMAIL_PASSWORD` in the profile `.env` file. |
| `profile.target`, `profile.command` | `hermes send` destination and, if needed, the Hermes executable. |
| `backup_unit`, `backup_target` | Backup user service to check, and a label for where backups go. No backup service is checked by default. |
| `services` | Extra systemd units to check. |
| `dashboard_url` | Hermes dashboard link shown in Services. The link is listed, not checked. |
| `disk_mounts` | Mounts to report. Defaults to `/`, plus `/boot/efi` when mounted. |
| `disk_size_paths` | Directory paths to measure and show in Disk usage details. No directories are measured by default. |
| `vaults` | List of `{name, path}` document vaults to count and date. |
| `hindsight.web_url`, `hindsight.api_url` | Memory console link and API base. The report checks `/health` on the API base and never prints that URL. |
| `probes` | List of `{name, argv}` read-only commands. The report shows pass or fail only, never the output. |
| `kanban_board`, `maintenance_profile` | Board for approval cards, and the profile assigned to them. |
| `update_plugins` | Exact plugin names an approval may update on the report profile. |
| `restart_unit`, `restart_downtime_minutes` | One Hermes user service an approval may restart after the card completes, and the downtime shown in the email. |

## Scheduled jobs

`hermes health-report setup` writes small wrapper scripts into the profile `scripts` directory and creates or updates three named cron jobs:

- `hermes-health-report:scheduled-maintenance` at 07:30 on Saturday. This job runs the allowlisted plugin update with an agent.
- `hermes-health-report:report` at 08:30 on Saturday, no agent.
- `hermes-health-report:poll` every 15 minutes, no agent. This job checks for approval replies.

Jobs use the profile time zone. The report and poll jobs run only when `scheduler: true`. The maintenance and poll jobs also need `maintenance: true` and `approvals: true`. Setup refuses to overwrite a wrapper or reuse a job name that points somewhere else. Turning the switches off and running setup again pauses the jobs.

## Install and verify

Start on a test profile. Install an exact commit and enable the plugin on that profile:

```sh
hermes -p example-lab plugins install <repository-url> --ref <full-commit-sha> --enable
hermes -p example-lab plugins doctor --ci hermes-health-report
hermes -p example-lab health-report status
hermes -p example-lab health-report report --dry-run
hermes -p example-lab health-report report --kind maintenance --dry-run
```

The command prints the preview path. Check the preview, then work through the [setup guide](docs/setup.md).

Run the tests from the repository root:

```sh
python -m pip install PyYAML pytest
python -m pytest -q
```

## Documentation

- [Setup guide](docs/setup.md)
- [Actions and approval rules](docs/approvals.md)
- [Architecture](docs/architecture.md)
- [Design](docs/design.md)
- [Changelog](CHANGELOG.md)

## License

MIT. See [LICENSE](LICENSE).
