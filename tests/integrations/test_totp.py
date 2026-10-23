"""The PyOTP adapter of the `TotpCodes` port (slice 5.17, ADR-0021): RFC 6238 vectors."""

import base64
from datetime import UTC, datetime, timedelta

import pytest

from htco_application.secrets import TotpParameters
from htco_domain.secrets import SecretValue
from htco_integrations.totp import PyOtpCodes

# RFC 6238 appendix B: the ASCII secret "12345678901234567890", base32-encoded, SHA-1.
RFC_SECRET = base64.b32encode(b"12345678901234567890").decode()
AT_59 = datetime.fromtimestamp(59, UTC)


def test_code__matches_the_rfc_6238_vectors() -> None:
    codes = PyOtpCodes()

    eight = codes.code(TotpParameters(SecretValue(RFC_SECRET), digits=8), at=AT_59)
    later = codes.code(
        TotpParameters(SecretValue(RFC_SECRET), digits=8),
        at=datetime.fromtimestamp(1111111109, UTC),
    )

    assert (eight.reveal(), later.reveal()) == ("94287082", "07081804")
    assert isinstance(eight, SecretValue)


def test_verify__the_current_code_and_one_period_of_drift() -> None:
    codes = PyOtpCodes()
    parameters = TotpParameters(SecretValue(RFC_SECRET))
    code = codes.code(parameters, at=AT_59).reveal()

    assert codes.verify(parameters, code, at=AT_59)
    assert codes.verify(parameters, code, at=AT_59 + timedelta(seconds=30))
    assert not codes.verify(parameters, code, at=AT_59 + timedelta(seconds=90))
    assert not codes.verify(parameters, "000000", at=AT_59)


def test_parse__a_plain_secret_or_an_otpauth_uri() -> None:
    codes = PyOtpCodes()

    plain = codes.parse(SecretValue("gezd gnbv gy3t qojq gezd gnbv gy3t qojq"))
    uri = codes.parse(
        SecretValue(f"otpauth://totp/Etsy:shop?secret={RFC_SECRET}&issuer=Etsy&digits=8&period=60")
    )

    assert (plain.secret.reveal(), plain.digits, plain.period_seconds) == (RFC_SECRET, 6, 30)
    assert (uri.secret.reveal(), uri.digits, uri.period_seconds) == (RFC_SECRET, 8, 60)


@pytest.mark.parametrize(
    "raw",
    [
        "not base32 !!",
        f"otpauth://hotp/Etsy?secret={RFC_SECRET}&counter=1",
        f"otpauth://totp/Etsy?secret={RFC_SECRET}&algorithm=SHA512",
        f"otpauth://totp/Etsy?secret={RFC_SECRET}&digits=7",
    ],
)
def test_parse__unsupported__refused(raw: str) -> None:
    with pytest.raises(ValueError):  # noqa: PT011 - each case has its own message
        PyOtpCodes().parse(SecretValue(raw))


def test_parse__a_refusal_never_echoes_the_secret() -> None:
    with pytest.raises(ValueError) as refused:  # noqa: PT011
        PyOtpCodes().parse(SecretValue("otpauth://totp/x?secret=ZZZZ-NOT-BASE32"))

    assert "ZZZZ" not in str(refused.value)
