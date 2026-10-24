# Contributing

Changes should make the next installation easier to understand, retry and recover. Keep the Python runtime dependency-free and preserve existing data, mount checks, private credentials and VPN isolation.

## Before opening a change

1. Describe the actual failure and the environment where it happens. Redact credentials, public addresses and personal paths from logs.
2. Inspect the existing implementation and make the smallest coherent fix. Keep stage boundaries explicit.
3. Add a regression test when the failure involves safety, ownership, retry behavior or an application API contract. A mock verifies our handling of that contract; it does not verify the application itself.
4. Run the checks below with Python 3.11 or newer. Compose validation also needs Docker Compose 2.24 or newer; it does not start services.

```bash
python3 -m unittest discover -s tests -v
python3 scripts/check-release.py .
python3 scripts/validate-compose.py
git diff --check
```

Use a fresh disposable Linux VM for changes to provisioning, package installation, storage permissions, service startup or live API wiring. Use synthetic credentials and test files. Record the actual application versions, commands, readbacks and limits in the pull request. Never test a destructive recovery against somebody's personal library.

## What a release must say

- Identify the exact source commit and application image digests tested.
- Separate unit tests, live startup, API readback, file transfers and restore evidence.
- State untested hardware, commercial providers and manual enrollment requirements.
- Scan both the source and the final archives for private files and recognizable secrets.

New storage or migration behavior needs a reviewed plan and a preservation test before it belongs in a release. Silent adoption, recursive permission changes, automatic disk formatting and VPN bypasses are outside the current design.

For vulnerabilities, use the private route in [SECURITY.md](docs/SECURITY.md).
