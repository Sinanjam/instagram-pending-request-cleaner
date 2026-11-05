# Security policy

## Reporting a vulnerability

Do not attach Instagram exports, Firefox profiles, cookies, screenshots, audit
logs, or real usernames to a public issue. Open a minimal issue containing only
synthetic reproduction steps and ask maintainers for a private reporting
channel. Until a private channel is published, do not disclose exploitable
details or personal data in this repository.

## Security boundaries

The application controls only its dedicated Firefox profile. It does not store
passwords, automate login, copy sessions, bypass Instagram security controls,
or send telemetry. State and diagnostics remain local under XDG state storage.

An `--apply` run is fail-closed, but it still depends on an unofficial and
changeable website. Back up state, use small limits, inspect the first actions,
and stop on any unexpected wording or account state.

Supported security fixes target the current main branch. There is no guarantee
that older releases remain compatible with Instagram's current UI.
