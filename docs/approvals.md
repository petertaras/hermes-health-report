# Actions and approval rules

**Keep `approvals: false` (the default) until your mail administrator verifies the approval mailbox's sender authentication.** Microsoft Graph `from` and `sender` agreement, the `owners` list, and reply headers do not prove the owner sent the message. The plugin does not authenticate the owner itself.

Before enabling approvals, verify that the mail system only delivers mail asserting a configured owner address from that authenticated owner. Check external ingress, relays, connectors and delegated send permissions. Domain authentication alone does not prove an individual mailbox identity. If you cannot verify this guarantee for every delivery path, leave approvals off. This does not prevent report delivery.

A report can offer actions only in Microsoft Graph mail. The plugin also requires a configured owner among the recipients, reply access to the mailbox, an existing Kanban board and a maintenance profile.

1. Reply to the original report from a configured owner mailbox within 72 hours of the sent time.
2. Write `approved` on a line by itself to approve all actions. To approve selected actions, write only their numbers after `approved`. For example, write `approved 2`. To approve more than one, write `approved 2 4`.
3. Wait for the confirmation email. One offer creates at most one maintenance card.

The reply must be a direct reply in the same conversation. The plugin checks the stored sent message again and matches the reply to it through the mail headers. It checks that the sender and visible sender addresses match. It reads the full plain-text body. Only `>`-quoted lines and their `On … wrote:` attribution are ignored. Remove signatures, forwarded sections and other unquoted history. Only one unquoted command line is accepted.

A question, a negation, an invalid number, a self-sent message, a message in another conversation, a second reply to a consumed offer, or a reply after 72 hours cannot create a card. No approval is active until the exact report appears in Sent Items. Only the latest confirmed offer for a conversation can be active, and a newer confirmed report for that conversation replaces the older one. Fallback reports never offer actions.

## Allowlist

- **Plugin updates:** Set `update_plugins` to the exact plugins to update on the report profile. Linked plugins are skipped without an update attempt, and this plugin never updates itself through an approval. Hermes can refuse pinned or unsafe updates. The result of each update is recorded for the next report.
- **Hermes restart:** Set `restart_unit` to an approved Hermes user service and `restart_downtime_minutes` to the expected downtime, from 1 to 60 minutes. The email shows the downtime. The completion hook schedules the restart through `systemd-run` for two minutes after the card completes. If the hook does not run inside that window, the plugin records the missed restart and does not restart late.
- **Report only:** Hermes Agent releases, OS packages and Tailscale. The plugin never updates or restarts them by mail approval.

The card body contains fixed instructions written by the plugin. It does not copy instructions out of the email. Only the worker with the matching live claim for the approved card can run `health-report update-plugins` for that card. The plugin claims the action once before starting any updates. A failure or interruption does not reset the claim. Review what changed before requesting a new approval. Cards from approvals consumed before this check was added cannot run plugin updates. Request a new report and approval for any such card that still needs work.

If the maintenance settings change after a report is sent, the offer no longer matches the current plan and cannot create a card. Send a new report instead. Review outstanding cards before changing a restart target or switching jobs.

## Uncertain delivery

The plugin records an unresolved delivery in `delivery-pending.json` before the first send attempt and removes it when the outcome is known. Check the recorded report ID in Sent Items or in the fallback provider. Do not clear this file until you know whether the report was sent. No fallback is attempted for an uncertain send. If an offer was not activated, send a new report after resolving delivery.
