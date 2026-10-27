"""Browser Configuration Profiles (slice 8A.2b, ADR-0025 ֲ§2): the platform baseline, the per-shop
settings, and the validator that runs on save and again at session start (AC-8A.2.10)."""

import uuid
from datetime import UTC, datetime

import pytest

from htco_domain.actors import ActorKind, ActorRef
from htco_domain.browser_configuration import (
    BACKGROUND_TRAFFIC_SWITCHES,
    CONFIGURABLE_KEYS,
    DENIED_PERMISSIONS,
    NEVER_CONFIGURABLE_KEYS,
    PINNED_SWITCHES,
    PLATFORM_BASELINE,
    SHARED_EGRESS,
    ConfigurationIssue,
    ConfigurationRefusedError,
    ConfigurationVersion,
    RecipeNeeds,
    ShopBrowserSettings,
    check_session,
    validate_settings,
)

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
OWNER = ActorRef(ActorKind.USER, str(uuid.UUID(int=1)))
BUILD = "141.0.7390.37"


def issues_of(error: pytest.ExceptionInfo[ConfigurationRefusedError]) -> set[tuple[str, str]]:
    return {(issue.code, issue.field) for issue in error.value.issues}


def version(**overrides: object) -> ConfigurationVersion:
    fields: dict[str, object] = {
        "environment_id": uuid.UUID(int=10),
        "organization_id": uuid.UUID(int=11),
        "shop_id": uuid.UUID(int=12),
        "version": 1,
        "settings": ShopBrowserSettings(locale="he-IL", egress_id=SHARED_EGRESS),
        "shop_languages": ("he-IL", "en-US"),
        "created_by": OWNER,
        "correlation_id": "req-1",
        "created_at": NOW,
    }
    fields.update(overrides)
    return ConfigurationVersion(**fields)  # type: ignore[arg-type]


# --- The baseline -----------------------------------------------------------------------------


def test_baseline__one_viewport_one_color_scheme_and_a_denying_permissions_policy() -> None:
    assert (PLATFORM_BASELINE.viewport.width, PLATFORM_BASELINE.viewport.height) == (1280, 900)
    assert PLATFORM_BASELINE.color_scheme == "light"
    assert {"notifications", "camera", "microphone", "clipboard-read", "geolocation"} <= (
        DENIED_PERMISSIONS
    )


@pytest.mark.ac("AC-8A.2.11")
def test_baseline__pinned_switches__background_traffic_and_safe_browsing_off() -> None:
    assert PINNED_SWITCHES == (
        "--disable-background-networking",
        "--disable-component-update",
        "--disable-field-trial-config",
        "--disable-domain-reliability",
        "--no-pings",
        "--disable-client-side-phishing-detection",
        "--safebrowsing-disable-auto-update",
        "--disable-sync",
        "--disable-breakpad",
    )
    assert set(BACKGROUND_TRAFFIC_SWITCHES) <= set(PINNED_SWITCHES)


@pytest.mark.ac("AC-8A.2.11")
@pytest.mark.parametrize(
    "fragment",
    [
        "remote-debugging-port", "user-agent", "proxy", "lang", "disable-blink-features",
        "AutomationControlled", "window-size", "force-device-scale-factor", "load-extension",
        "no-sandbox", "disable-web-security", "disable-site-isolation", "host-resolver-rules",
    ],
)  # fmt: skip
def test_baseline__no_switch_disguises_the_browser_or_weakens_it(fragment: str) -> None:
    assert not any(fragment in switch for switch in PINNED_SWITCHES)


# --- Saving: AC-8A.2.10 -----------------------------------------------------------------------


@pytest.mark.ac("AC-8A.2.10")
def test_validate__a_locale_the_shop_works_in_and_the_shared_egress__accepted() -> None:
    settings = validate_settings(
        {"locale": "he-IL", "egress_id": "shared"},
        shop_languages=("he-IL", "en-US"),
        allocated_egress=frozenset(),
    )

    assert settings == ShopBrowserSettings(locale="he-IL", egress_id=SHARED_EGRESS)


