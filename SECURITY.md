# Security Policy

Thanks for helping keep `AlpacaPlatform` safe.

This repository is a bounded Alpaca paper and shadow execution gateway. Please do **not** open a public issue for vulnerabilities involving credentials, broker access, cloud resources (artifact or receipt stores), risk-gate or policy-gate receipts, or other secret material.

## Reporting a Vulnerability

- Contact the maintainer directly at GitHub: `@Pigbibi`.
- Private vulnerability reporting is not currently enabled for this repository, so please report directly to the maintainer rather than opening a public issue.
- Include the repository name, affected commit or branch, environment details, and exact reproduction steps.

## Secret and Credential Exposure

If you suspect Alpaca API keys/secrets, GCP service-account keys, or other broker/cloud credentials were exposed:

1. Rotate the exposed secrets immediately.
2. Pause any scheduled jobs or deployments that could be affected.
3. Share only the minimum evidence needed to reproduce the issue.

## Scope Notes

Security fixes should stay minimal and focused. Please avoid bundling unrelated refactors with a security report or patch. This repository currently implements only the P5 `SHADOW` stage (no broker connectivity, no credentials, no order submission); P4 `PAPER_DRY_RUN` will use separate paper-only credentials and endpoints once implemented — scope any report or fix to the stage actually affected.
