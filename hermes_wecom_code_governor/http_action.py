from __future__ import annotations

import urllib.error
import urllib.request
from dataclasses import dataclass

from .execution import MAX_OUTPUT_CHARS
from .policy import HttpAction

# 灯网关这类内部服务响应都很小；上限只是防异常服务把上下文撑爆。
_MAX_BODY_BYTES = 256 * 1024


@dataclass(frozen=True)
class HttpActionResult:
    status: int
    body: str
    error: str = ""


class HttpActionRunner:
    """执行预登记 HTTP 动作：URL 与请求体来自配置模板，参数逐个过白名单。

    模型只能按名称触发并提供声明过的参数；多余参数、缺失参数、越界整数、
    枚举之外的取值一律拒绝。choice 值在配置加载时已限制为无注入面的字符集，
    渲染进 URL/JSON 模板是安全的纯文本替换。
    """

    @staticmethod
    def _rendered_values(action: HttpAction, params: dict[str, object]) -> dict[str, str]:
        declared = {parameter.name: parameter for parameter in action.parameters}
        extra = set(params) - set(declared)
        if extra:
            raise PermissionError(f"unknown parameters for this action: {sorted(extra)}")
        values: dict[str, str] = {}
        for name, parameter in declared.items():
            if name not in params:
                raise ValueError(f"missing required parameter: {name}")
            raw = params[name]
            if parameter.type == "integer":
                # 外层模型常把数字序列化成字符串，等价接受；bool 是 int 子类，排除。
                if isinstance(raw, bool) or not isinstance(raw, (int, str)):
                    raise ValueError(f"parameter {name} must be an integer")
                try:
                    value = int(str(raw).strip())
                except ValueError as error:
                    raise ValueError(f"parameter {name} must be an integer") from error
                assert parameter.minimum is not None and parameter.maximum is not None
                if not parameter.minimum <= value <= parameter.maximum:
                    raise ValueError(
                        f"parameter {name} must be between "
                        f"{parameter.minimum} and {parameter.maximum}"
                    )
                values[name] = str(value)
            else:
                if raw not in parameter.choices:
                    raise ValueError(
                        f"parameter {name} must be one of: {', '.join(parameter.choices)}"
                    )
                values[name] = str(raw)
        return values

    @staticmethod
    def build_request(action: HttpAction, params: dict[str, object]) -> tuple[str, str | None]:
        values = HttpActionRunner._rendered_values(action, params)
        url = action.url.format(**values)
        body = action.body_template.format(**values) if action.body_template else None
        return url, body

    def run(self, action: HttpAction, params: dict[str, object]) -> HttpActionResult:
        url, body = self.build_request(action, params)
        request = urllib.request.Request(
            url,
            method=action.method,
            data=body.encode("utf-8") if body is not None else None,
            headers={"content-type": "application/json"} if body is not None else {},
        )
        # 直连目标：忽略本机代理环境变量，治理动作不经代理转发（Fake-IP 代理
        # 会吞掉连接失败并伪装成 503，也可能拦截内网目标）。
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=action.timeout_seconds) as response:
                text = response.read(_MAX_BODY_BYTES).decode("utf-8", errors="replace")
                return HttpActionResult(status=response.status, body=text[:MAX_OUTPUT_CHARS])
        except urllib.error.HTTPError as error:
            text = error.read(_MAX_BODY_BYTES).decode("utf-8", errors="replace")
            return HttpActionResult(status=error.code, body=text[:MAX_OUTPUT_CHARS])
        except Exception as error:  # noqa: BLE001 - 网络层失败统一转结果
            return HttpActionResult(status=0, body="", error=str(error))
