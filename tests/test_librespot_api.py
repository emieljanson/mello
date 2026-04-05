"""
Tests for LibrespotAPI transport/status behavior.
"""
from pathlib import Path
from unittest.mock import MagicMock

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
