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
  graph: false
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

- **Microsoft Graph:** Follow the OAuth setup below, then enable `delivery.graph`. It is off by default. Remove the old `graph_token_helper` setting; it is not used.
- **SMTP:** Set `smtp.host`, `smtp.port`, `smtp.security` (`ssl` or `starttls`) and, if needed, `smtp.username`. Configure `EMAIL_PASSWORD` through Hermes on the report profile. Enable `delivery.smtp`.
- **Profile:** Set `profile.target` to a `hermes send` destination. Enable `delivery.profile`.
- **Local:** If no enabled channel accepts the report, the plugin writes a private HTML file in the profile report directory. If a send result is uncertain, it stops instead of trying another channel.

No-agent cron scripts may not receive `EMAIL_PASSWORD` or `GITHUB_TOKEN` from Hermes. Without those secrets, an authenticated SMTP configuration is skipped and release lookups use the public GitHub API. Use local delivery for unattended reports unless your Hermes delivery route is available in that profile. Do not pass credentials in the cron command or put them in plugin configuration.

### Microsoft Graph OAuth

1. In Microsoft Entra, register a mobile/desktop public client with no client secret. Use an account type that supports the `/common` authority. Register `http://127.0.0.1/callback` for this public client. Hermes prints the callback URL with an OS-assigned port. Follow Microsoft's [loopback redirect guidance](https://learn.microsoft.com/en-us/entra/identity-platform/reply-url) for registering an HTTP IP-literal redirect.
2. Add delegated Microsoft Graph permissions `User.Read`, `Mail.Send`, and `Mail.Read`, and obtain any tenant-required consent. The OAuth request also asks for `offline_access` so Hermes can refresh the grant. The client uses the Microsoft identity platform common v2 authorize and token endpoints and the `graph.microsoft.com` API host.
3. Configure the application's public client ID as `HEALTH_REPORT_GRAPH_CLIENT_ID` in the selected profile's Hermes environment. This is an application ID, not a client secret. Set `sender` to the signed-in mailbox and configure recipients. Do not place tokens or credentials in the report config.
4. As the same OS user and profile that will run reports, sign in interactively and check the grant:

   ```sh
   hermes -p example-lab auth add hermes-health-report:graph
   hermes -p example-lab auth status hermes-health-report:graph
   ```

   Hermes owns and refreshes the profile-scoped grant; it will not start interactive sign-in from cron. If the browser is on a different host, use the port and SSH tunnel instructions printed by Hermes; see [OAuth over SSH](https://hermes-agent.nousresearch.com/docs/guides/oauth-over-ssh). Do not expose the callback listener publicly.
5. Enable `delivery.graph`, check `hermes -p example-lab health-report status`, and verify delivery on a test profile. Leave `approvals: false` until your mail administrator verifies owner authentication on every delivery path as described in [approval rules](approvals.md). Graph delivery does not require approvals.

To remove the local grant, run `hermes -p example-lab auth logout hermes-health-report:graph`. Revoke consent in Microsoft Entra separately if needed. The plugin uses Hermes public OAuth and does not invoke a token helper.

Hermes reads each full Graph response into memory before the plugin checks its 1 MiB response and 4 MiB total conversation-page limits. Exceeding a limit fails closed; these limits are not peak-memory or transfer bounds.

`health-report status` reports whether Microsoft Graph sending and reply access work, whether the Kanban board and maintenance profile are available, whether a backup unit is configured, which switches are on, and whether an unresolved delivery is recorded.

## Schedule and rollback

Run `health-report setup` on the selected profile. It writes wrapper scripts into the profile `scripts` directory and creates or updates three named jobs. Optional maintenance runs at 07:30 on Saturday with an agent. The report runs at 08:30 on Saturday without an agent. The approval poll runs every 15 minutes without an agent. Jobs use the profile time zone.

Preview the report on the profile first. Resolve open approval offers before you change maintenance settings or switch jobs. Set `scheduler: true` and run setup again to start the report job. Set `maintenance: true` only to enable scheduled maintenance. Enable `approvals` only after the administrator verification above; run setup again to start the approval poll.

Setup refuses to replace a wrapper script with different contents, and it refuses to reuse a job name that is bound to a different script.

To roll back, set `scheduler`, `maintenance` and `approvals` to `false`. Run setup again to pause the plugin jobs. Check the user systemd timers for any restart that was already scheduled, because a scheduled restart still runs after the jobs stop.
