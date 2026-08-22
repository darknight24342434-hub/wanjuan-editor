from __future__ import annotations

import unittest
from email.message import Message
from http import HTTPStatus

from src.server import allowed_local_host, mutation_request_error


class ServerBoundaryTests(unittest.TestCase):
    def test_only_local_hosts_are_allowed(self) -> None:
        self.assertTrue(allowed_local_host("127.0.0.1:8765", 8765))
        self.assertTrue(allowed_local_host("localhost:8765", 8765))
        self.assertFalse(allowed_local_host("evil.example:8765", 8765))

    def test_cross_site_and_plain_text_mutations_are_blocked(self) -> None:
        plain = Message()
        plain["Content-Type"] = "text/plain"
        issue = mutation_request_error(plain, 8765)
        self.assertEqual(issue[0], HTTPStatus.UNSUPPORTED_MEDIA_TYPE)

        cross_site = Message()
        cross_site["Content-Type"] = "application/json"
        cross_site["Origin"] = "https://evil.example"
        cross_site["Sec-Fetch-Site"] = "cross-site"
        issue = mutation_request_error(cross_site, 8765)
        self.assertEqual(issue[0], HTTPStatus.FORBIDDEN)

    def test_same_origin_json_mutation_is_allowed(self) -> None:
        headers = Message()
        headers["Content-Type"] = "application/json; charset=utf-8"
        headers["Origin"] = "http://127.0.0.1:8765"
        headers["Sec-Fetch-Site"] = "same-origin"
        self.assertIsNone(mutation_request_error(headers, 8765))


if __name__ == "__main__":
    unittest.main()
