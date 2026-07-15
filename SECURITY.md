# Security Policy

## Supported Versions

Security fixes are applied to the latest stable release and, before `1.0.0`,
the current `1.0.0` release candidate. Older images and arbitrary commits are
not supported security branches.

| Version | Supported |
| --- | --- |
| Latest stable release | Yes |
| `1.0.0-rc.x` while active | Yes |
| Older releases | No |

## Reporting A Vulnerability

Do not open a public issue for a suspected vulnerability. Use GitHub's private
[security advisory form](https://github.com/Kha-kis/manga-arr/security/advisories/new)
and include:

- the affected version or commit;
- deployment details relevant to the issue;
- reproduction steps or a minimal proof of concept;
- the expected and observed security boundary;
- any known workarounds.

Do not include real API keys, passwords, setup tokens, private tracker URLs, or
unencrypted copies of another user's data. Reports are handled on a best-effort
basis by the project maintainer. There is currently no paid bug-bounty program.

## Deployment Boundary

Mangarr is designed to run behind Docker network controls and, for remote
access, an HTTPS reverse proxy. The supported security posture assumes:

- `/config` is private to the operator and writable only by the configured
  container UID/GID;
- the browser administrator and API key are both configured;
- the application is not directly exposed to the public internet;
- the database and `/config/.mangarr-secret-key` are backed up together;
- operators install supported images and review release notes before upgrade.

Compromise of the Docker host, access to `/config`, or access to the container
control plane is equivalent to Mangarr administrator access.
