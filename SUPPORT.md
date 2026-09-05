# Support

The Wizard's Brush is a community-maintained pre-1.0 project. Support is
best-effort and no response-time guarantee is offered.

## Supported baseline

The release-tested local path is Linux or WSL2 on x86-64 with an NVIDIA CUDA GPU
and the project-owned toolchain installed by `make install`. A 16 GB GPU is the
starter hardware target. Native Windows, macOS, AMD ROCm, Apple Metal, and
CPU-only generation are not currently supported.

Standalone release users install with `./install.sh` and can run the same
containment audit with `.venv/bin/python scripts/doctor.py`.

The optional Remote GPU is supported only on hardware and network access you
operate or are explicitly authorized to use. Provider account, quota, network,
and model-license questions belong with their respective provider or publisher.

## Before opening an issue

1. Read the [getting-started guide](docs/getting-started.md) and the relevant [user guide](docs/user-guide.md) section.
2. Run `make doctor` and open **Diagnosis**.
3. Search existing issues for the same symptom.
4. Reproduce with the smallest prompt, settings, and input files that demonstrate the problem.
5. Remove tokens, private URLs, prompts, generated media, personal data, and machine usernames from everything you attach.

Do not attach `.env`, `remote_gpu_filled.py`, SQLite databases, private model
URLs, or unredacted logs.

## Where to ask

- Open a bug report for a repeatable failure in a supported configuration.
- Open a feature request for a concrete user outcome and acceptance criteria.
- Use Discussions, if enabled, for setup questions and open-ended ideas.
- Follow [SECURITY.md](SECURITY.md) for vulnerabilities. Never create a public security issue.
- Follow [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) for private conduct reporting.

## What a useful bug report contains

- The Wizard's Brush version or commit.
- OS and WSL version when applicable.
- GPU model, VRAM, NVIDIA driver, and the selected hardware profile.
- Local or Remote GPU lane, exact model ID, and quantization/offload choice.
- Reproduction steps, expected result, and actual result.
- The smallest non-secret log excerpt and Diagnosis status that explain the failure.
- Whether the problem survives a restart and whether it occurs with the starter model.

If a model loads outside this application but not inside it, include the exact
publisher-supported command that worked and its dependency versions.
