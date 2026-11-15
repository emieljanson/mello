"""
Tests for LibrespotAPI transport/status behavior.
"""
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from mello.api.librespot import LibrespotAPI


def test_status_204_returns_explicit_stopped_payload():
    api = LibrespotAPI("http://localhost:3678")
    resp = MagicMock()
    resp.status_code = 204
    api.session.get = MagicMock(return_value=resp)

    status = api.status()

    assert status == {
        "stopped": True,
        "paused": False,
        "context_uri": None,
        "track": None,
    }


def test_status_request_exception_returns_none():
    api = LibrespotAPI("http://localhost:3678")
    api.session.get = MagicMock(side_effect=requests.RequestException("boom"))

    status = api.status()

    assert status is None


@pytest.mark.parametrize('code, body', [
    (503, b'{"error": "unavailable"}'),
    (200, b'{"error": "unavailable"}'),
    (200, b'[]'),
])
def test_status_rejects_error_responses_and_non_status_json(code, body):
    api = LibrespotAPI('http://localhost:3678')
    response = requests.Response()
    response.status_code = code
    response._content = body
    api.session.get = MagicMock(return_value=response)

    assert api.status() is None


def test_set_repeat_context_posts_expected_body():
    api = LibrespotAPI("http://localhost:3678")
    resp = MagicMock()
    resp.ok = True
    resp.status_code = 200
    api.session.post = MagicMock(return_value=resp)

    result = api.set_repeat_context(True)

    assert result is True
    api.session.post.assert_called_once_with(
        "http://localhost:3678/player/repeat_context",
        json={"repeat_context": True},
        timeout=2,
    )


def test_set_repeat_context_failure_returns_false():
    api = LibrespotAPI("http://localhost:3678")
    resp = MagicMock()
    resp.ok = False
    resp.status_code = 500
    resp.text = "boom"
    api.session.post = MagicMock(return_value=resp)

    result = api.set_repeat_context(True)

    assert result is False


def test_set_repeat_context_request_exception_returns_false():
    api = LibrespotAPI("http://localhost:3678")
    api.session.post = MagicMock(side_effect=requests.RequestException("boom"))

    result = api.set_repeat_context(True)

    assert result is False
