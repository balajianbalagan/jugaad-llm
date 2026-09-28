import pytest
from fastapi import HTTPException

from app.middleware.rate_limit import SlidingWindowLimiter


def test_rate_limit():
    limiter = SlidingWindowLimiter(1)
    limiter.check("client")
    with pytest.raises(HTTPException) as error:
        limiter.check("client")
    assert error.value.status_code == 429
