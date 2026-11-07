"""rest_api.py: the controller's REST API (Ryu's built-in web server, port 8080 on the VM).

    GET  /fortress/status    switches, links, learned hosts, mode, reroutes, blocked hosts
    GET  /fortress/blocked   just the blocked hosts and the recent block history
    POST /fortress/block     {"ip": ..., "reason": ..., "seconds": 300} cut a host off (T21)
    POST /fortress/unblock   {"ip": ...} let it back on

The threat engine (T20) is the normal caller of /block; a human can use the same endpoint with
curl, which is how the demo shows the block being lifted again.
"""
import json

from ryu.app.wsgi import ControllerBase, route
from webob import Response

APP_KEY = "fortress_ctrl"


class FortressRest(ControllerBase):
    def __init__(self, req, link, data, **config):
        super().__init__(req, link, data, **config)
        self.ctrl = data[APP_KEY]

    def json_response(self, data, status=200):
        return Response(status=status, content_type="application/json", charset="utf-8",
                        body=json.dumps(data, indent=1).encode())

    def body(self, req):
        try:
            return json.loads(req.body or b"{}")
        except ValueError:
            return {}

    @route("fortress", "/fortress/status", methods=["GET"])
    def status(self, req, **kwargs):
        return self.json_response(self.ctrl.status())

    @route("fortress", "/fortress/blocked", methods=["GET"])
    def blocked(self, req, **kwargs):
        return self.json_response(self.ctrl.responder.status())

    @route("fortress", "/fortress/block", methods=["POST"])
    def block(self, req, **kwargs):
        data = self.body(req)
        ip = data.get("ip")
        if not ip:
            return self.json_response({"error": 'give an address: {"ip": "192.168.40.66"}'}, 400)
        result = self.ctrl.responder.block(ip, data.get("reason", ""),
                                           float(data.get("seconds", 300)))
        return self.json_response(result, 400 if "error" in result else 200)

    @route("fortress", "/fortress/unblock", methods=["POST"])
    def unblock(self, req, **kwargs):
        data = self.body(req)
        if not data.get("ip"):
            return self.json_response({"error": 'give an address: {"ip": "192.168.40.66"}'}, 400)
        result = self.ctrl.responder.unblock(data["ip"])
        return self.json_response(result, 400 if "error" in result else 200)
