from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from hermes_wecom_code_governor.http_action import HttpActionRunner
from hermes_wecom_code_governor.policy import HttpAction, HttpActionParameter


def light_action(url: str = "http://example.test/v1/lights/{light}/command") -> HttpAction:
    return HttpAction(
        name="设置警示灯颜色",
        method="POST",
        url=url,
        body_template='{{"action":"color","color":"{color}","mode":"solid"}}',
        parameters=(
            HttpActionParameter(name="light", type="integer", minimum=1, maximum=9),
            HttpActionParameter(
                name="color",
                type="choice",
                choices=("red", "green", "blue"),
            ),
        ),
    )


def test_valid_parameters_render_url_and_json_body() -> None:
    url, body = HttpActionRunner.build_request(light_action(), {"light": 3, "color": "blue"})

    assert url == "http://example.test/v1/lights/3/command"
    assert json.loads(body) == {"action": "color", "color": "blue", "mode": "solid"}


@pytest.mark.parametrize(
    "params",
    [
        {"light": 3},  # 缺参数
        {"light": 3, "color": "blue", "extra": "x"},  # 多余参数
        {"light": 0, "color": "blue"},  # 越界
        {"light": 10, "color": "blue"},  # 越界
        {"light": "3; rm -rf", "color": "blue"},  # 非整数
        {"light": 3, "color": "magenta"},  # 不在枚举
        {"light": 3, "color": 'blue"},{"evil":1'},  # 注入尝试
    ],
)
def test_bad_parameters_fail_closed(params: dict) -> None:
    with pytest.raises((ValueError, PermissionError)):
        HttpActionRunner.build_request(light_action(), params)


def test_integer_strings_from_the_model_are_accepted() -> None:
    # 外层模型经常把数字参数序列化成字符串，"3" 应当等价于 3。
    url, _body = HttpActionRunner.build_request(light_action(), {"light": "3", "color": "red"})

    assert url == "http://example.test/v1/lights/3/command"


class _RecordingHandler(BaseHTTPRequestHandler):
    requests: list[tuple[str, str, str]] = []

    def do_GET(self) -> None:  # noqa: N802
        type(self).requests.append(("GET", self.path, ""))
        if self.path.endswith("/offline"):
            # 真实灯网关对没有 TCP 客户端连上的灯就是回 409 + 错误体。
            payload = json.dumps({"error": "light-offline"}).encode()
            self.send_response(409)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(payload)
            return
        payload = json.dumps({"lights": [{"id": 1, "online": True}]}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("content-length", "0"))
        body = self.rfile.read(length).decode("utf-8")
        type(self).requests.append(("POST", self.path, body))
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"sent":"0102"}')

    def log_message(self, *args: object) -> None:
        return


@pytest.fixture()
def http_server():
    server = HTTPServer(("127.0.0.1", 0), _RecordingHandler)
    _RecordingHandler.requests = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def test_runner_posts_rendered_body_and_returns_response(http_server: str) -> None:
    action = light_action(url=f"{http_server}/v1/lights/{{light}}/command")

    result = HttpActionRunner().run(action, {"light": 2, "color": "green"})

    assert result.status == 200
    assert json.loads(result.body) == {"sent": "0102"}
    assert result.error == ""
    method, path, body = _RecordingHandler.requests[0]
    assert (method, path) == ("POST", "/v1/lights/2/command")
    assert json.loads(body) == {"action": "color", "color": "green", "mode": "solid"}


def test_runner_gets_status_without_parameters(http_server: str) -> None:
    action = HttpAction(name="查看警示灯状态", method="GET", url=f"{http_server}/v1/lights")

    result = HttpActionRunner().run(action, {})

    assert result.status == 200
    assert "online" in result.body
    assert _RecordingHandler.requests[0][0] == "GET"


def test_error_response_keeps_the_gateway_status_and_body(http_server: str) -> None:
    action = HttpAction(name="查看警示灯状态", method="GET", url=f"{http_server}/v1/lights/offline")

    result = HttpActionRunner().run(action, {})

    assert result.status == 409
    assert json.loads(result.body) == {"error": "light-offline"}


def test_connection_failure_returns_error_instead_of_raising() -> None:
    action = HttpAction(
        name="查看警示灯状态",
        method="GET",
        url="http://127.0.0.1:1/v1/lights",
        timeout_seconds=2,
    )

    result = HttpActionRunner().run(action, {})

    assert result.status == 0
    assert result.error != ""
