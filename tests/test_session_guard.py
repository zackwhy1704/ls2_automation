"""Tests for the Synergix session-expiry guardrail detection.

Synergix's session times out mid-batch, landing every action on a 'your page has expired' screen.
_is_session_expired() must recognise that screen (so the driver can re-login) and NOT false-positive
on a healthy app page.
"""
from __future__ import annotations

import asyncio

import pytest

from src.synergix_driver import SynergixDriver

pytest.importorskip("playwright")


def _detect(html: str) -> bool:
    from playwright.async_api import async_playwright

    async def run() -> bool:
        d = SynergixDriver()
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            d.page = await browser.new_page()
            await d.page.set_content(html)
            try:
                return await d._is_session_expired()
            finally:
                await browser.close()

    return asyncio.run(run())


def test_detects_expired_page():
    html = ("<body>Synergix Software. You've left your browser idle for too long and your page "
            "has expired. Reload Page</body>")
    assert _detect(html) is True


def test_healthy_page_not_flagged():
    assert _detect("<body><div>Service Quotation - LS2</div><div>Enquiry/Subject</div></body>") is False
