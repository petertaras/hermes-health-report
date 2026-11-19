import io
import unittest
import urllib.error
import urllib.request
import urllib.response
from email.message import Message
from unittest.mock import patch

import probes


class RedirectTransport(urllib.request.BaseHandler):
    handler_order = 100

    def __init__(self, locations):
        self.locations = locations
        self.visited = []

    def http_open(self, request):
        self.visited.append(request.full_url)
        headers = Message()
        location = self.locations.get(request.full_url)
        if location:
            headers['Location'] = location
        response = urllib.response.addinfourl(io.BytesIO(b'{"status":"healthy"}'), headers,
                                             request.full_url, 302 if location else 200)
        response.msg = 'Found' if location else 'OK'
        return response

    https_open = http_open


class HindsightRedirectTests(unittest.TestCase):
    def test_origin_comparison(self):
        origin = probes._http_origin('https://EXAMPLE.test/path')
        self.assertEqual(origin, probes._http_origin('https://example.test:443/other'))
        for destination in ('http://example.test', 'https://other.test',
                            'https://example.test:444', 'https://example.test:bad',
                            'https://user@example.test', 'https://example.test\\@other.test'):
            with self.subTest(destination=destination):
                self.assertNotEqual(origin, probes._http_origin(destination))

    def test_same_origin_hops_then_cross_origin_is_blocked(self):
        transport = RedirectTransport({
            'http://origin.test/health': '/next',
            'http://origin.test/next': 'http://origin.test:80/final',
            'http://origin.test:80/final': 'http://origin.test:81/other',
        })
        real_build = urllib.request.build_opener
        with patch('urllib.request.build_opener', side_effect=lambda *handlers: real_build(urllib.request.ProxyHandler({}), *handlers, transport)):
            result = probes._hindsight_api_health('http://origin.test', {})
        self.assertEqual(result, ('warn', 'health unavailable'))
        self.assertEqual(transport.visited, ['http://origin.test/health', 'http://origin.test/next',
                                            'http://origin.test:80/final'])

    def test_same_origin_redirect_can_succeed(self):
        transport = RedirectTransport({'https://origin.test/health': '/healthy'})
        real_build = urllib.request.build_opener
        with patch('urllib.request.build_opener', side_effect=lambda *handlers: real_build(urllib.request.ProxyHandler({}), *handlers, transport)):
            result = probes._hindsight_api_health('https://origin.test', {})
        self.assertEqual(result, ('ok', 'healthy'))
        self.assertEqual(transport.visited, ['https://origin.test/health', 'https://origin.test/healthy'])

    def test_cross_origin_redirect_is_not_requested(self):
        transport = RedirectTransport({'http://origin.test/health': 'http://other.test/health'})
        real_build = urllib.request.build_opener
        with patch('urllib.request.build_opener', side_effect=lambda *handlers: real_build(urllib.request.ProxyHandler({}), *handlers, transport)):
            result = probes._hindsight_api_health('http://origin.test', {})
        self.assertEqual(result, ('warn', 'health unavailable'))
        self.assertEqual(transport.visited, ['http://origin.test/health'])
