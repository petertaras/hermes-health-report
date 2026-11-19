# Changelog

## 0.1.0 preview

- Add profile-local health and maintenance reports with HTML previews.
- Add the seven report sections: backup, disk, plugins, Hermes Agent release, OS and security updates, memory, and services and other dependencies.
- Add delegated Graph, SMTP, Hermes profile and local delivery, with fallback only on a definite failure and a durable record of any uncertain send.
- Add single-use mail approvals for allowlisted plugin updates and a delayed Hermes restart. Hermes Agent, OS and Tailscale updates stay report-only.
- Add repeatable setup for optional scheduled maintenance, report and approval poll jobs.
