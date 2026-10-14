"""The DigitalOcean app spec and the deploy workflow (slice 1.5 part 2), checked without deploying.

AC-1.5.4: no secret value is committed; every secret comes from `${…}` filled at deploy time.
Cost control: one fixed-size instance that cannot scale, so the bill cannot grow by itself.
"""

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SPEC: dict[str, Any] = yaml.safe_load(
    (REPO_ROOT / "infra" / "digitalocean" / "app.yaml").read_text(encoding="utf-8")
)
DEPLOY_TEXT = (REPO_ROOT / ".github" / "workflows" / "deploy.yml").read_text(encoding="utf-8")
DEPLOY: dict[str, Any] = yaml.safe_load(DEPLOY_TEXT)
PLACEHOLDER = re.compile(r"^\$\{[A-Z][A-Z0-9_]*\}$")
SECRET_SETTINGS = {"PGPASSWORD", "HTCO_SENTRY_DSN", "HTCO_SUPABASE_SECRET_KEY"}


def _api() -> dict[str, Any]:
    [service] = SPEC["services"]
    assert service["name"] == "api"
    return service


def _envs() -> dict[str, dict[str, Any]]:
    return {env["key"]: env for env in _api()["envs"]}


@pytest.mark.ac("AC-1.5.4")
def test_spec__every_secret_is_a_placeholder_filled_at_deploy_time() -> None:
    envs = _envs()
    for key in SECRET_SETTINGS:
        assert envs[key]["type"] == "SECRET", key
        assert PLACEHOLDER.match(envs[key]["value"]), key
    assert PLACEHOLDER.match(_api()["image"]["registry_credentials"])


@pytest.mark.ac("AC-1.5.4")
def test_spec__the_database_url_carries_no_password() -> None:
    url = _envs()["HTCO_DATABASE_URL"]["value"]
    assert "${" in url
    assert ":${" not in url.split("@")[0]  # user, never user:password
    assert "sslmode=require" in url


def test_spec__one_fixed_size_instance_without_any_scaling() -> None:
    api = _api()
    assert api["instance_size_slug"] == "apps-s-1vcpu-0.5gb"
    assert api["instance_count"] == 1
    assert "autoscaling" not in api


def test_spec__frankfurt_production_with_the_supabase_project() -> None:
    envs = _envs()
    assert SPEC["region"] == "fra"
    assert envs["HTCO_ENVIRONMENT"]["value"] == "production"
    assert envs["HTCO_SUPABASE_URL"]["value"] == "${HTCO_SUPABASE_URL}"
    assert envs["HTCO_RELEASE"]["value"] == "${IMAGE_TAG}"


def test_spec__health_checks_and_alerts() -> None:
    api = _api()
    assert api["http_port"] == 8080
    assert api["health_check"]["http_path"] == "/readyz"
    assert api["liveness_health_check"]["http_path"] == "/healthz"
    assert {alert["rule"] for alert in SPEC["alerts"]} >= {"DEPLOYMENT_FAILED"}
    assert {alert["rule"] for alert in api["alerts"]} == {
        "CPU_UTILIZATION",
        "MEM_UTILIZATION",
        "RESTART_COUNT",
    }


def test_spec__image_comes_from_ghcr_at_the_deployed_tag() -> None:
    image = _api()["image"]
    assert (image["registry_type"], image["registry"], image["repository"]) == (
        "GHCR",
        "tzvi1234",
        "htco-api",
    )
    assert image["tag"] == "${IMAGE_TAG}"


def test_deploy__only_the_owner_starts_it() -> None:
    triggers = DEPLOY[True] if True in DEPLOY else DEPLOY["on"]  # YAML 1.1 reads `on` as true
    assert set(triggers) == {"workflow_dispatch"}


def test_deploy__every_placeholder_of_the_spec_is_provided() -> None:
    placeholders = set(re.findall(r"\$\{([A-Z][A-Z0-9_]*)\}", yaml.safe_dump(SPEC)))
    [deploy_step] = [
        step
        for job in DEPLOY["jobs"].values()
        for step in job["steps"]
        if str(step.get("uses", "")).startswith("digitalocean/app_action/deploy@")
    ]
    assert placeholders <= set(deploy_step["env"])


def test_deploy__secrets_reach_only_the_deploy_step() -> None:
    for job in DEPLOY["jobs"].values():
        assert "secrets." not in yaml.safe_dump(job.get("env", {}))
        for step in job["steps"]:
            uses = str(step.get("uses", ""))
            if uses.startswith(("digitalocean/app_action/deploy@", "docker/login-action@")):
                continue
            assert "secrets." not in yaml.safe_dump(step), step.get("name")
