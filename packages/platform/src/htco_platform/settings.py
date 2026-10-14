"""Load typed settings from the environment once, at startup, and fail fast (ER-CODE-07)."""

from pydantic import ValidationError
from pydantic_core import ErrorDetails
from pydantic_settings import BaseSettings


class ConfigurationError(Exception):
    """Settings are missing or invalid. The message names every problem setting."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("\n".join(problems))
        self.problems = problems


def load_settings[SettingsT: BaseSettings](settings_class: type[SettingsT]) -> SettingsT:
    """Read `settings_class` from the environment, or raise `ConfigurationError`.

    The error names each missing or invalid environment variable but never repeats its value,
    because settings often hold secrets.
    """
    try:
        return settings_class()
    except ValidationError as error:
        prefix = str(settings_class.model_config.get("env_prefix", ""))
        problems = [_describe(prefix, detail) for detail in error.errors(include_input=False)]
        raise ConfigurationError(problems) from None


def _describe(prefix: str, detail: ErrorDetails) -> str:
    variable = (prefix + "__".join(str(part) for part in detail["loc"])).upper()
    if detail["type"] == "missing":
        return f"{variable} is required"
    return f"{variable} is invalid: {detail['msg']}"
