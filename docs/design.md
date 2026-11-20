# Design

The Microsoft Graph paths below describe the legacy flow. Graph delivery and new approvals are unavailable in 0.1.1.

This document records the decisions behind the current implementation. Setup and day-to-day use belong in the [setup guide](setup.md) and the [approval rules](approvals.md).

## Configuration in the profile

All settings live in `hermes-health-report/config.yaml` inside the selected Hermes profile directory, along with the report state. Nothing about the instance, its mailboxes or its services is hardcoded in a report. Scheduler, delivery, maintenance and reply approval settings default to off, so installing the plugin changes nothing until an operator configures it. The config file holds no credentials. The plugin reads the SMTP password through Hermes.

## Partial reports

Each of the seven collectors runs on its own. A collector that fails returns a warning row for its section instead of aborting the report. An unattended health check is most needed exactly when part of the system is broken, and a report that shows six sections and marks the seventh as unavailable is more useful than no report.

Collectors are read-only. They inspect services, disks, packages, plugins and files, and they compare installed versions with upstream releases. The only commands that change anything are the ones behind an approved maintenance card and the scheduled restart, both described below.

## Report shape

The report is one self-contained HTML page that works as an email body and as a saved file. It uses a `rgb(22, 30, 63)` header with the instance name above the report title and the hostname at the top right. A verdict banner uses the worst section status, followed by a summary table with one headline per section, then the section details in this order:

1. Backup
2. Disk
3. Plugins
4. Hermes Agent release
5. OS and security updates
6. Memory
7. Services and other dependencies

Section statuses use fixed symbols and labels: ✅ OK, ℹ️ Info, ⚠️ Warning and ❌ Failed. Memory file contents are never included. Vault paths appear only in the email version, because the local file and the default preview are easier to share by accident.

## Browser reporting

The report shows the effective Chromium version and where it came from, and compares it with the latest Chrome for Testing Stable version. Hermes asks its own installed runtime which executable would be used, rather than guessing from configuration order, because a configured path, a managed browser and a baked image path can all be present at once. Playwright caches and sandbox images are listed separately, since a cached browser is not necessarily the one in use. Hermes releases are not mapped to browser builds. The two move independently.

## Delivery order and uncertain sends

Delivery tries delegated Microsoft Graph mail first, then SMTP, then a Hermes send target, then a local file. A channel is skipped only on a definite failure or when it is unavailable. When a send result cannot be classified, the run stops and leaves `delivery-pending.json` in place. Sending a second copy through a fallback would be worse than sending none until an operator checks, and an approval offer can only be activated by the copy in Sent Items, so a duplicate report would also create a confusing second offer.

## Mail approvals

An approval is a reply, not a link or a form. The plugin checks the reported address, not the authenticated mailbox identity. Keep approvals off unless the mail system guarantees that identity as described in the [approval rules](approvals.md). The offer binds the report ID to the mailbox, sent message and conversation for 72 hours. The plugin checks the full plain-text reply for a single unquoted command line.

Only two actions can be offered: updating an explicit allowlist of plugins on the report profile, and restarting one named Hermes user service. Hermes Agent releases, OS packages and Tailscale stay report-only, because their updates carry more risk than a mail reply should authorise. The allowlist names plugins on the report profile so that an approval cannot widen its own scope, and the offer stores a snapshot of the maintenance plan. If the settings changed after the report was sent, the offer no longer matches and a new report is needed.

Approval creates a Kanban card rather than running commands from the poll job. The card is fixed text written by the plugin, assigned to the configured maintenance profile, and keyed idempotently by report ID. That gives the work a visible place, a worker with the right tools, and a completion event the plugin can react to.

## Delayed restart

An approved restart runs last, two minutes after the maintenance card completes, scheduled through `systemd-run`. The delay lets the completion hook finish and the result email go out before the service goes down, and the email states the expected downtime up front. If the hook does not run inside the two-minute window, the restart is recorded as missed and never run late. A restart that fires long after the approval, at a moment nobody is expecting, is worse than one that has to be approved again.

## Scheduling

There is no plugin cron hook. The setup command writes profile-local wrapper scripts and creates three named jobs: optional maintenance at 07:30 on Saturday, the report at 08:30 on Saturday, and the approval poll every 15 minutes. Setup is repeatable. It refuses to overwrite a wrapper whose contents differ and refuses to reuse a job name bound to another script, and running it with the switches off pauses the jobs. The report and poll jobs run without a model. Only the maintenance job and the maintenance card use an agent.
