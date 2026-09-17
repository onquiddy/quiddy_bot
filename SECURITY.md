# Security Policy

Quiddy is published for source review, but production secrets and private infrastructure data must never be included in public reports.

## Reporting a vulnerability

Please report security issues privately through an official QuiddyNetwork communication channel. Do not open a public issue containing exploit details for an unpatched vulnerability.

Include the affected component, a minimal reproduction, expected impact and any relevant sanitized logs. Do not include live Discord tokens, API keys, HMAC secrets, cookies, database credentials or private user data.

## Credentials

If a credential is ever committed, pasted into a public issue or otherwise exposed, treat it as compromised and rotate it. Removing it from the latest commit does not make the old credential safe again.

## Scope

Do not perform destructive testing against production infrastructure, other users, third-party services or systems you do not own or have explicit permission to test.
