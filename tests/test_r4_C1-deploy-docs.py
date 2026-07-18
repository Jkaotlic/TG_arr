"""C1-deploy-docs: guard tests for deploy/docs config invariants.

These items are config/docs only (no Python behaviour), so instead of pinning
runtime behaviour we assert the concrete invariants the cluster fixed, reading
the repo's own files. This keeps the changes from silently regressing.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

try:
    import yaml  # type: ignore
except ImportError:  # pragma: no cover - PyYAML not guaranteed in runtime env
    yaml = None

REPO_ROOT = Path(__file__).resolve().parent.parent


def _read(name: str) -> str:
    return (REPO_ROOT / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# DEP-01: `make dev` installs dev dependencies via the [dev] extra.
# ---------------------------------------------------------------------------
def test_makefile_dev_installs_dev_extra() -> None:
    text = _read("Makefile")
    # Find the recipe body under the `dev:` target.
    m = re.search(r"^dev:\n((?:\t.*\n?)+)", text, re.MULTILINE)
    assert m, "Makefile is missing a `dev:` target"
    recipe = m.group(1)
    assert 'pip install -e ".[dev]"' in recipe or "-r requirements-dev.txt" in recipe, (
        "`make dev` must install dev dependencies "
        '(pip install -e ".[dev]" or -r requirements-dev.txt)'
    )
    # The old broken behaviour installed an ad-hoc `ruff mypy` pair.
    assert "pip install ruff mypy" not in recipe


# ---------------------------------------------------------------------------
# DEP-02: README no longer lists orjson (removed/unused dependency).
# ---------------------------------------------------------------------------
def test_readme_does_not_mention_orjson() -> None:
    assert "orjson" not in _read("README.md").lower()


# ---------------------------------------------------------------------------
# DEP-03: README pins the same aiogram version as requirements.txt.
# TEST-15: no hardcoded version literal here — a routine aiogram bump (Wave 3
# / DEP-05) must not fail this test just for updating both files in lockstep.
# It only checks that README and requirements.txt still agree with each other.
# ---------------------------------------------------------------------------
def test_readme_aiogram_matches_requirements() -> None:
    req = _read("requirements.txt")
    m = re.search(r"^aiogram==([\d.]+)", req, re.MULTILINE)
    assert m, "requirements.txt must pin aiogram"
    pinned = m.group(1)
    readme = _read("README.md")
    assert f"aiogram {pinned}" in readme, (
        f"README should reference aiogram {pinned}, not a stale version"
    )


# ---------------------------------------------------------------------------
# DEP-04: requirements-dev.txt pins fall inside pyproject [dev] ranges.
# ---------------------------------------------------------------------------
def _parse_pin(spec: str) -> tuple[str, tuple[int, ...]]:
    name, ver = spec.split("==")
    parts = tuple(int(p) for p in ver.split(".") if p.isdigit())
    return name.strip(), parts


def _parse_floor(spec: str) -> tuple[str, tuple[int, ...]]:
    # e.g. "pytest>=9.0,<10"  -> ("pytest", (9, 0))
    name = re.split(r"[<>=]", spec, maxsplit=1)[0].strip()
    m = re.search(r">=\s*([\d.]+)", spec)
    floor = tuple(int(p) for p in m.group(1).split(".")) if m else ()
    return name, floor


def test_dev_deps_consistent_between_pyproject_and_requirements() -> None:
    pyproject = tomllib.loads(_read("pyproject.toml"))
    dev_ranges = pyproject["project"]["optional-dependencies"]["dev"]
    floors = dict(_parse_floor(s) for s in dev_ranges)

    pins: dict[str, tuple[int, ...]] = {}
    for line in _read("requirements-dev.txt").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "==" not in line:
            continue
        name, ver = _parse_pin(line)
        pins[name] = ver

    assert set(pins) == set(floors), (
        "requirements-dev.txt and pyproject [dev] must list the same tools"
    )
    for name, pin in pins.items():
        floor = floors[name]
        assert pin >= floor, (
            f"{name}=={'.'.join(map(str, pin))} is below pyproject floor "
            f">={'.'.join(map(str, floor))}"
        )


# ---------------------------------------------------------------------------
# DEPLOY-02: compose bot service exposes Lidarr/Deezer env passthrough.
# ---------------------------------------------------------------------------
@pytest.mark.skipif(yaml is None, reason="PyYAML not installed")
def test_compose_exposes_music_env() -> None:
    compose = yaml.safe_load(_read("docker-compose.yml"))
    env = compose["services"]["tg-arr-bot"]["environment"]
    # environment may be a mapping or a list of KEY: VALUE strings
    keys = set(env) if isinstance(env, dict) else {e.split(":")[0].split("=")[0] for e in env}
    for required in ("LIDARR_URL", "LIDARR_API_KEY", "DEEZER_ENABLED"):
        assert required in keys, f"{required} missing from compose bot environment"


# ---------------------------------------------------------------------------
# DEPLOY-03: Dockerfile base image is pinned by digest.
# ---------------------------------------------------------------------------
def test_dockerfile_base_pinned_by_digest() -> None:
    froms = re.findall(r"^FROM\s+(\S+)", _read("Dockerfile"), re.MULTILINE)
    assert froms, "Dockerfile has no FROM lines"
    for image in froms:
        assert "@sha256:" in image, f"FROM {image} is not pinned by digest"


# ---------------------------------------------------------------------------
# DEPLOY-04: resource-limits comment no longer claims Swarm-only.
# ---------------------------------------------------------------------------
def test_compose_resource_comment_not_swarm_only() -> None:
    text = _read("docker-compose.yml")
    assert "use Portainer UI to set limits" not in text
    # The fixed comment must mention Compose V2 enforcing limits standalone.
    assert "Compose V2" in text


def test_deploy_and_rollback_wait_for_container_health() -> None:
    """DEPLOY-01: a finished build is not a successful rollout."""
    text = _read("Makefile")
    for target in ("deploy", "rollback"):
        match = re.search(rf"^{target}:\n((?:\t.*\n?)+)", text, re.MULTILINE)
        assert match, f"Makefile is missing `{target}`"
        recipe = match.group(1)
        assert "--wait" in recipe and "--wait-timeout" in recipe


# ---------------------------------------------------------------------------
# DEPLOY-05: dev compose defines the bot-data volume it mounts.
# ---------------------------------------------------------------------------
@pytest.mark.skipif(yaml is None, reason="PyYAML not installed")
def test_dev_compose_defines_mounted_volume() -> None:
    compose = yaml.safe_load(_read("docker-compose.dev.yml"))
    volumes = compose.get("volumes") or {}
    mounts = compose["services"]["tg-arr-bot"].get("volumes", [])
    named = {m.split(":")[0] for m in mounts if not m.startswith(".") and not m.startswith("/")}
    for name in named:
        assert name in volumes, (
            f"named volume {name!r} is mounted but not defined in docker-compose.dev.yml"
        )
