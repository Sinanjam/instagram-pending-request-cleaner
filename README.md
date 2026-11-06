# Instagram Pending Request Cleaner

**English** | [Türkçe](README.tr.md)

A beginner-friendly, local tool for checking and withdrawing **follow requests you sent** on Instagram. It reads your **own Instagram data export**. It can inspect requests without changing anything; withdrawing one requires deliberate authorization.

> [!WARNING]
> **Unofficial tool:** not affiliated with Instagram or Meta. Browser automation may trigger account restrictions. Instagram can change its interface at any time. No CAPTCHA, checkpoint, challenge, or action-block bypass is provided. This program deliberately **stops when it is unsure**.

## Start here: what do I actually need?

Think of the process as **five small steps**:

1. Install/enable **Nix with flakes** on a graphical Linux system.
2. Obtain **your own Instagram export in JSON format**.
3. Open a **separate Firefox profile**, sign in yourself, and close that window.
4. Use `--dry-run` (just look) and `--status` (just count).
5. Only if you really want to withdraw requests, use `--apply --limit 3` (small real batch).

**Vocabulary for absolute beginners:**

| Term | What it means |
| --- | --- |
| Pending / Requested | You sent a follow request; the other account has not accepted it yet. |
| Cancel / withdraw | Take back **your own outgoing** follow request. |
| Deferred | Set aside because the program **could not safely determine** what to do. Not canceled. |
| Dry run | A rehearsal: **no Instagram changes**. |
| Profile | Here, a separate Firefox storage folder containing your signed-in session. |
| Parameter / flag | A word like `--status` that tells the program which task to perform. |

## Step 1 — Open the correct folder

The examples assume your copy lives in `~/Projects/instagram-pending-request-cleaner`. If it does not, go to wherever your `flake.nix` file lives.

```fish
cd ~/Projects/instagram-pending-request-cleaner
nix run . -- --help
```

**How to read that command:**

- `nix run` = launch an app using Nix.
- `.` = use the project in the **current folder**.
- The standalone `--` = the next options go to the **app**, not to Nix.
- `--help` = show the app's instructions.

If you have not downloaded the repository, clone it once you have GitHub access:

```fish
git clone https://github.com/OWNER/instagram-pending-request-cleaner.git
cd instagram-pending-request-cleaner
```

Replace `OWNER` with the GitHub repository owner. A private repository requires access. Nix may download/build dependencies the first time.

**Supported environment:** graphical Linux; `x86_64-linux` has been built/tested. `aarch64-linux` is defined but not verified on native ARM hardware. Firefox supports Wayland/X11; Hyprland is optional. Windows and macOS are not packaged or tested here. No global `pip install` is needed.

## Step 2 — Download your Instagram data

In Instagram **Accounts Center**, use **Download your information**, select the followers/following connection information, and request the **JSON** format (not HTML). Instagram may change its menu labels.

The app accepts:

- A ZIP with exactly one `pending_follow_requests.json` inside; **or**
- The JSON file itself; **or**
- An extracted folder containing exactly one such JSON file.

The ZIP contains real usernames. **Do not commit it to GitHub or attach it to a public issue.** Keep it outside this repository, for example in `Downloads`.

## Step 3 — Point Fish at the file, and set your username

The lines below are for the **Fish shell**:

```fish
set archive "$HOME/Downloads/instagram-export.zip"
set account example_owner
ls -lh "$archive"
```

Read them slowly:

1. `set archive ...`: give your ZIP's path a short nickname, `$archive`.
2. `set account example_owner`: replace `example_owner` with **your own Instagram username**, without `@`. The nickname will be `$account`.
3. `ls -lh "$archive"`: check if that ZIP actually exists. Fix the path if you get “No such file.”

**Important Fish mistake:** `set --archive ...` is **wrong**. Fish variables are set using `set archive ...`. You use the `--archive` parameter **later**, in the `nix run` command.

If you use **Bash/Zsh** instead of Fish, set the two variables like this:

```bash
archive="$HOME/Downloads/instagram-export.zip"
account="example_owner"
ls -lh "$archive"
```

The later `"$archive"` and `"$account"` examples work in both shells once those variables are set.

## Step 4 — One-time separate Firefox sign-in

