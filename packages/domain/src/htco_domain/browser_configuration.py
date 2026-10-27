"""Browser Configuration Profiles (slice 8A.2b, ADR-0025 §2).

Each browser environment has one versioned configuration profile. It has two parts:

- **The platform baseline**, the same for every shop and never editable per shop: the one pinned
  Chromium, one viewport, one color scheme, one permissions policy that denies everything a
  recipe has not recorded a need for, and a pinned list of switches that turns off the
  browser's own background traffic and Safe Browsing.
- **The per-shop settings**: the locale the shop is really operated in, which must be one of the
  shop's recorded languages, and its egress (the shared one unless the Owner allocated a
  dedicated one).

The browser stays honest (ADR-0020 §2). There is no way to set a User-Agent, a screen, a time
zone, a coordinate, an extension, a proxy or any fingerprint or "stealth" setting: those keys are
refused by name, and the launch settings have no field that could carry them. The validator
runs when a profile is saved and again when a session starts; every refusal names its issues as
codes, never the refused values.
"""

import re
import uuid
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from htco_domain.actors import ActorRef

SHARED_EGRESS = "shared"
LOCALE = "locale"
EGRESS = "egress_id"
CONFIGURABLE_KEYS = frozenset({LOCALE, EGRESS})

# Refused by name: each would make shops look like different devices, hide automation, or move
# traffic outside `NetworkProvider`. The baseline's own settings are here too, because they are
# the same for every shop.
NEVER_CONFIGURABLE_KEYS = frozenset(
    {
        "user_agent", "useragent", "screen", "screen_resolution", "resolution", "viewport",
        "window_size", "device_scale_factor", "timezone", "time_zone", "timezone_id",
        "geolocation", "latitude", "longitude", "coordinates", "fingerprint", "stealth",
        "webdriver", "automation", "extensions", "extension", "proxy", "proxy_server",
        "args", "switches", "flags", "chromium_version", "chromium_build", "executable_path",
        "channel", "color_scheme", "permissions", "downloads", "headless", "platform",
        "hardware_concurrency", "device_memory", "webgl", "canvas", "fonts", "accept_language",
        "extra_http_headers", "is_mobile", "has_touch", "dns", "resolver",
    }
)  # fmt: skip

# The browser's own background traffic, off, so the egress carries the recipe's traffic only.
BACKGROUND_TRAFFIC_SWITCHES = (
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-field-trial-config",
    "--disable-domain-reliability",
    "--no-pings",
)
# The complete list of switches the engine adds. Safe Browsing is off, decided explicitly: it
# would send URL hash prefixes to Google, and navigation is allowlist-only (8A.5). Sync and crash
# reports are off too. No switch here changes what the browser says about itself.
PINNED_SWITCHES = (
    *BACKGROUND_TRAFFIC_SWITCHES,
    "--disable-client-side-phishing-detection",
    "--safebrowsing-disable-auto-update",
    "--disable-sync",
    "--disable-breakpad",
)

# The permissions policy: nothing is granted. Geolocation is granted only to a recipe whose scope
# row records a real need, and then it is the worker's real location, never a coordinate.
DENIED_PERMISSIONS = frozenset(
    {"notifications", "camera", "microphone", "clipboard-read", "clipboard-write", "geolocation"}
)
GEOLOCATION = "geolocation"

MAX_SETTINGS = 20
MAX_LANGUAGES = 20
_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_KEY = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_FIELD = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
# A language tag as Etsy records it: `he`, `en-US`, `zh-Hant`? Only language plus region here.
_LOCALE = re.compile(r"^([A-Za-z]{2,3})(?:-([A-Za-z]{2}))?$")
_EGRESS = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,99}$")


@dataclass(frozen=True, order=True)
class ConfigurationIssue:
    """One reason a configuration is refused: a stable code and the setting it concerns."""

    code: str
    field: str

    def __post_init__(self) -> None:
        if not _CODE.match(self.code):
            raise ValueError("an issue's code is a short code")
        if not _FIELD.match(self.field):
            raise ValueError("an issue's field is a setting name")


