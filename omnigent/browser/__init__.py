"""A server-owned browser that a headless agent drives and a person can watch.

See :mod:`omnigent.browser.gateway`. Kept as its own package because it owns OS
processes and profile directories, which is a different kind of thing from the
rest of the server.
"""

from omnigent.browser.gateway import BrowserGateway, BrowserUnavailable, gateway

__all__ = ["BrowserGateway", "BrowserUnavailable", "gateway"]
