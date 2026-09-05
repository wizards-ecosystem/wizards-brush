# Contributing to The Wizard's Brush

Thank you for helping make a dependable creative tool. Small, focused changes
with a clear user outcome and evidence are the easiest to review and maintain.

Read the [Code of Conduct](CODE_OF_CONDUCT.md), [governance](GOVERNANCE.md), and
[security policy](SECURITY.md) before participating. Ordinary support requests
belong in the channels described by [SUPPORT.md](SUPPORT.md).

## Ways to contribute

- Reproduce and reduce bugs, especially across GPU and WSL configurations.
- Improve setup, error messages, accessibility, or documentation.
- Add tests around queue recovery, persisted parameters, model compatibility, or input boundaries.
- Propose a model or generator only with hardware measurements and a current license review.
- Review pull requests and validate workflows on hardware the maintainers do not have.

Security vulnerabilities must not be filed as public issues. Follow
[SECURITY.md](SECURITY.md).

## Choose a setup

For full application development:

```bash
cp .env.example .env
make install
make doctor
make check
```

`make install` omits model weights. Run `make models` only when your change
requires real generation. Use `make dev` for FastAPI and Vite reload servers.

Documentation, API, and unit-test contributors can install the smaller
torch-free CI environment:

```bash
make bootstrap
source scripts/project-env.sh
source scripts/tool-versions.env
uv python install "$PYTHON_VERSION"
uv sync --only-group test --frozen
(cd frontend && npm ci)
make check
```

All project commands keep writable state inside the checkout. Do not replace
that behavior with user-home caches or global package installations.

## Development workflow

1. Search existing issues and pull requests.
2. Open an issue before changing a persisted format, public API, security boundary, supported platform, license posture, or major workflow.
3. Create a focused branch and keep unrelated cleanup out of the change.
4. Add or update tests for behavior and update the relevant public guide.
5. Run `make check`. Use `make release-check` for setup, release, storage, dependency, or public-documentation changes.
6. Complete the pull-request template with the commands and hardware paths you actually validated.

The frontend format check is enforced. Run `npm run format` from `frontend/`
when Prettier reports a difference.

## Architectural contracts

The contributor-facing overview is in [docs/architecture.md](docs/architecture.md).
These contracts deserve special care:

- Generator controls come from the backend registry; do not hardcode a second control catalogue in React.
- Job kinds, variant names, asset generator strings, and saved parameters are persisted API. Add compatibility instead of renaming them.
- Database migrations are append-only modules. Never reorder or edit a migration that may have shipped.
- Heavy CUDA and imaging imports stay lazy so the API and CI suite remain torch-free.
- Local GPU operations share one lock. New work must not create an uncoordinated GPU path.
- Router validation happens before user input is persisted, because rerun replays stored parameters.
- Asset deletion remains soft until explicit purge.
- Secrets, generated Remote GPU runners, models, output media, databases, and private research notes are never committed.

## Licensing and provenance

The Wizard's Brush is Apache-2.0. By intentionally submitting a contribution,
you represent that you have the right to submit it and agree that it is provided
under the project's Apache-2.0 license. Mark material that is not intended as a
contribution explicitly.

Copying permitted upstream source or substantial material requires an accurate
`NOTICE` entry, any required license text, and attribution at the use site. GPL
and AGPL projects may be studied for behavior, algorithms, or wire formats, but
their source expression must not be copied into this repository. Record the
clean-room decision in [the exploration ledger](docs/exploration-mining-ledger.md).

Model weights and adapters are separate from the source license. Update
[docs/model-licenses.md](docs/model-licenses.md) when a default or recommended
model changes, and link the publisher's current terms.

## Pull-request review

Reviewers look for correctness, a narrow diff, recovery behavior, privacy and
security impact, backwards compatibility, tests proportional to risk,
documentation, and licensing evidence. Hardware-specific claims should include
the GPU, VRAM, quantization/offload mode, model revision, and a reproducible
command or benchmark record.

Maintainers may ask to split a pull request even when every part is useful. A
small, auditable change is easier to merge and safer to revert.
