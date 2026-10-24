# Security policy

## Scope

`pcbwatch` is a local tool. By design it:

- binds to `127.0.0.1` only;
- refuses state-changing requests from other origins;
- never writes to the KiCad projects it watches;
- runs `kicad-cli` on the files you choose to watch, and nothing else.

It is **not** meant to be exposed to a network. Anyone who can reach the port
can make it render any KiCad file the user can read. Reports that need it to
be deliberately exposed are out of scope, but tell us if the docs make that
easy to do by accident.

## Reporting a vulnerability

Please **don't open a public issue**. Use GitHub's private reporting instead:
**Security → Report a vulnerability** on this repository.

Include what you ran, what you expected and what happened. You'll get an
answer as soon as possible; this is a small project maintained in spare time.

## Supported versions

Only the latest commit on `main`.
