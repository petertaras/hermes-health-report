# Setup

Use Hermes Agent 0.23.2 or later on Linux with systemd. Start on a test profile. Install an exact commit and enable the plugin on that profile:

```sh
hermes -p example-lab plugins install <repository-url> --ref <full-commit-sha> --enable
hermes -p example-lab plugins doctor --ci hermes-health-report
hermes -p example-lab health-report status
hermes -p example-lab health-report report --dry-run
hermes -p example-lab health-report report --kind maintenance --dry-run
```

The command prints the preview path. A dry run writes only HTML. It does not send mail, create an approval offer or run maintenance. The default preview and local delivery omit vault paths. Add `--preview-email` to `--dry-run` to include email-only details such as vault paths. Keep that preview private.

For authenticated release lookups, configure `GITHUB_TOKEN` through Hermes on the report profile. Without it, the report uses the public GitHub API. Do not put a token in the report config.

## Configuration

Create `hermes-health-report/config.yaml` in the selected Hermes profile directory. Keep this file private. Omit settings that you do not use. Put no credentials in this file.

```yaml
instance_name: Example Lab
sender: reports@example.com
recipients: [owner@example.com]
owners: [owner@example.com]
scheduler: false
maintenance: false
approvals: false
delivery:
  smtp: false
  profile: false
backup_unit: example-backup.service
backup_target: example backup account / container
services: [example-worker.service]
dashboard_url: https://dashboard.example.com
disk_mounts: ['/']
disk_size_paths: ['~/example-data', '/mnt/example-backup']
vaults:
  - name: Notes
    path: /mnt/example-notes
hindsight:
  web_url: https://memory.example.com
  api_url: https://memory-api.example.com
probes: []
kanban_board: example-board
maintenance_profile: example-lab
update_plugins: []
# restart_unit: hermes-gateway-example.service
# restart_downtime_minutes: 2
```

Optional probes accept a `name` and an `argv` list. Use trusted read-only commands only. The report shows the exit result, not the command output.

Set `backup_unit` to check a backup user service. Without it, the report does not check a backup service. Set `disk_size_paths` to list the directories to measure. Use absolute paths or paths that start with `~/`. The report uses the configured paths as labels.

Set `dashboard_url` to show the Hermes dashboard link in Services. Use an HTTP or HTTPS URL without credentials, a query string or a fragment. The report lists the link but does not check its availability.

Set `hindsight.web_url` and `hindsight.api_url` to show the console link and API state in Memory. Use HTTP or HTTPS base URLs without credentials, query strings or fragments. The report checks `/health` on the API base URL. It does not print the API URL or send credentials. Per-profile API checks still use each profile's own memory configuration.

List each vault explicitly under `vaults`. The report no longer discovers vaults or Hindsight endpoints from profile `.env` files. Vault paths appear only in the email version.

## Delivery

- **Microsoft Graph:** Unavailable in 0.1.1. Remove the old `graph_token_helper` setting and keep `approvals: false`.
- **SMTP:** Set `smtp.host`, `smtp.port`, `smtp.security` (`ssl` or `starttls`) and, if needed, `smtp.username`. Configure `EMAIL_PASSWORD` through Hermes on the report profile. Enable `delivery.smtp`.
- **Profile:** Set `profile.target` to a `hermes send` destination. Enable `delivery.profile`.
- **Local:** If no enabled channel accepts the report, the plugin writes a private HTML file in the profile report directory. If a send result is uncertain, it stops instead of trying another channel.

No-agent cron scripts may not receive `EMAIL_PASSWORD` or `GITHUB_TOKEN` from Hermes. Without those secrets, an authenticated SMTP configuration is skipped and release lookups use the public GitHub API. Use local delivery for unattended reports unless your Hermes delivery route is available in that profile. Do not pass credentials in the cron command or put them in plugin configuration.

`health-report status` reports whether Microsoft Graph sending and reply access work, whether the Kanban board and maintenance profile are available, whether a backup unit is configured, which switches are on, and whether an unresolved delivery is recorded.

## Schedule and rollback

Run `health-report setup` on the selected profile. It writes wrapper scripts into the profile `scripts` directory and creates or updates three named jobs. Optional maintenance runs at 07:30 on Saturday with an agent. The report runs at 08:30 on Saturday without an agent. The approval poll runs every 15 minutes without an agent. Jobs use the profile time zone.

Preview the report on the profile first. Resolve open approval offers before you change maintenance settings or switch jobs. Set `scheduler: true` and run setup again to start the report job. Set `maintenance: true` only to enable scheduled maintenance. Keep `approvals: false` in 0.1.1.

Setup refuses to replace a wrapper script with different contents, and it refuses to reuse a job name that is bound to a different script.

To roll back, set `scheduler`, `maintenance` and `approvals` to `false`. Run setup again to pause the plugin jobs. Check the user systemd timers for any restart that was already scheduled, because a scheduled restart still runs after the jobs stop.
