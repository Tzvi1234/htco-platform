"""Resend inbound (slice 7.14): the raw quote mail behind an `email.received` notice.

`ResendReceivedMails.raw_mail` asks `GET https://api.resend.com/emails/receiving/{id}` for the
received email, then downloads its original file from the signed `raw.download_url` it returns
(HTTPS only, no redirects followed, no API key sent there, at most 1 MiB). The mail is returned
to the job and never logged; errors name the status, never the content or the URL. A failed
fetch is a `ReceivedMailError` (the job retries); a mail over the cap is `RawMailTooLargeError`,
permanent, and the job opens a Human task instead.
"""

import re
from typing import Any, override
from urllib.parse import urlsplit

import httpx

from htco_application.kernel import JobError
from htco_application.quote_mailbox import RawMailTooLargeError

RECEIVING_URL = "https://api.resend.com/emails/receiving/"
MAX_RAW_BYTES = 1024 * 1024
_EMAIL_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class ReceivedMailError(JobError):
    """The mail could not be fetched; the job retries. Its message is safe to store."""


class ResendReceivedMails:
    def __init__(self, http: httpx.AsyncClient, *, api_key: str, timeout: float = 10.0) -> None:
        if not api_key.startswith("re_"):
            raise ValueError("a Resend API key starts with re_")
        self._http = http
        self._api_key = api_key
        self._timeout = timeout

    @override
    def __repr__(self) -> str:
        return "ResendReceivedMails(<key hidden>)"

    async def raw_mail(self, email_id: str) -> str:
        if _EMAIL_ID.match(email_id) is None:
            raise ReceivedMailError("not a Resend email ID")
        try:
            response = await self._http.get(
                RECEIVING_URL + email_id,
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=self._timeout,
            )
        except httpx.HTTPError as error:
            raise ReceivedMailError(f"resend {type(error).__name__}") from None
        if not response.is_success:
            raise ReceivedMailError(f"resend {response.status_code}")
        url = _download_url(response)
        return await self._download(url)

    async def _download(self, url: str) -> str:
        chunks: list[bytes] = []
        size = 0
        try:
            async with self._http.stream(
                "GET", url, timeout=self._timeout, follow_redirects=False
            ) as response:
                if not response.is_success:  # a redirect too: it is never followed
                    raise ReceivedMailError(f"resend raw {response.status_code}")
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_RAW_BYTES:
                        # Permanent: retrying never makes it fit (the job opens a Human task).
                        raise RawMailTooLargeError("resend raw mail over 1 MiB")
                    chunks.append(chunk)
        except httpx.HTTPError as error:
            raise ReceivedMailError(f"resend raw {type(error).__name__}") from None
        return b"".join(chunks).decode("utf-8", errors="replace")


def _download_url(response: httpx.Response) -> str:
    try:
        body: Any = response.json()
        url = body["raw"]["download_url"]
    except ValueError, KeyError, TypeError:
        raise ReceivedMailError("resend answered without a raw mail") from None
    if not isinstance(url, str) or urlsplit(url).scheme != "https" or not urlsplit(url).hostname:
        raise ReceivedMailError("resend's raw mail URL is not HTTPS")
    return url
