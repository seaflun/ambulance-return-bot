"""HTTP transport policy for authenticated internal Worker APIs."""

from __future__ import annotations

import urllib.error
import urllib.request


class _NoWorkerApiRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "Worker API redirects are disabled", headers, fp)


def create_worker_api_opener() -> urllib.request.OpenerDirector:
    """Keep requests, tokens and acknowledgements on the selected API origin."""

    return urllib.request.build_opener(_NoWorkerApiRedirect())