def test_validate__the_egress_defaults_to_shared() -> None:
    settings = validate_settings(
        {"locale": "en-US"}, shop_languages=("en-US",), allocated_egress=frozenset()
    )

    assert settings.egress_id == SHARED_EGRESS
    assert not settings.dedicated_egress


def test_validate__a_locale_matches_its_recorded_language_whatever_its_case() -> None:
    settings = validate_settings(
        {"locale": "en-us"}, shop_languages=("en-US",), allocated_egress=frozenset()
    )

    assert settings.locale == "en-US"


@pytest.mark.ac("AC-8A.2.10")
@pytest.mark.parametrize("key", sorted(NEVER_CONFIGURABLE_KEYS))
def test_validate__every_never_configurable_key__refused_by_name(key: str) -> None:
    with pytest.raises(ConfigurationRefusedError) as refused:
        validate_settings(
            {"locale": "en-US", key: "anything"},
            shop_languages=("en-US",),
            allocated_egress=frozenset(),
        )

    assert issues_of(refused) == {("never_configurable", key)}


@pytest.mark.ac("AC-8A.2.10")
def test_validate__the_named_honesty_keys_are_never_configurable() -> None:
    assert {
        "user_agent", "screen", "timezone", "geolocation", "fingerprint", "stealth",
        "extensions", "proxy", "viewport", "permissions", "chromium_version", "args",
    } <= NEVER_CONFIGURABLE_KEYS  # fmt: skip
    assert not CONFIGURABLE_KEYS & NEVER_CONFIGURABLE_KEYS


def test_validate__an_unknown_key__refused_by_name() -> None:
    with pytest.raises(ConfigurationRefusedError) as refused:
        validate_settings(
            {"locale": "en-US", "colour": "blue"},
            shop_languages=("en-US",),
            allocated_egress=frozenset(),
        )

    assert issues_of(refused) == {("unknown_setting", "colour")}


@pytest.mark.ac("AC-8A.2.10")
def test_validate__a_locale_outside_the_shops_recorded_languages__refused() -> None:
    with pytest.raises(ConfigurationRefusedError) as refused:
        validate_settings(
            {"locale": "fr-FR"}, shop_languages=("he-IL", "en-US"), allocated_egress=frozenset()
        )

    assert issues_of(refused) == {("locale_not_recorded", "locale")}


def test_validate__a_shop_with_no_recorded_language__refuses_every_locale() -> None:
    with pytest.raises(ConfigurationRefusedError) as refused:
        validate_settings({"locale": "en-US"}, shop_languages=(), allocated_egress=frozenset())

    assert issues_of(refused) == {("locale_not_recorded", "locale")}


@pytest.mark.parametrize("locale", ["", "english", "en_US", "en-US-x-stealth", 7, None, "e"])
def test_validate__a_malformed_locale__refused(locale: object) -> None:
    with pytest.raises(ConfigurationRefusedError) as refused:
        validate_settings(
            {"locale": locale}, shop_languages=("en-US",), allocated_egress=frozenset()
        )

    assert issues_of(refused) == {("locale_invalid", "locale")}


def test_validate__a_missing_locale__refused() -> None:
    with pytest.raises(ConfigurationRefusedError) as refused:
        validate_settings({}, shop_languages=("en-US",), allocated_egress=frozenset())

    assert issues_of(refused) == {("locale_required", "locale")}


@pytest.mark.ac("AC-8A.2.10")
def test_validate__an_unallocated_dedicated_egress__refused() -> None:
    with pytest.raises(ConfigurationRefusedError) as refused:
        validate_settings(
            {"locale": "en-US", "egress_id": "dedicated-shop-7"},
            shop_languages=("en-US",),
            allocated_egress=frozenset({"dedicated-shop-8"}),
        )

    assert issues_of(refused) == {("egress_not_allocated", "egress_id")}


def test_validate__an_allocated_dedicated_egress__accepted() -> None:
    settings = validate_settings(
        {"locale": "en-US", "egress_id": "dedicated-shop-7"},
        shop_languages=("en-US",),
        allocated_egress=frozenset({"dedicated-shop-7"}),
    )

    assert settings.dedicated_egress


