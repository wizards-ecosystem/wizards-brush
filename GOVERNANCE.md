# Governance

The Wizard's Brush is maintained in the open. The maintainers are responsible
for technical direction, release integrity, community safety, and the
Apache-2.0 licensing boundary.

## How decisions are made

Small fixes and routine improvements are decided through pull-request review.
Changes that alter persisted formats, security boundaries, licensing posture,
supported platforms, or major workflows should begin with a public issue so the
problem and tradeoffs are visible before implementation.

Maintainers seek rough consensus, then make the final call when a decision is
needed. Decisions should favor user safety, backward compatibility, a contained
local runtime, understandable operation, and evidence from tests or measured
hardware behavior.

## Roles

- **Contributors** report issues, improve documentation, propose designs, and submit changes.
- **Reviewers** provide consistent technical review and may be asked to own a subsystem.
- **Maintainers** merge changes, manage releases and repository settings, handle conduct and security reports, and enforce project policy.

Sustained, constructive contribution is the path from contributor to reviewer
or maintainer. Access is granted conservatively and may be removed after a long
period of inactivity or a serious policy breach.

## Releases

A maintainer owns each release, verifies [the release checklist](docs/release-checklist.md),
reviews third-party and model-license changes, updates the changelog and version,
and creates the signed or otherwise attributable tag. No contributor should
represent an unmerged build as an official project release.

## Project assets and independence

The repository, release credentials, issue tracker, and security reporting
channel are project infrastructure. No model vendor, GPU provider, or upstream
project is implied to sponsor or endorse The Wizard's Brush.

Governance changes use the same public pull-request process as code changes.
