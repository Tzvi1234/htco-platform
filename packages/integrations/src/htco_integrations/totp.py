"""TOTP codes with PyOTP (ADR-0021, slice 5.17): the `TotpCodes` port at the integrations edge,
because the application layer imports only the standard library.

Codes are generated on the server only. A `secret_or_uri` is either a base32 secret or an
`otpauth://totp/...` URI; only SHA-1 TOTP (the authenticator-app default) is accepted. The
secret leaves this module only inside a `SecretValue`.
"""

from datetime import datetime

import pyotp

from htco_application.secrets import TotpParameters
from htco_domain.secrets import SecretValue

_DIGITS = (6, 8)
_MAX_PERIOD = 300


class PyOtpCodes:
    def parse(self, secret_or_uri: SecretValue) -> TotpParameters:
        raw = secret_or_uri.reveal().strip()
        if raw.lower().startswith("otpauth://"):
            try:
                parsed = pyotp.parse_uri(raw)
            except ValueError:
                raise ValueError("not a valid otpauth URI") from None
            if not isinstance(parsed, pyotp.TOTP) or parsed.digest().name != "sha1":
                raise ValueError("only SHA-1 TOTP accounts are supported")
            secret, digits, period = parsed.secret, parsed.digits, parsed.interval
        else:
            secret, digits, period = raw.replace(" ", "").upper(), 6, 30
        if digits not in _DIGITS or not 0 < period <= _MAX_PERIOD:
            raise ValueError("a TOTP account has 6 or 8 digits and a period up to 300 s")
        try:
            pyotp.TOTP(secret).byte_secret()
        except ValueError:
            raise ValueError("the secret is not base32") from None
        return TotpParameters(SecretValue(secret), digits, period)

    def code(self, parameters: TotpParameters, *, at: datetime) -> SecretValue:
        return SecretValue(self._totp(parameters).at(at))

    def verify(self, parameters: TotpParameters, code: str, *, at: datetime) -> bool:
        """One period of clock drift either way."""
        return self._totp(parameters).verify(code.strip(), for_time=at, valid_window=1)

    @staticmethod
    def _totp(parameters: TotpParameters) -> pyotp.TOTP:
        return pyotp.TOTP(
            parameters.secret.reveal(),
            digits=parameters.digits,
            interval=parameters.period_seconds,
        )