@pytest.mark.parametrize("egress", ["", "Shared", "a b", "x" * 101, 3, None])
def test_validate__a_malformed_egress__refused(egress: object) -> None:
    with pytest.raises(ConfigurationRefusedError) as refused:
        validate_settings(
            {"locale": "en-US", "egress_id": egress},
            shop_languages=("en-US",),
            allocated_egress=frozenset(),
        )

    assert issues_of(refused) == {("egress_invalid", "egress_id")}


@pytest.mark.ac("AC-8A.2.10")
def test_validate__every_issue_is_named_at_once() -> None:
    with pytest.raises(ConfigurationRefusedError) as refused:
        validate_settings(
            {"locale": "fr-FR", "egress_id": "dedicated-9", "user_agent": "x", "proxy": "y"},
            shop_languages=("en-US",),
            allocated_egress=frozenset(),
        )

    assert issues_of(refused) == {
        ("locale_not_recorded", "locale"),
        ("egress_not_allocated", "egress_id"),
        ("never_configurable", "proxy"),
        ("never_configurable", "user_agent"),
    }
    # Sorted, so the answer is stable.
    assert list(refused.value.issues) == sorted(refused.value.issues)


def test_validate__a_value_is_never_echoed_in_an_issue() -> None:
    with pytest.raises(ConfigurationRefusedError) as refused:
        validate_settings(
            {"locale": "en-US", "proxy": "http://user:secret@proxy.example:8080"},
            shop_languages=("en-US",),
            allocated_egress=frozenset(),
        )

    assert "secret" not in repr(refused.value.issues)
    assert "secret" not in str(refused.value)


def test_validate__too_many_keys__refused_without_naming_each() -> None:
    raw = {f"k{i}": i for i in range(40)}
    with pytest.raises(ConfigurationRefusedError) as refused:
        validate_settings(raw, shop_languages=("en-US",), allocated_egress=frozenset())

    assert issues_of(refused) == {("too_many_settings", "settings")}


@pytest.mark.parametrize("key", ["", "a" * 65, "Locale!", "../x"])
def test_validate__a_key_that_is_not_a_plain_name__refused_without_echoing_it(key: str) -> None:
    with pytest.raises(ConfigurationRefusedError) as refused:
        validate_settings(
            {"locale": "en-US", key: 1}, shop_languages=("en-US",), allocated_egress=frozenset()
        )

    assert issues_of(refused) == {("unknown_setting", "settings")}


def test_issue__a_code_and_a_field_only() -> None:
    with pytest.raises(ValueError, match="code"):
        ConfigurationIssue("Not A Code", "locale")


# --- The version record ----------------------------------------------------------------------


def test_version__starts_at_one_and_keeps_the_snapshot_of_languages() -> None:
    with pytest.raises(ValueError, match="version"):
        version(version=0)
    with pytest.raises(ValueError, match="UTC"):
        version(created_at=datetime(2026, 10, 27, 12, 0))  # noqa: DTZ001 - the case under test
    with pytest.raises(ValueError, match="recorded"):
        version(settings=ShopBrowserSettings(locale="fr-FR", egress_id=SHARED_EGRESS))


def test_version__as_audit__ids_and_codes_only() -> None:
    assert version().as_audit() == {
        "version": 1, "locale": "he-IL", "egress_id": "shared",
    }  # fmt: skip


# --- At session start: AC-8A.2.10, the same validation again --------------------------------


@pytest.mark.ac("AC-8A.2.10")
def test_check_session__a_valid_version__gives_the_launch_settings() -> None:
    launch = check_session(
        version(), needs=RecipeNeeds(), allocated_egress=frozenset(),
        fleet_build=BUILD, worker_build=BUILD,
    )  # fmt: skip

    assert launch.locale == "he-IL"
    assert launch.viewport == PLATFORM_BASELINE.viewport
    assert launch.color_scheme == "light"
    assert launch.granted_permissions == frozenset()
    assert not launch.downloads
    assert launch.egress_id == SHARED_EGRESS