**Why?** The program must inspect Instagram as you, but should not interfere with your everyday Firefox session. You sign in manually; the program never asks for your password.

**Fish-compatible setup:**

```fish
set state_root "$HOME/.local/state"
if set -q XDG_STATE_HOME
    set state_root "$XDG_STATE_HOME"
end
set state_dir "$state_root/instagram-request-cleaner"
mkdir -p "$state_dir/firefox-profile"
nix develop --command firefox --no-remote --profile "$state_dir/firefox-profile"
```

- `state_dir` is where the app keeps its local files.
- `mkdir -p` creates the folder if necessary.
- The final line opens a **dedicated Firefox**.

In that window, sign into Instagram, verify your account, and **fully close the dedicated Firefox**. This setup usually only needs repeating if the login expires.

**Bash/Zsh version:**

```bash
state_root="$HOME/.local/state"
if [ -n "$XDG_STATE_HOME" ]; then state_root="$XDG_STATE_HOME"; fi
state_dir="$state_root/instagram-request-cleaner"
mkdir -p "$state_dir/firefox-profile"
nix develop --command firefox --no-remote --profile "$state_dir/firefox-profile"
```

Do not pass your normal personal Firefox profile to `--profile-dir`. GTK messages about missing resize cursors are often harmless theme warnings **if the Firefox window opens and works**.

## Step 5 — Safest first commands: preview and status

**Preview the ZIP without opening Firefox:**

```fish
nix run . -- --archive "$archive" --dry-run
```

This reads the archive and prints up to 30 names plus the remaining count. **It does not click anything on Instagram.**

**Count locally saved results:**

```fish
nix run . -- --archive "$archive" --status
```

This shows canceled, no-longer-pending, deferred, and queued counts. It also **does not open Firefox**.

> [!IMPORTANT]
> Already used an older version? A new install may have a **different state folder** and might not know about old cancellations. Check `--status` and ideally download a **fresh Instagram export** before any real `--apply` run. Do not blindly repeat changes against an old export.

## Step 6 — Inspect one target without clicking

```fish
nix run . -- --archive "$archive" --account "$account" --inspect-user example_user_1
```

Replace `example_user_1` with an actual username **from the export**. The app checks the signed-in owner and reads that target's state, but **does not press Follow or Unfollow**.

## Step 7 — Actually withdraw requests (start with THREE)

This is the real action; do **not** run it unless you mean to withdraw outgoing requests:

```fish
nix run . -- --archive "$archive" --account "$account" --apply --limit 3
```

Normally the terminal asks you to type **`APPLY`** exactly. That is your authorization.

What happens next?

1. The tool checks that the signed-in account matches `--account`.
2. It checks the target visibly says **Requested**.
3. It opens the confirmation and verifies a **target-specific request-withdrawal explanation**.
4. It uses a regular Selenium click only on the verified confirmation control.
5. It verifies the final **Follow** state before recording success.
6. If any of that is ambiguous, it **stops** instead of guessing.

`--limit 3` = at most three target accounts are **attempted**. It does not promise exactly three successful cancellations. You may resume later using the **same archive and state folder**; already completed entries are skipped.

## Step 8 — What is “deferred,” and what do I do with it?

“Deferred” means **set aside**, not canceled. Examples: unavailable profile, unreadable follow state, confusing confirmation dialog.

**Review up to five deferred accounts without changing their Instagram state:**

```fish
nix run . -- --archive "$archive" --account "$account" --deferred-review --limit 5
```

This is read-only **on Instagram**, but it **does write local classification records**.

**Generate local JSON/CSV/Markdown reports from those records:**

```fish
nix run . -- --archive "$archive" --deferred-report
```

**Only for deferred accounts previously verified as `active_requested`, try a small real batch:**

```fish
nix run . -- --archive "$archive" --account "$account" --apply-deferred --limit 3
```

This is **not** a “force everything” option. All safety checks still apply. Never weaken an ambiguous Unfollow-dialog check.

## The full option dictionary — every parameter explained

Every fragment below is an option for `nix run . --`, **not** a complete standalone shell command. Ordinary commands need `--archive "$archive"`; browser-based commands also need `--account "$account"`. Some modes are mutually exclusive.

### Everyday flags

