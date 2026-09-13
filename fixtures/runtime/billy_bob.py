"""Compatibility import for Worker 2 tests; the canonical fixture lives in the package."""

from businessbuilder.fixtures.billy_bob import FIXED_NOW, run_billy_bob, website_request

__all__ = ["FIXED_NOW", "run_billy_bob", "website_request"]
