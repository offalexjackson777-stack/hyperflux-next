# Security Policy

HyperFlux Next is pre-release and currently has no supported public version.

Security-sensitive design rules are enforced from the first commit:

- applications never receive raw receiver transport;
- one bridge owns all userspace hardware writes;
- authorization is bound to an active receiver generation and ownership lease;
- unknown, stale, malformed, or unqualified state fails closed;
- every queue, message, journal, and frame set is bounded;
- support artifacts exclude stable host identifiers, hardware serials, raw
  reports, private paths, and arbitrary logs by default;
- research senders and Stage-A operations cannot enter product dependency
  graphs.

Do not open a public issue for a vulnerability that could enable unauthorized
hardware writes, kernel memory corruption, privilege escalation, or private
identity disclosure. Private reporting will be configured before publication.