| Flag | Plain-English explanation | Example |
| --- | --- | --- |
| `-h` / `--help` | “Show me the instruction sheet.” | `--help` |
| `--archive PATH` | “My ZIP/JSON/export folder is here.” Required for normal commands. | `--archive "$archive"` |
| `--account NAME` | “This is the account that must be logged in.” Required for browser modes. | `--account "$account"` |
| `--dry-run` | “Read my export but make **zero Instagram changes**.” | `--dry-run` |
| `--status` | “Show counts from saved records.” No Firefox. | `--status` |
| `--apply` | “Try to withdraw requests for real.” Usually prompts for `APPLY`. | `--apply` |
| `--limit N` | “Attempt at most N accounts this time.” `0` = **no limit**. Start with 1–3. | `--limit 3` |
| `--state-dir PATH` | “Save counters, logs, reports and default Firefox profile here.” | `--state-dir "$HOME/cleaner-state"` |
| `--profile-dir PATH` | “Use this **separate** Firefox profile folder.” | `--profile-dir "$HOME/cleaner-profile"` |

### Inspecting and handling deferred accounts

| Flag | Plain-English explanation | Example |
| --- | --- | --- |
| `--inspect-user NAME` | “Check this exported person's state, without clicking.” | `--inspect-user example_user_1` |
| `--diagnose` | “Describe the **structure** of export entries the parser could not understand.” It redacts values; it does not fix the file. | `--diagnose` |
| `--defer-user NAME` | “Put this exported account aside in local records.” Does not open Instagram; **does change local state**. | `--defer-user example_user_1` |
| `--deferred-review` | “Read unreviewed deferred profiles and save their classification.” No follow changes. | `--deferred-review --limit 5` |
| `--deferred-report` | “Turn saved deferred classifications into JSON, CSV and Markdown reports.” No browser. | `--deferred-report` |
| `--apply-deferred` | “Try canceling only previously verified `active_requested` deferred records.” **Real operation**; rechecks safety. | `--apply-deferred --limit 3` |
| `--deferred-allowlist PATH` | “Make `--apply-deferred` consider only users in this validated, export-linked JSON list.” Cannot add unreviewed users. | `--deferred-allowlist /path/to/allowlist.json` |
| `--reconcile-user NAME` | “Read one deferred profile; if no request is **definitely** pending, update local records.” No follow clicks. | `--reconcile-user example_user_1` |
| `--retry-deferred` | “Use the older `--apply` flow on deferred records.” **Advanced**; prefer review + apply-deferred. | `--apply --retry-deferred --limit 1` |

### Advanced controls

| Flag | Plain-English explanation | Example |
| --- | --- | --- |
| `--defer-unverified` | With `--apply`, save certain unresolved/unavailable cases as deferred, **not canceled**. Unsafe ambiguity still stops the run. | `--apply --defer-unverified` |
| `--min-delay SECONDS` | Wait at least this long between targets; minimum **5** seconds, default **8**. | `--min-delay 8` |
| `--max-delay SECONDS` | Wait at most this long between targets; default **15**, never less than the minimum. | `--max-delay 15` |
| `--non-interactive` | **Advanced unattended mode:** omits the usual typed `APPLY` prompt in permitted workflows. Use only with prior authorization and safeguards. | `--apply --defer-unverified --non-interactive` |
| `--deadline DATE` | Stop starting new accounts after a **future, timezone-aware** date/time. Legacy `next-istanbul-0800` is supported. | `--deadline "2030-01-01T08:00:00+03:00"` |

**Combinations that are NOT allowed:**

- `--apply` and `--dry-run` cannot be combined.
- `--status` is a standalone mode; don't mix it with action/review modes.
- Choose **one** of `--deferred-review`, `--deferred-report`, `--apply-deferred`.
- `--deferred-allowlist` works only with `--apply-deferred`.
- `--retry-deferred` / `--defer-unverified` require `--apply`.
- `--non-interactive` is restricted and is **not** permission to bypass safety checks.
- A `--limit` of `0` means **all candidates**. Avoid it for your first actual run.

## Logs: how do I see what happened?

There are **two kinds** of information:

**A. Quick status (the simplest choice):**

```fish
nix run . -- --archive "$archive" --status
```

**B. Saved action history:** (use the `state_dir` variable defined in Step 4)

