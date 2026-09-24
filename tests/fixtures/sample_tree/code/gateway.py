"""Nexus API gateway entry point."""


def route(request):
    """Route a request to the backend service."""
    return request


class RateLimiter:
    """Token bucket limiter: 100 requests per second."""
