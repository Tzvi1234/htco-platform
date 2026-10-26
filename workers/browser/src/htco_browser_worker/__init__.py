"""HTCO browser-worker (Phase 8A, ADR-0020): the Browser channel's own component.

It runs Chromium through Playwright on one shop's unpacked profile at a time, with its own
database role (`htco_browser`), its own profile key ring and no Etsy OAuth tokens. Only this
package imports Playwright. No business rules live here.
"""
