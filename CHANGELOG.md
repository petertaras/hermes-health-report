# Changelog

## 0.1.1 preview

- Declare Hermes Agent 0.23.2 as the minimum version and disclose what the plugin accesses.
- Read SMTP and GitHub credentials through Hermes. Stop reading profile `.env` files or using GitHub CLI sign-in.
- Disable Microsoft Graph delivery and new approvals until Hermes supports bounded credentialed requests.
- Configure vaults explicitly and set Hindsight endpoints in the report or Hindsight configuration.

## 0.1.0 preview

- Add profile-local health and maintenance reports with HTML previews.
- Add the seven report sections: backup, disk, plugins, Hermes Agent release, OS and security updates, memory, and services and other dependencies.
- Add delegated Microsoft Graph, SMTP, Hermes profile and local delivery, with fallback only on a definite failure and a durable record of any uncertain send.
- Add single-use mail approvals for allowlisted plugin updates and a delayed Hermes restart. Hermes Agent, OS and Tailscale updates stay report-only.
- Add repeatable setup for optional scheduled maintenance, report and approval poll jobs.
