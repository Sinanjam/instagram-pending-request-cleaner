# Instagram Pending Request Cleaner

A local, fail-closed Selenium utility for reviewing and withdrawing **sent,
pending** Instagram follow requests from your own Instagram data export.

> [!WARNING]
> This is an unofficial community project. It is not affiliated with,
> sponsored by, or endorsed by Instagram or Meta. Web UI automation can break
> when Instagram changes its interface and may trigger account restrictions.
> The program never attempts to bypass CAPTCHA, checkpoints, challenges, or
> action blocks.

[Türkçe belge](README.tr.md)

## Safety model

- Read-only preview is the default; mutations require `--apply` and an explicit
  `APPLY` confirmation.
- The signed-in account must match `--account` using the profile URL, heading,
  and an owner-only Edit Profile control.
- A target must visibly be in `Requested` state both before and immediately
  before opening its confirmation dialog.
- A click is allowed only when one visible dialog contains the target-specific
  request-withdrawal explanation and exactly one unobscured confirmation
  control paired with Cancel.
- `Follow` is never clicked. A visible `Following` state is never unfollowed.
- JavaScript observes DOM state; only Selenium WebElement `.click()` performs
  authorized UI clicks.
- Ambiguity, stale identity, a covered control, an incomplete render, or an
  Instagram security warning stops the run.
- No telemetry, analytics, external AI service, or server upload is included.

## Supported systems

The flake declares Linux packages for `x86_64` and `aarch64`, using Firefox and
geckodriver from the same pinned Nixpkgs revision. The complete build and test
suite has been run on `x86_64-linux`; `aarch64-linux` is declared but not yet
verified on native hardware. Wayland and X11 are supported by Firefox;
Hyprland integration is optional. macOS and Windows are not packaged or tested.

## Install and run with Nix

Requirements: Nix with flakes enabled and a graphical Linux session.

```console
nix run . -- --help
nix build
nix flake check
nix develop
nix profile install .
```

No global pip installation is required. The Nix wrapper supplies Python,
Selenium, Firefox, and geckodriver. Runtime data is written to
`$XDG_STATE_HOME/instagram-request-cleaner` (normally
`~/.local/state/instagram-request-cleaner`), never to the read-only Nix store.

## Obtain the Instagram export

Use Instagram/Accounts Center's official **Download your information** flow
and request the followers/following connection data in JSON format. UI labels
and archive layout may change; consult Meta's current help pages. Keep the ZIP
private: it contains account relationship data and must not be committed.

The importer accepts:

- the downloaded ZIP containing exactly one `pending_follow_requests.json`;
- that JSON file directly; or
- an extracted export directory containing exactly one matching file.

## Dedicated Firefox profile

The application never reuses your normal Firefox profile unless you explicitly
point `--profile-dir` at it (not recommended). Initialize the dedicated profile:

```console
nix develop --command firefox --no-remote \
  --profile "${XDG_STATE_HOME:-$HOME/.local/state}/instagram-request-cleaner/firefox-profile"
```

Sign in manually, verify that the correct account is active, and close that
Firefox window. The application does not accept passwords, automate login, or
copy cookies from another profile.

## Quick start

Preview the export without opening Firefox:

```console
nix run . -- --archive /path/to/export.zip --dry-run
```

Show persistent status:

```console
nix run . -- --archive /path/to/export.zip --status
```

Inspect one exported account without changing it:

```console
nix run . -- --archive /path/to/export.zip \
  --account example_owner --inspect-user example_user_1
```

Withdraw requests in a small controlled batch:

```console
nix run . -- --archive /path/to/export.zip \
  --account example_owner --apply --limit 3
```

Review deferred accounts read-only and generate reports:

```console
nix run . -- --archive /path/to/export.zip \
  --account example_owner --deferred-review --limit 10
nix run . -- --archive /path/to/export.zip --deferred-report
```

Apply only previously reviewed `active_requested` deferred records:

```console
nix run . -- --archive /path/to/export.zip \
  --account example_owner --apply-deferred --limit 3
```

Use `--deferred-allowlist /path/to/allowlist.json` to narrow that safe candidate
set further. It cannot add an unreviewed, resolved, or manually excluded target.

## Progress, resume, and reports

Progress is keyed to the SHA-256 digest of the imported JSON. Completed and
deferred records are disjoint and validated before mutating runs. Re-running
the same archive resumes without processing completed accounts again. A changed
export requires separate state; use `--state-dir` to select it explicitly.

Local state includes `progress.json`, `deferred.json`, `actions.jsonl`, a
dedicated Firefox profile, redacted modal diagnostics, and deferred JSON/CSV/
Markdown reports. These files can contain usernames and must remain private.

## Hyprland (optional)

Firefox is launched with the unique Wayland app ID
`ig-request-cleaner-firefox`. Copy and adapt
[`examples/hyprland.conf`](examples/hyprland.conf); change workspace `9` to any
workspace. The example uses `silent`, `no_initial_focus`,
`focus_on_activate = false`, and `render_unfocused = true`. Do not create a
broad rule for class `firefox`, which would affect personal windows.

## Troubleshooting

- **Account verification failed:** open the dedicated profile manually and
  confirm that `--account` is signed in and its own profile shows Edit Profile.
- **Marionette/geckodriver error:** close only the cleaner-owned Firefox,
  confirm no other process owns the dedicated profile, then retry. Do not kill
  unrelated Firefox processes.
- **Rendering timeout:** keep the Hyprland `render_unfocused` rule or temporarily
  keep the cleaner workspace visible. The program will not click an invisible
  button.
- **Ambiguous dialog / empty dialog:** stop and inspect diagnostics in the state
  directory. Do not weaken message or identity checks.
- **Challenge, CAPTCHA, checkpoint, action block:** stop and resolve it manually
  through Instagram. The tool will not bypass it.
- **Archive rejected:** request JSON data and ensure there is exactly one
  `pending_follow_requests.json`.

## Limitations and privacy

Instagram has no public supported API for this workflow; selectors and wording
can change without notice. “Page isn't available” does not prove that an
account was deleted, renamed, suspended, or deactivated. Such targets remain
unknown. Private accounts, localization, gradual UI rollouts, or browser
throttling may require manual review.

All decisions run locally. Logs deliberately omit cookies, tokens, passwords,
and full page text, but usernames and action results are still personal data.
Back up and protect the state directory. See [SECURITY.md](SECURITY.md).

## Development

```console
nix develop
python -m unittest discover -s tests -v
python scripts/privacy_scan.py
gitleaks detect --no-git --source . --redact
```

Real Instagram sessions are never used in CI; browser tests use synthetic
local HTML. Contributions must preserve fail-closed behavior. See
[CONTRIBUTING.md](CONTRIBUTING.md). Licensed under the [MIT License](LICENSE).
