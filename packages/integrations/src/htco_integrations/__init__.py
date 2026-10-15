"""HTCO integrations: adapters for Etsy (and later AutoVA and email) behind application ports.

Each adapter maps one provider's HTTP API to a port in `htco_application`, so use cases never see
provider payloads. Outgoing clients come from `htco_platform.http` (timeouts, no redirects,
trace propagation).
"""