class ConfigurationRefusedError(ValueError):
    """Nothing is saved or started. The message names the codes and fields only."""

    def __init__(self, issues: Collection[ConfigurationIssue]) -> None:
        self.issues = tuple(sorted(set(issues)))
        if not self.issues:
            raise ValueError("a refusal names at least one issue")
        super().__init__(", ".join(f"{i.field}: {i.code}" for i in self.issues))


@dataclass(frozen=True)
class Viewport:
    width: int
    height: int


@dataclass(frozen=True)
class PlatformBaseline:
    """The same for every shop (ADR-0025 §2)."""

    viewport: Viewport = Viewport(1280, 900)
    color_scheme: str = "light"
    denied_permissions: frozenset[str] = DENIED_PERMISSIONS
    switches: tuple[str, ...] = PINNED_SWITCHES


PLATFORM_BASELINE = PlatformBaseline()


def canonical_locale(raw: object) -> str | None:
    """`en-us` → `en-US`; None when it is not a language with an optional region."""
    if not isinstance(raw, str):
        return None
    match = _LOCALE.match(raw)
    if match is None:
        return None
    language, region = match.groups()
    return language.lower() if region is None else f"{language.lower()}-{region.upper()}"


@dataclass(frozen=True)
class ShopBrowserSettings:
    """The per-shop part of a configuration profile, already validated."""

    locale: str
    egress_id: str = SHARED_EGRESS

    def __post_init__(self) -> None:
        if canonical_locale(self.locale) != self.locale:
            raise ValueError("a locale is a canonical language tag")
        if not _EGRESS.match(self.egress_id):
            raise ValueError("an egress ID is a short identifier")

    @property
    def dedicated_egress(self) -> bool:
        return self.egress_id != SHARED_EGRESS


def _shop_languages(languages: Collection[str]) -> frozenset[str]:
    return frozenset(
        found for found in (canonical_locale(language) for language in languages) if found
    )


def _issue(code: str, name: str) -> ConfigurationIssue:
    return ConfigurationIssue(code, name)


def _key_issues(raw: Mapping[str, object]) -> list[ConfigurationIssue]:
    issues: list[ConfigurationIssue] = []
    for key in raw:
        # A key that is not a plain name is never echoed back, not even as a field.
        if not _KEY.match(key):
            issues.append(_issue("unknown_setting", "settings"))
        elif key in NEVER_CONFIGURABLE_KEYS:
            issues.append(_issue("never_configurable", key))
        elif key not in CONFIGURABLE_KEYS:
            issues.append(_issue("unknown_setting", key))
    return issues


def _locale_issues(
    raw: Mapping[str, object], recorded: frozenset[str]
) -> tuple[str | None, list[ConfigurationIssue]]:
    if LOCALE not in raw:
        return None, [_issue("locale_required", LOCALE)]
    locale = canonical_locale(raw[LOCALE])
    if locale is None:
        return None, [_issue("locale_invalid", LOCALE)]
    if locale not in recorded:
        return locale, [_issue("locale_not_recorded", LOCALE)]
    return locale, []


def _egress_issues(
    raw: Mapping[str, object], allocated: Collection[str]
) -> tuple[str | None, list[ConfigurationIssue]]:
    egress = raw.get(EGRESS, SHARED_EGRESS)
    if not isinstance(egress, str) or not _EGRESS.match(egress):
        return None, [_issue("egress_invalid", EGRESS)]
    if egress != SHARED_EGRESS and egress not in allocated:
        return egress, [_issue("egress_not_allocated", EGRESS)]
    return egress, []


def validate_settings(
    raw: Mapping[str, Any],
    *,
    shop_languages: Collection[str],
    allocated_egress: Collection[str],
) -> ShopBrowserSettings:
    """The per-shop settings, or `ConfigurationRefusedError` naming every issue at once."""
    if len(raw) > MAX_SETTINGS:
        raise ConfigurationRefusedError([_issue("too_many_settings", "settings")])
    issues = _key_issues(raw)
    locale, locale_issues = _locale_issues(raw, _shop_languages(shop_languages))
    egress, egress_issues = _egress_issues(raw, allocated_egress)
    issues += locale_issues + egress_issues
    if issues or locale is None or egress is None:
        raise ConfigurationRefusedError(issues)
    return ShopBrowserSettings(locale=locale, egress_id=egress)