```fish
tail -n 30 "$state_dir/actions.jsonl"
```

This shows the last 30 action records. To see new entries as they are written:

```fish
tail -f "$state_dir/actions.jsonl"
```

Press **Ctrl+C** to stop watching the log. A cleaner running in a **different terminal** keeps running. The file may not exist until the app has saved actions.

To **show and save** a harmless preview's terminal output at the same time:

```fish
nix run . -- --archive "$archive" --dry-run 2>&1 | tee "$state_dir/preview.log"
```

**Keep logs private.** They can contain Instagram usernames.

Default storage: `~/.local/state/instagram-request-cleaner`, unless `XDG_STATE_HOME` or `--state-dir` overrides it.

| Local file | Why it exists |
| --- | --- |
| `progress.json` | Successfully completed entries. |
| `deferred.json` | Unresolved entries set aside. |
| `actions.jsonl` | Recorded action events (one JSON object per line). |
| `deferred-review.json` | Deferred review results. |
| `deferred-review-report.csv` / `.md` | Human-readable review reports. |
| `firefox-profile/` | **Sensitive** dedicated Firefox session. |
| `last-modal-diagnostic.json` and related files | Diagnostics after unusual UI behavior; may contain identifiers. |

## Troubleshooting: see a message, take this step

| Problem | Simple fix |
| --- | --- |
| Fish says `set: --archive: unknown option` | Use `set archive ...`, **not** `set --archive ...`. |
| Fish complains about Bash variable syntax | Use the **Fish** Firefox setup in Step 4, not the Bash version. |
| “No such file” | Check `ls -lh "$archive"` and fix the path. |
| Prompt asks for `APPLY` | Actual changes may follow. Do not type it unless you intend them. |
| Wrong account / identity check fails | Open the **dedicated** Firefox profile, sign in to the correct account, and fully close it. |
| Firefox / Marionette / geckodriver error | Close only cleaner-owned Firefox; do not kill your regular Firefox. |
| Invisible dialog / rendering timeout | In Hyprland, render the unfocused workspace or briefly make it visible. Never force invisible clicks. |
| Ambiguous Unfollow dialog | **Stop.** Do not guess whether it means withdrawing a request or unfollowing someone. |
| CAPTCHA, challenge, checkpoint, action block | Stop automation; handle the Instagram account manually. |
| “Page isn't available” | Unknown state: **not proof** of deactivation, deletion or username change. |
| GTK resize-cursor warning | Usually cosmetic if Firefox itself opens normally. |

**Exit codes:** `0` = success; `1` = operational/transient error; `2` = bad data/configuration; `3` = **safety stop**. **Do not automatically restart after exit 3.** Check output and logs first.

## Hyprland (optional)

The Selenium Firefox has its own app ID `ig-request-cleaner-firefox`. See [examples/hyprland.conf](examples/hyprland.conf). Change the example workspace `9` if desired; `render_unfocused` helps keep the dialog rendering while the workspace is not visible. Do **not** create a broad rule matching normal `firefox`, or your everyday browser may be affected.

## For developers

Run from the folder containing `flake.nix`:

```fish
nix build
nix flake check
nix develop --command python -m unittest discover -s tests -v
nix develop --command python scripts/privacy_scan.py
nix develop --command gitleaks dir . --redact
nix develop --command gitleaks git . --redact --log-opts="--all"
```

`nix build` packages the app; `nix flake check` runs Nix checks; `nix develop` supplies development tools; privacy/Gitleaks scans help detect unsafe files. CI tests use **synthetic data, not live Instagram accounts**. See [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md), and the [MIT License](LICENSE).

## Important limitations

This uses the unofficial Instagram **web interface**, not a supported API. Layout, translations, restrictions and browser behavior can change. The program intentionally refuses uncertain clicks; it **cannot guarantee every request will be withdrawn**. Never publish actual exports, Firefox profiles, action logs or real account identifiers.

## Vibe coding disclosure

**This entire project is vibe-coded.** Its code, tests and documentation were created and iteratively refined using AI-assisted coding prompts rather than an exclusively hand-written development process. Passing tests is **not** proof of a formal security audit or a guarantee of correctness. Review the source, keep backups, start with tiny batches, and use it at your own risk.
