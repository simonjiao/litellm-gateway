"""Exercise the derived image's page and version endpoints without external services."""

from __future__ import annotations

import unittest

import httpx
from open_webui.main import app


class FrontendTests(unittest.IsolatedAsyncioTestCase):
    async def test_entry_pages_and_version_revalidate_cached_responses(self):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://webui.test"
        ) as client:
            for path in ("/", "/c/existing-chat", "/_app/version.json"):
                with self.subTest(path=path):
                    response = await client.get(path)
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.headers["cache-control"], "no-cache")
                    cached = await client.get(
                        path, headers={"If-None-Match": response.headers["etag"]}
                    )
                    self.assertEqual(cached.status_code, 304)
                    self.assertEqual(cached.headers["cache-control"], "no-cache")
