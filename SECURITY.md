# Security and Privacy

Grocery Cockpit is a local-first personal dashboard. Treat the local database and browser profiles as sensitive.

## Sensitive Files

Never publish:

- `config.json`
- `data/`
- `browser-profile/`
- `browser-profiles/`
- provider setup/probe logs
- exported order history
- screenshots from real accounts
- tunnel URLs or dashboard access keys

The repository ignores these by default, but contributors should still review changes before publishing.

## Reporting Issues

If you find a security or privacy issue, open a private advisory if the repository host supports it. If not, contact the maintainer privately before filing a public issue.

Please include:

- affected version or commit
- what data or access may be exposed
- reproduction steps using demo data where possible
- suggested mitigation if known

## Provider Accounts

Browser-based provider probes may use logged-in sessions on your own machine. Grocery Cockpit should not ask users to share provider credentials with maintainers, hosted demos, or public services.

## Network Exposure

The dashboard uses a private access key when enabled. If you expose it through a tunnel, reverse proxy, or public hosting, use HTTPS and a strong private key. Public demos should run only with seeded data.