def _aware(at: datetime) -> bool:
    return at.tzinfo is not None and at.utcoffset() is not None


@dataclass(frozen=True)
class ConfigurationVersion:
    """One saved version. `shop_languages` is the shop's recorded languages at the save, so the
    session start checks the locale again without reading the marketplace."""

    environment_id: uuid.UUID
    organization_id: uuid.UUID
    shop_id: uuid.UUID
    version: int
    settings: ShopBrowserSettings
    shop_languages: tuple[str, ...]
    created_by: ActorRef
    correlation_id: str
    created_at: datetime

    def __post_init__(self) -> None:
        if self.version < 1:
            raise ValueError("a configuration version starts at 1")
        if not _aware(self.created_at):
            raise ValueError("a configuration's time is in UTC")
        if not 1 <= len(self.shop_languages) <= MAX_LANGUAGES:
            raise ValueError("a configuration keeps the shop's recorded languages")
        if self.settings.locale not in _shop_languages(self.shop_languages):
            raise ValueError("a configuration's locale is one of the shop's recorded languages")
        if not self.correlation_id.strip() or len(self.correlation_id) > 200:  # noqa: PLR2004
            raise ValueError("a configuration needs a correlation ID")

    def as_audit(self) -> dict[str, Any]:
        """What the audit keeps: the version and the settings' codes, nothing else."""
        return {
            "version": self.version,
            "locale": self.settings.locale,
            "egress_id": self.settings.egress_id,
        }


@dataclass(frozen=True)
class RecipeNeeds:
    """What the recipe about to run declares (8A.5). Geolocation is granted only with a need
    recorded in the recipe's scope row."""

    permissions: frozenset[str] = frozenset()
    downloads: bool = False
    geolocation_recorded: bool = False


@dataclass(frozen=True)
class LaunchSettings:
    """Everything the engine may set, and nothing else: no field can carry a User-Agent, a time
    zone, a screen, a coordinate or a proxy."""

    locale: str
    egress_id: str
    viewport: Viewport = PLATFORM_BASELINE.viewport
    color_scheme: str = PLATFORM_BASELINE.color_scheme
    granted_permissions: frozenset[str] = field(default_factory=frozenset)
    downloads: bool = False
    switches: tuple[str, ...] = PLATFORM_BASELINE.switches


def _permission_issues(needs: RecipeNeeds) -> list[ConfigurationIssue]:
    issues: list[ConfigurationIssue] = []
    for permission in sorted(needs.permissions):
        name = permission if _FIELD.match(permission) else "permissions"
        if permission == GEOLOCATION:
            if not needs.geolocation_recorded:
                issues.append(_issue("geolocation_not_recorded", GEOLOCATION))
        else:
            # Everything else is denied by the baseline, named or not.
            issues.append(_issue("permission_denied_by_baseline", name))
    return issues


def check_session(
    version: ConfigurationVersion,
    *,
    needs: RecipeNeeds,
    allocated_egress: Collection[str],
    fleet_build: str,
    worker_build: str,
) -> LaunchSettings:
    """The validation again, at session start, plus what only the start can know: the recipe's
    needs, the egress allocations now, and this worker's Chromium build. Refuses the session
    with every issue named."""
    issues: list[ConfigurationIssue] = []
    stored = {LOCALE: version.settings.locale, EGRESS: version.settings.egress_id}
    try:
        validate_settings(
            stored, shop_languages=version.shop_languages, allocated_egress=allocated_egress
        )
    except ConfigurationRefusedError as refused:
        issues += refused.issues
    issues += _permission_issues(needs)
    if not fleet_build or worker_build != fleet_build:
        issues.append(_issue("chromium_build_mismatch", "chromium"))
    if issues:
        raise ConfigurationRefusedError(issues)
    granted = frozenset({GEOLOCATION}) if GEOLOCATION in needs.permissions else frozenset()
    return LaunchSettings(
        locale=version.settings.locale,
        egress_id=version.settings.egress_id,
        granted_permissions=granted,
        downloads=needs.downloads,
    )
