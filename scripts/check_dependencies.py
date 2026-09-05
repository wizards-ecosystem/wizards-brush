"""Validate installed requirements, allowing only tested metadata overrides."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, distributions, version

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

_ALLOWED_OVERRIDES = {
    ("mediapipe", "protobuf"),
    ("torch", "setuptools"),
}


def _override_is_tested(owner: str, dependency: str) -> bool:
    installed = {
        "mediapipe": version("mediapipe"),
        "protobuf": version("protobuf"),
        "setuptools": version("setuptools"),
        "torch": version("torch"),
    }
    if (owner, dependency) == ("mediapipe", "protobuf"):
        return installed["mediapipe"] == "0.10.14" and installed["protobuf"] == "5.29.6"
    if (owner, dependency) == ("torch", "setuptools"):
        return installed["torch"] == "2.11.0+cu130" and installed["setuptools"] == "83.0.0"
    return False


def main() -> None:
    problems: list[str] = []
    allowed: list[str] = []
    for distribution in distributions():
        owner = canonicalize_name(distribution.metadata["Name"])
        for raw_requirement in distribution.requires or ():
            requirement = Requirement(raw_requirement)
            if requirement.marker and not requirement.marker.evaluate({"extra": ""}):
                continue

            dependency = canonicalize_name(requirement.name)
            try:
                installed_version = version(requirement.name)
            except PackageNotFoundError:
                problems.append(f"{owner} requires missing package {requirement}")
                continue
            if not requirement.specifier or requirement.specifier.contains(installed_version):
                continue

            pair = (owner, dependency)
            message = f"{owner} requires {requirement}, but {installed_version} is installed"
            if pair in _ALLOWED_OVERRIDES and _override_is_tested(*pair):
                allowed.append(message)
            else:
                problems.append(message)

    if problems:
        raise RuntimeError("installed dependency problems:\n  " + "\n  ".join(sorted(set(problems))))

    print("installed dependency requirements OK")
    for message in sorted(set(allowed)):
        print(f"  tested security override: {message}")


if __name__ == "__main__":
    main()
