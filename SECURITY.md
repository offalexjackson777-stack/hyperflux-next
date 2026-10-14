# Security Policy

## Supported Versions

| Version | Platform | Supported |
| --- | --- | --- |
| Latest 2.0.x release (currently 2.0.0) | Arch Linux x86_64 | Yes |
| Earlier 2.0.x patch releases | Arch Linux x86_64 | No - update to the latest 2.0.x |
| Development branches and unpublished builds | Any | No |

Each 2.0.x patch release replaces the previous one. Other distributions are not
supported yet.

## Report Privately

Report suspected vulnerabilities through
[GitHub private vulnerability reporting](https://github.com/offalexjackson777-stack/hyperflux-next/security/advisories/new).
Do not open a public issue for anything involving unauthorized hardware writes,
kernel memory safety, privilege escalation, secret exposure, identity leakage,
or a plausible path to those outcomes.

Include only the minimum information needed to reproduce and assess the issue.
Do not attach hardware serials, stable host identifiers, private filesystem
paths, raw HID or USB payloads, memory dumps, captures, credentials, or
arbitrary terminal/journal output. A maintainer may request a bounded,
privacy-reviewed artifact after initial triage.

## What To Expect

- The report remains private while impact and affected boundaries are assessed.
- Receipt should be acknowledged within 7 days when maintainer availability
  permits.
- Remediation and disclosure timing depend on severity, reproducibility, and
  whether the affected behavior exists in a published release.
- Security-report handling does not imply that a package, tag, or hardware
  qualification is ready.

## Security Boundaries

- Applications never receive raw receiver transport.
- One bridge owns all userspace hardware writes.
- Authorization is bound to an active receiver generation, qualified route,
  capability, deadline, transaction, and ownership lease.
- Unknown, stale, malformed, partial, or unqualified state fails closed.
- Every queue, message, journal, frame set, retry policy, and retained history
  is bounded.
- Support artifacts exclude stable identity and raw transport by default.
- Research senders and physical-test coordinators cannot enter product
  dependency graphs.

For ordinary troubleshooting, use [SUPPORT.md](SUPPORT.md), not a security
advisory.
