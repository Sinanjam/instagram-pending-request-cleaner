# Contributing

Contributions are welcome when they preserve the fail-closed model.

1. Never commit an Instagram export, profile, cookie database, screenshot,
   action log, real username, email address, token, or absolute home path.
2. Use only names such as `example_owner` and `example_user_1` in tests.
3. Add an offline regression test for every selector or decision change.
4. Never replace Selenium `.click()` with JavaScript click injection.
5. Never add CAPTCHA/checkpoint bypasses, automatic login, telemetry, or a
   broad Firefox process/window rule.
6. Run the commands below before opening a pull request.

```console
nix flake check
nix build
nix develop --command python scripts/privacy_scan.py
nix develop --command gitleaks detect --no-git --source . --redact
```

Real Instagram sessions are outside CI and must not be required for a test.
By contributing, you agree that your contribution is licensed under MIT.
