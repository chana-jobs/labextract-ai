"""Tests of review, export and storage logic run as a signed-in test user in the default (local)
workspace. Tests marked `real_auth` sign in through /auth/login instead (tests/test_api_auth.py)."""
import pytest

from backend.app import app
from backend.auth import Principal, require_principal
from backend.storage import DEFAULT_WORKSPACE


def pytest_configure(config):
    config.addinivalue_line("markers", "real_auth: sign in through the real /auth/login flow")


@pytest.fixture(autouse=True)
def _test_principal(request):
    if request.node.get_closest_marker("real_auth"):
        yield
        return
    app.dependency_overrides[require_principal] = lambda: Principal("test-user", DEFAULT_WORKSPACE)
    yield
    app.dependency_overrides.pop(require_principal, None)
