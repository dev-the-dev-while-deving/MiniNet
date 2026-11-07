"""rest_api.py: the controller's REST API (Ryu's built-in web server, port 8080 on the VM).

    GET /fortress/status   switches, links, learned hosts, mode, reroute count
                           (used by topology/checks.py and, later, the dashboard)
Block/unblock endpoints are added here in T21 (decisions D14).
"""
import json

from ryu.app.wsgi import ControllerBase, route
from webob import Response

APP_KEY = "fortress_ctrl"


class FortressRest(ControllerBase):
    def __init__(self, req, link, data, **config):
        super().__init__(req, link, data, **config)
        self.ctrl = data[APP_KEY]

    @route("fortress", "/fortress/status", methods=["GET"])
    def status(self, req, **kwargs):
        return Response(content_type="application/json", charset="utf-8",
                        body=json.dumps(self.ctrl.status(), indent=1).encode())
