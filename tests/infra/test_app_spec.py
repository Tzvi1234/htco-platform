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
DEPLOY: dict[Any, Any] = yaml.safe_load(DEPLOY_TEXT)
PLACEHOLDER = re.compile(r"^\$\{[A-Z][A-Z0-9_]*\}$")
SECRET_SETTINGS = {
    "PGPASSWORD",
    "HTCO_SENTRY_DSN",
    "HTCO_SUPABASE_SECRET_KEY",
    "HTCO_SESSION_KEY",
    "HTCO_TOKEN_VAULT_KEY",
    "HTCO_TOKEN_VAULT_PREVIOUS_KEYS",
    "HTCO_ETSY_KEYSTRING",
    "HTCO_ETSY_SHARED_SECRET",
}


def _api() -> dict[str, Any]:
    [service] = SPEC["services"]
    assert service["name"] == "api"
    api: dict[str, Any] = service
    return api


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


def test_spec__invitation_links_land_on_the_web_apps_accept_page() -> None:
    assert _envs()["HTCO_INVITE_REDIRECT_URL"]["value"] == "https://dash.ht-co.app/accept-invite"


def test_spec__password_reset_links_land_on_the_web_apps_reset_page() -> None:
    env = _envs()["HTCO_PASSWORD_RESET_REDIRECT_URL"]
    assert env["value"] == "https://dash.ht-co.app/reset-password"
    assert env.get("type") != "SECRET"


def test_spec__etsy_redirect_uri_is_the_web_apps_callback_route() -> None:
    env = _envs()["HTCO_ETSY_REDIRECT_URI"]

    assert env["value"] == "https://dash.ht-co.app/integrations/etsy/callback"
    assert "type" not in env  # a plain value, registered exactly at Etsy


def test_deploy__etsy_app_credentials_come_from_their_github_secrets() -> None:
    [deploy_step] = [
        step
        for job in DEPLOY["jobs"].values()
        for step in job["steps"]
        if str(step.get("uses", "")).startswith("digitalocean/app_action/deploy@")
    ]
    env = deploy_step["env"]

    assert env["HTCO_ETSY_KEYSTRING"] == "${{ secrets.ETSY_KEYSTRING }}"
    assert env["HTCO_ETSY_SHARED_SECRET"] == "${{ secrets.ETSY_SHARED_SECRET }}"  # noqa: S105 - test value


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
            if step.get("name") == BUDGET_STEP:
                # Reads the proposed monthly cost: the DigitalOcean token, and the image pull token
                # so the proposal can validate the private image. No application secret.
                assert set(re.findall(r"secrets\.([A-Z_]+)", yaml.safe_dump(step))) == {
                    "DIGITALOCEAN_ACCESS_TOKEN",
                    "GHCR_PULL_TOKEN",
                }
                continue
            assert "secrets." not in yaml.safe_dump(step), step.get("name")


WORKFLOWS = REPO_ROOT / ".github" / "workflows"
BUDGET_STEP = "The monthly cost stays within the budget"


def _workflow(name: str) -> dict[Any, Any]:
    loaded: dict[Any, Any] = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    return loaded


@pytest.mark.ac("AC-2.1.4")
@pytest.mark.parametrize("name", ["migrate.yml", "deploy.yml", "admin-bootstrap.yml"])
def test_production_workflows__refuse_anyone_but_the_repository_owner(name: str) -> None:
    """Write access is enough to start a dispatched workflow; the first step stops non-owners."""
    for job in _workflow(name)["jobs"].values():
        first = job["steps"][0]
        assert first["name"] == "Only the repository owner may run this"
        assert '"$GITHUB_ACTOR" != "$GITHUB_REPOSITORY_OWNER"' in first["run"]


def test_migrate__records_the_history_even_when_applying_fails() -> None:
    [job] = _workflow("migrate.yml")["jobs"].values()
    [record] = [step for step in job["steps"] if step.get("name") == "Record the applied versions"]
    assert record["if"] == "${{ !cancelled() }}"


