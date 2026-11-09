# Security policy

Baler holds building data and can command equipment, so we treat security
reports as a priority.

## Reporting a vulnerability

**Please report privately. Do not open a public issue, pull request or
discussion for a suspected vulnerability.**

Use GitHub's private vulnerability reporting: on this repository, open the
**Security** tab and choose **Report a vulnerability**. Only the maintainers
can see the report, and we can work on a fix with you there in a private
advisory before anything is public.

If that button isn't available to you, open a public issue that says only
that you have a security report and would like a private way to send it.
Leave out what is affected and how. A maintainer will reply with a private
channel.

A useful report says:

- which part of Baler is affected (service, endpoint or file) and which
  commit or deploy you tested;
- what an attacker could do, and what access they need first (none, a
  viewer account, an edge credential, and so on);
- steps to reproduce, or a proof of concept, kept to what demonstrates the
  issue.

Please don't test against an install you don't run, don't access or change
other people's data, and don't run denial-of-service tests against shared
installs.

## What happens next

- We acknowledge a report within 5 working days.
- We confirm or rule it out, agree a fix plan with you, and keep you posted
  in the private advisory.
- Once a fix is released, we publish the advisory, crediting you if you
  want to be credited.

## Supported versions

Baler is deployed from `main`. Security fixes land on `main`; there are no
maintained release branches. If you run your own install, update to the
latest `main` to get fixes.

## Scope

In scope: everything in this repository, including the gateway and its
auth policy, the APIs, the UI, the edge-node connection path, the deploy
scripts and the CI workflows.

Out of scope: vulnerabilities in third-party dependencies that are already
public (report those upstream; our automated scanning picks up their
advisories), and findings that need an already compromised host or admin
account.

## How the repository is scanned

Dependency, secret, container image and static analysis scans run in CI.
`docs-site/pages/security-scanning.mdx` explains what each scan covers,
which ones block a pull request, and how a maintainer records an accepted
risk.
