# Architecture and safety invariants

The command-line layer parses an Instagram export and binds persistent state to
the SHA-256 digest of its pending-request JSON. The Selenium layer uses a
dedicated Firefox profile and verifies the signed-in owner before inspecting a
target. DOM observation is atomic and read-only; it reports identity, message,
visibility, viewport, hit-test, and unique confirmation-control evidence.

Only the cancellation coordinator may click. It re-fetches all DOM elements,
requires `Requested`, opens the dialog, waits only for a recognized rendering
transition, revalidates the entire observation, and calls Selenium `.click()`.
It then requires `Follow` before persisting `cancelled`.

Safety invariants:

- no Follow click path exists;
- Following returns `not_pending` without opening an Unfollow dialog;
- one target-specific withdrawal explanation and one unobscured confirmation
  are required;
- stale elements are discarded;
- post-click uncertainty stops without a second click;
- challenges, login pages, checkpoints, restrictions, and automation warnings
  stop the run;
- progress and deferred sets cannot overlap;
- deferred application requires a previous `active_requested` review;
- an optional allowlist can narrow, never broaden, candidates.