def test_deploy__a_rollback_tag_must_be_a_full_commit_sha() -> None:
    """A shortened SHA passed the old check and failed only at DigitalOcean (2026-10-14 drill)."""
    [job] = DEPLOY["jobs"].values()
    [choose] = [step for step in job["steps"] if step.get("name") == "Choose the image tag"]
    assert '"${#REQUESTED_TAG}" -ne 40' in choose["run"]


def test_spec__the_api_and_the_web_app_answer_on_their_domains() -> None:
    assert SPEC["domains"] == [
        {"domain": "api.ht-co.app", "type": "PRIMARY"},
        {"domain": "dash.ht-co.app", "type": "ALIAS"},
    ]


def _web() -> dict[str, Any]:
    [site] = SPEC["static_sites"]
    assert site["name"] == "web"
    web: dict[str, Any] = site
    return web


def test_spec__the_web_app_is_a_static_site_built_from_the_clients_main() -> None:
    """Client ADR-0006: a static site costs nothing inside an app with a paid service."""
    web = _web()
    assert web["github"] == {
        "repo": "Tzvi1234/htco-clients",
        "branch": "main",
        "deploy_on_push": True,
    }
    assert web["dockerfile_path"] == "apps/web/Dockerfile"
    assert web["output_dir"] == "/src/apps/web/dist"
    # Client routes such as /settings/security load directly.
    assert web["catchall_document"] == "index.html"


def test_spec__the_web_origin_sends_api_to_the_api_and_the_rest_to_the_web_app() -> None:
    """Platform ADR-0006: the API on the page's own origin, so no CORS and a first-party cookie."""
    rules = [
        (
            rule["match"].get("authority", {}).get("exact"),
            rule["match"]["path"]["prefix"],
            rule["component"]["name"],
            rule["component"].get("preserve_path_prefix", False),
        )
        for rule in SPEC["ingress"]["rules"]
    ]
    assert rules == [
        ("dash.ht-co.app", "/api", "api", False),  # /api/v1/me reaches the API as /v1/me
        ("dash.ht-co.app", "/", "web", False),
        (None, "/", "api", False),  # api.ht-co.app and the platform URL
    ]


def test_deploy__checks_health_on_the_default_ingress() -> None:
    """A new custom domain takes minutes to get its certificate; the platform URL works at once."""
    [job] = DEPLOY["jobs"].values()
    [check] = [
        s for s in job["steps"] if s.get("name") == "The deployed API answers its health checks"
    ]
    # The URLs arrive through the step's environment; the platform URL is tried first.
    assert ".default_ingress" in check["env"]["INGRESS"]
    assert 'url="${INGRESS:-$LIVE_URL}"' in check["run"]


def test_deploy__refuses_a_spec_that_costs_more_than_the_budget() -> None:
    """The owner's limit: nothing may make the bill grow unnoticed (a static site is free today)."""
    [job] = DEPLOY["jobs"].values()
    names = [step.get("name") for step in job["steps"]]
    assert names.index(BUDGET_STEP) < names.index("Deploy")
    [budget] = [step for step in job["steps"] if step.get("name") == BUDGET_STEP]
    assert budget["env"]["BUDGET_USD"] == "20"
    assert "/v2/apps/propose" in budget["run"]
    assert ".app_cost" in budget["run"]


def test_admin_bootstrap__owner_dispatch_only_inputs_via_env_and_no_password_in_the_url() -> None:
    """Slice 2.5 in production: the owner runs `htco admin bootstrap` from Actions."""
    workflow = _workflow("admin-bootstrap.yml")
    triggers = workflow[True] if True in workflow else workflow["on"]
    assert set(triggers) == {"workflow_dispatch"}
    [job] = workflow["jobs"].values()
    [run] = [step for step in job["steps"] if "htco admin bootstrap" in str(step.get("run", ""))]
    # Inputs reach the shell as environment variables, never interpolated into the script.
    assert "${{" not in run["run"]
    assert run["env"]["PGPASSWORD"] == "${{ secrets.SUPABASE_DB_PASSWORD }}"
    assert ":${" not in run["env"]["HTCO_DATABASE_URL"].split("@")[0]


