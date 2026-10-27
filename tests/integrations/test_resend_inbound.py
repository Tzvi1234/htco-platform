"""Resend inbound's raw mail (slice 7.14), on a mock transport: the API key goes to Resend's API
only, the raw file is fetched over HTTPS with a size cap, and failures retry. Values invented."""

import httpx
import pytest

from htco_integrations.resend_inbound import (
    MAX_RAW_BYTES,
    ReceivedMailError,
    ResendReceivedMails,
)

pytestmark = pytest.mark.anyio

EMAIL_ID = "4ef9a417-02e9-4d39-ad75-9611e0fcc33c"
RAW_URL = "https://raw.resend.example.net/mail.eml?sig=abc"
KEY = "re_fake_key_0001"


def mails(handler: httpx.MockTransport) -> ResendReceivedMails:
    return ResendReceivedMails(httpx.AsyncClient(transport=handler), api_key=KEY)


def resend(
    raw: bytes = b"From: a@b.example\n\nbody\n", *, url: str = RAW_URL
) -> httpx.MockTransport:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "api.resend.com":
            assert request.url.path == f"/emails/receiving/{EMAIL_ID}"
            assert request.headers["Authorization"] == f"Bearer {KEY}"
            return httpx.Response(200, json={"id": EMAIL_ID, "raw": {"download_url": url}})
        assert "Authorization" not in request.headers  # the key never leaves Resend's API
        return httpx.Response(200, content=raw)

    transport = httpx.MockTransport(handle)
    transport.seen = seen  # type: ignore[attr-defined]
    return transport


async def test_raw_mail__fetched_from_the_signed_url() -> None:
    assert await mails(resend()).raw_mail(EMAIL_ID) == "From: a@b.example\n\nbody\n"


async def test_raw_mail__a_url_that_is_not_https__refused() -> None:
    with pytest.raises(ReceivedMailError):
        await mails(resend(url="http://raw.resend.example.net/mail.eml")).raw_mail(EMAIL_ID)


async def test_raw_mail__over_the_cap__refused() -> None:
    with pytest.raises(ReceivedMailError, match="over 1 MiB"):
        await mails(resend(b"x" * (MAX_RAW_BYTES + 1))).raw_mail(EMAIL_ID)


async def test_raw_mail__resend_fails__a_retryable_job_error_naming_the_status() -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(503))
    with pytest.raises(ReceivedMailError, match="resend 503"):
        await mails(transport).raw_mail(EMAIL_ID)


async def test_raw_mail__not_an_email_id__nothing_is_asked() -> None:
    transport = resend()
    with pytest.raises(ReceivedMailError):
        await mails(transport).raw_mail("../../emails")
    assert transport.seen == []  # type: ignore[attr-defined]


def test_the_key__never_in_a_repr() -> None:
    assert KEY not in repr(mails(resend()))
