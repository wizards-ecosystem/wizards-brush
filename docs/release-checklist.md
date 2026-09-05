# Release checklist

This repository is ready for a public release only after the checks below pass.
They intentionally include GitHub settings that source changes cannot make.

## In the checkout

```bash
git status --short
make release-check
make release-bundle
(cd release && sha256sum -c wizards-brush-*.sha256)
```

- Confirm `git status --short` contains only reviewed source/documentation
  changes. It must not include `.env`, generated runners, model weights,
  generated media, SQLite databases, or private notes.
- Read `.env.example`, `README.md`, `NOTICE`, `THIRD_PARTY_NOTICES.md`,
  `LICENSE`, `SECURITY.md`, the model-license inventory, and the Remote GPU
  guide as rendered Markdown.
- Extract the standalone bundle into a temporary directory, verify
  `MANIFEST.sha256`, and run `./install.sh` plus `./start.sh` there. Confirm it
  needs no Git, Make, Node, repository tests, or frontend source.
- Inspect `RELEASE-MANIFEST.json`: it must name the reviewed commit/tag, report
  `dirty_build: false`, and match the archive version.
- Validate both CycloneDX SBOMs, their adjacent checksums, and their source
  commit property. They must reflect the locked Python runtime and the frontend
  packages actually installed for the Linux build.
- Test the documented fresh-clone flow with no pre-existing `.env`, `.runtime`,
  `.venv`, `frontend/node_modules`, models, or output. Confirm `make install`
  performs no model download and `make setup` remains re-runnable.
- Confirm the version agrees across `pyproject.toml`, `frontend/package.json`,
  `CITATION.cff`, the changelog, and the intended tag.
- Check the current model licenses and terms for every default-enabled model.
  Model weights are not covered by this project's Apache-2.0 license.
- Confirm every restricted, non-commercial, geographically limited, unknown,
  or gated model remains opt-in and is described in `docs/model-licenses.md`.
- Build the frontend and verify that `/third-party-notices.txt`, the favicon,
  and the web manifest are present in `frontend/dist/`.
- Compare the browser notice against the exact locked versions and licenses of
  runtime dependencies and build tools that contribute output. The
  release-readiness test automates this against `frontend/package-lock.json`.
- If code or substantial material from an upstream is added, record the source,
  license, scope of reuse, and clean-room boundary in `NOTICE` and the
  exploration ledger before release.
- Do not add the ignored `docs/feedback-loop.md` or
  `docs/video-stack-research.md`; they are intentionally local/private notes.

## GitHub settings to complete immediately before publishing

- Enable private vulnerability reporting and verify the link in `SECURITY.md`.
- Set the repository description, website (if any), topics, and social preview.
- Protect `main`: require the CI workflow, require pull requests as appropriate,
  and restrict force pushes/deletions.
- Add a tag ruleset for `v*` so only release maintainers can create, update, or
  delete release tags. Enable immutable releases if the repository offers it.
- Review Actions permissions, token permissions, and allowed third-party
  actions. Keep workflow actions pinned to reviewed major versions or commit
  SHAs according to the project's policy.
- Enable Dependabot security updates, dependency graph, secret scanning,
  push protection, and CodeQL where the repository plan supports them.
- Decide whether Issues and Discussions are enabled, then configure the contact
  links and templates to match that decision.
- Confirm the default branch, license detection, `NOTICE`, and security policy
  are visible in the repository interface.
- Create labels used by the issue templates (`bug` and `enhancement`) or update
  the templates before the first issue is opened.
- Set `docs/assets/readme-social.png` as the repository social preview.

## Tag and release

- Move the changelog's Unreleased entries under the new semantic version and
  UTC release date; leave a fresh Unreleased section above it.
- Commit the version and changelog before creating the tag. Prefer a signed or
  otherwise attributable annotated tag.
- Publish from the reviewed tag, not an uncommitted working tree. Include the
  changelog summary, supported-platform statement, model-license boundary, and
  upgrade notes in the release description.
- Confirm the tag workflow attached the `.tar.xz`, CycloneDX SBOMs, adjacent
  `.sha256` files, and GitHub build provenance attestations to the release.
  Verify the attestations with `gh`.
- Require reviewer approval for the `release` Actions environment. The build job
  is read-only, attestation alone receives OIDC permission, and only the final
  publish step receives `GH_TOKEN` with release-write permission.
- Download the generated source archives and confirm they contain the license,
  NOTICE, third-party notices, community files, and no ignored runtime data.

## Visibility change

Changing a repository from private to public is a separate, irreversible
external action. This project does not perform it. After the preceding steps
are complete, have a maintainer review the final diff and change visibility in
the repository settings.
