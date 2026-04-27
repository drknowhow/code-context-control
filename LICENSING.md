# Licensing FAQ

This document answers the most common questions about how Code Context
Control (C3) is licensed and where the project is headed. It is informal
guidance — the authoritative documents are [`LICENSE`](LICENSE),
[`NOTICE`](NOTICE), and (when introduced) [`EULA-PRO.md`](EULA-PRO.md).

## TL;DR

- **Right now (2.x releases):** Apache License 2.0. Free for any use,
  including commercial. Modify, fork, redistribute — all permitted.
- **Future plan:** A paid Pro tier with license-key activation. Future
  major versions may switch to a source-available license (e.g. BSL 1.1).
- **Already-published 2.x versions stay Apache-2.0 forever.** Anything
  you've installed today is yours under permissive terms in perpetuity.

## Common questions

### Can I use C3 at work?

**Yes.** Apache-2.0 permits commercial use, including inside companies of
any size. No fee, no registration. Use it freely.

### Can I fork it?

**Yes.** Apache-2.0 permits forks and derivative works. Two requests as a
courtesy (not legal requirements):

1. Use a distinct name — don't present your fork as an official C3
   distribution. ("C3" and "Code Context Control" are trademarks; see
   [`NOTICE`](NOTICE).)
2. If you're forking to build a competing commercial product, please
   email dtselenc@gmail.com first. We can almost certainly work something
   out, and direct competition is genuinely the only thing we'd ask you
   to think twice about.

### Can I redistribute / repackage / re-host C3?

**Yes**, under Apache-2.0 terms (preserve the LICENSE and NOTICE files,
state your changes). Same courtesy request as above: please don't
repackage as a commercial competing product without talking first.

### Will the license change in future versions?

**Possibly.** We're considering a source-available license (such as
**Business Source License 1.1** with a 4-year automatic conversion to
Apache-2.0) for future major versions (3.x onwards). This would:

- **Allow:** internal commercial use, modification, redistribution for
  non-competing purposes, viewing source.
- **Restrict:** offering C3 (or substantially similar fork) as a hosted
  service that competes with the official Code Context Control offering.
- **Auto-revert:** every BSL release converts to Apache-2.0 four years
  after publication.

If we make this change, the affected version will be a major version bump
(3.0.0) and clearly announced in the changelog. We will not retroactively
relicense any 2.x release.

### What happens to my install if you relicense?

Nothing. Your installed version retains the license it was published
under. If you have v2.28.0 installed, it's Apache-2.0 forever. Upgrading
is opt-in — you decide whether to accept any new terms in future versions.

### Is this a "rug pull"?

We don't think so, but you get to decide. We're declaring our intent
**before** building a community that depends on permissive terms,
**not after**. That's the difference between a rug pull and an honest
license selection. If we *do* relicense in the future, we will:

- Bump the major version (signals breaking change to license terms).
- Document the rationale in the changelog.
- Keep the prior major version available and supported for a
  reasonable transition window.
- Honor every Apache-2.0 right granted under prior releases.

### What about the Pro tier?

The Pro tier doesn't exist yet. When it does, it will be **additive**:
new features behind a license-key activation gate. We are explicitly not
planning to take currently-free features and put them behind the Pro
paywall in already-published versions. Future versions may introduce
new features that ship Pro-only — those features simply won't exist in
the OSS version.

The Pro tier (when introduced) will be governed by [`EULA-PRO.md`](EULA-PRO.md).

### How does the Pro tier coexist with the OSS license?

The Pro features will live in the same repository and follow the same
license as the rest of the codebase, BUT they will require a valid
license key to activate. This is the model used by Sentry, GitLab, and
others: the *code* is permissive, but the *license keys* and the
*hosted issuance service* are commercial.

### Who do I talk to about licensing questions?

Email **`dtselenc@gmail.com`** with subject `[c3-licensing]` and we will
respond within a few business days.