@pytest.mark.ac("AC-8A.2.10")
def test_check_session__a_dedicated_egress_released_since_the_save__refuses_the_session() -> None:
    saved = version(settings=ShopBrowserSettings(locale="he-IL", egress_id="dedicated-1"))
    with pytest.raises(ConfigurationRefusedError) as refused:
        check_session(
            saved, needs=RecipeNeeds(), allocated_egress=frozenset(),
            fleet_build=BUILD, worker_build=BUILD,
        )  # fmt: skip

    assert issues_of(refused) == {("egress_not_allocated", "egress_id")}


@pytest.mark.ac("AC-8A.2.10")
@pytest.mark.parametrize("permission", ["notifications", "camera", "microphone", "clipboard-read"])
def test_check_session__a_recipe_needing_a_permission_the_baseline_denies__refused(
    permission: str,
) -> None:
    with pytest.raises(ConfigurationRefusedError) as refused:
        check_session(
            version(), needs=RecipeNeeds(permissions=frozenset({permission})),
            allocated_egress=frozenset(), fleet_build=BUILD, worker_build=BUILD,
        )  # fmt: skip

    assert issues_of(refused) == {("permission_denied_by_baseline", permission)}


def test_check_session__an_unknown_permission__refused_too() -> None:
    with pytest.raises(ConfigurationRefusedError) as refused:
        check_session(
            version(), needs=RecipeNeeds(permissions=frozenset({"midi-sysex"})),
            allocated_egress=frozenset(), fleet_build=BUILD, worker_build=BUILD,
        )  # fmt: skip

    assert issues_of(refused) == {("permission_denied_by_baseline", "midi-sysex")}


@pytest.mark.ac("AC-8A.2.11")
def test_check_session__geolocation_without_a_recorded_need__refused() -> None:
    with pytest.raises(ConfigurationRefusedError) as refused:
        check_session(
            version(), needs=RecipeNeeds(permissions=frozenset({"geolocation"})),
            allocated_egress=frozenset(), fleet_build=BUILD, worker_build=BUILD,
        )  # fmt: skip

    assert issues_of(refused) == {("geolocation_not_recorded", "geolocation")}


@pytest.mark.ac("AC-8A.2.11")
def test_check_session__geolocation_with_a_recorded_need__granted_without_any_coordinate() -> None:
    launch = check_session(
        version(),
        needs=RecipeNeeds(permissions=frozenset({"geolocation"}), geolocation_recorded=True),
        allocated_egress=frozenset(), fleet_build=BUILD, worker_build=BUILD,
    )  # fmt: skip

    assert launch.granted_permissions == frozenset({"geolocation"})
    assert not hasattr(launch, "geolocation")  # a coordinate cannot even be expressed


def test_check_session__a_recipe_that_declares_a_download__gets_its_own_folder_only() -> None:
    launch = check_session(
        version(), needs=RecipeNeeds(downloads=True), allocated_egress=frozenset(),
        fleet_build=BUILD, worker_build=BUILD,
    )  # fmt: skip

    assert launch.downloads


@pytest.mark.ac("AC-8A.2.12")
def test_check_session__a_worker_on_another_chromium_build__refused() -> None:
    with pytest.raises(ConfigurationRefusedError) as refused:
        check_session(
            version(), needs=RecipeNeeds(), allocated_egress=frozenset(),
            fleet_build=BUILD, worker_build="142.0.0.1",
        )  # fmt: skip

    assert issues_of(refused) == {("chromium_build_mismatch", "chromium")}


def test_check_session__the_stored_locale_is_checked_again_against_its_snapshot() -> None:
    # A version written around the domain rule (a bad row) is still refused at the start.
    tampered = object.__new__(ConfigurationVersion)
    for name, value in vars(version()).items():
        object.__setattr__(tampered, name, value)
    object.__setattr__(tampered, "shop_languages", ("en-US",))
    with pytest.raises(ConfigurationRefusedError) as refused:
        check_session(
            tampered, needs=RecipeNeeds(), allocated_egress=frozenset(),
            fleet_build=BUILD, worker_build=BUILD,
        )  # fmt: skip

    assert issues_of(refused) == {("locale_not_recorded", "locale")}
