# Security policy

## Supported versions

Security updates are provided exclusively for the latest commit on the `main` branch. Older commits and unmerged development branches do not receive backported security patches.

| Version / Branch | Supported |
|---|---|
| `main` | Yes |
| `develop` | Active development only |
| Older releases | No |

## Reporting a vulnerability

Do not report security vulnerabilities in public GitHub issues, discussions, or Telegram chats.

Use GitHub Private Vulnerability Reporting to submit findings securely:

[Submit a Security Advisory](https://github.com/HotaruProject/Hotaru/security/advisories/new)

If you cannot use GitHub Advisories, contact the project maintainers directly with PGP-encrypted details.

### What to include in your report

To help us verify and resolve the issue quickly, include:
- A clear description of the vulnerability and its potential impact.
- Step-by-step reproduction instructions or a minimal proof of concept script.
- The environment details (OS, Python version, Docker or host installation).
- Any proposed remediation or mitigation steps, if available.

### Vulnerability scope

High-priority issues for Hotaru include:
- Sandbox and namespace escapes: breaking out of `.hmod` worker isolation or accessing the host filesystem outside the sandbox directory.
- Credential leaks: unauthorized exfiltration of master vault keys, Telegram session strings, or auth tokens.
- Privilege escalation: bypassing the capability model (`mt`, `state`, `net`) or triggering unauthorized RPC methods.
- Sandbox firewall circumvention: defeating network blocking or IPC filters for restricted modules.

Out of scope:
- Attacks requiring existing root access or host compromise.
- Denial-of-service against third-party network providers.
- Account restrictions or bans applied by Telegram for spam or terms violations.

## Response timeline

- Acknowledgment: within 48 hours of submission.
- Assessment: initial severity rating and reproduction within 5 business days.
- Patch and release: coordinated disclosure schedule agreed with the reporter once a fix is verified.