def test_deploy_never_puts_the_whole_app_object_in_a_step_environment() -> None:
    """GitHub prints every step's environment in the run log. DigitalOcean's app object holds the
    account's email, internal IDs and every (encrypted) secret, so only the URLs are passed on."""
    assert "${{ steps.deploy.outputs.app }}" not in DEPLOY_TEXT


WORKER_SECRETS = {
    "PGPASSWORD",
    "HTCO_TOKEN_VAULT_KEY",
    "HTCO_TOKEN_VAULT_PREVIOUS_KEYS",
    "HTCO_ETSY_KEYSTRING",
    "HTCO_ETSY_SHARED_SECRET",
}


def _worker() -> dict[str, Any]:
    [worker] = SPEC["workers"]
    assert worker["name"] == "agent-worker"
    found: dict[str, Any] = worker
    return found


def test_worker__the_apis_image_running_the_worker_on_one_fixed_instance() -> None:
    """Slice 5.1: $5 a month that cannot grow by itself."""
    worker = _worker()
    assert worker["image"] == _api()["image"]
    assert worker["run_command"] == "python -m htco_worker"
    assert (worker["instance_size_slug"], worker["instance_count"]) == ("apps-s-1vcpu-0.5gb", 1)
    assert "autoscaling" not in worker
    assert worker["liveness_health_check"]["http_path"] == "/healthz"


def test_worker__secrets_are_placeholders_and_the_database_url_has_no_password() -> None:
    envs = {env["key"]: env for env in _worker()["envs"]}
    for key in WORKER_SECRETS:
        assert envs[key]["type"] == "SECRET", key
        assert PLACEHOLDER.match(envs[key]["value"]), key
    assert envs["HTCO_DATABASE_URL"]["value"] == _envs()["HTCO_DATABASE_URL"]["value"]
    assert envs["HTCO_ENVIRONMENT"]["value"] == "production"


def test_worker__runs_only_the_named_schedules() -> None:
    envs = {env["key"]: env for env in _worker()["envs"]}
    assert set(envs["HTCO_WORKER_SCHEDULES"]["value"].split(",")) == {
        "maintenance.capability_probes",
        "maintenance.refresh_token_expiry",
        "maintenance.purge_disconnected_shops",
        "maintenance.prune_finished_jobs",
        # Activation step 4 (5.11): reconciliation at HTCO_RECONCILIATION_SCHEDULE's times.
        "orders.reconciliation",
    }


def test_order_automations_active__the_api_stops_syncing_and_takes_webhooks() -> None:
    """Activation steps 3 and 5 (5.11): the worker owns the order sync; the webhook endpoint is
    on, since the owner registered it in the Etsy developer portal and set ETSY_WEBHOOK_SECRET
    (2026-10-26), which the deploy maps from the GitHub secret, never a literal."""
    envs = _envs()
    assert envs["HTCO_ORDER_SYNC_IN_API"]["value"] == "false"
    assert envs["HTCO_ETSY_WEBHOOKS_ENABLED"]["value"] == "true"
    assert envs["HTCO_ETSY_WEBHOOK_SECRET"]["type"] == "SECRET"
    assert envs["HTCO_ETSY_WEBHOOK_SECRET"]["value"] == "${HTCO_ETSY_WEBHOOK_SECRET}"


def test_api__starts_automation_executions_for_the_worker() -> None:
    """Activation step 2b and the audited admin retry (5.10, 5.11): the API's engine creates
    executions and jobs, and never runs a handler."""
    assert _envs()["HTCO_AUTOMATIONS_ENABLED"]["value"] == "true"
