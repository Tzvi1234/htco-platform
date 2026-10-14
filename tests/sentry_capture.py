"""A Sentry transport that keeps events in memory instead of sending them."""

import json
from typing import Any, override

from sentry_sdk.envelope import Envelope
from sentry_sdk.transport import Transport

# A syntactically valid DSN for a host that cannot exist (RFC 6761 `.invalid`).
FAKE_DSN = "https://public@o0.ingest.example.invalid/1"


class CapturingTransport(Transport):
    def __init__(self) -> None:
        super().__init__({"dsn": None})
        self.events: list[dict[str, Any]] = []

    @override
    def capture_envelope(self, envelope: Envelope) -> None:
        for item in envelope.items:
            event = item.get_event()
            if event is not None:
                self.events.append(dict(event))

    def serialized(self) -> str:
        return json.dumps(self.events, default=str)
