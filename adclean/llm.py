# -*- coding: utf-8 -*-
"""adclean.llm —— OpenAI 兼容的第三方 LLM 接入（零依赖，只用标准库）。

api_key 不写日志、不出现在异常文本里；`as_public_dict()` 只返回掩码。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field

from .review import DECISION_UNCERTAIN, build_prompt, parse_decision

# ---------------------------------------------------------------- 默认参数
DEFAULT_TIMEOUT = 60.0
DEFAULT_RETRIES = 2
DEFAULT_CONCURRENCY = 2
DEFAULT_MAX_ITEMS = 50
RETRY_BACKOFF = (1.0, 2.0, 4.0)      # 秒，按重试次数取

def config_path() -> str:
    """配置文件路径（每次调用都读环境变量 `ADCLEAN_HOME`）。"""
    home = os.environ.get("ADCLEAN_HOME") or os.path.join(
        os.path.expanduser("~"), ".adclean")
    return os.path.join(home, "llm.json")


CONFIG_PATH = config_path()


class LLMError(Exception):
    """面向使用者的错误（消息里绝不含 api_key）。"""


# ---------------------------------------------------------------- 配置
@dataclass
class LLMConfig:
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    timeout: float = DEFAULT_TIMEOUT
    retries: int = DEFAULT_RETRIES
    concurrency: int = DEFAULT_CONCURRENCY
    max_items: int = DEFAULT_MAX_ITEMS
    temperature: float = 0.0
    max_tokens: int = 700
    json_mode: bool = True
    save_key: bool = True
    extra_headers: dict = field(default_factory=dict)

    # -- 端点归一化 -------------------------------------------------
    def _base(self) -> str:
        b = (self.base_url or "").strip().rstrip("/")
        for suf in ("/chat/completions", "/completions", "/models",
                    "/responses", "/embeddings"):
            if b.endswith(suf):
                b = b[: -len(suf)]
        return b.rstrip("/")

    def chat_url(self) -> str:
        b = self._base()
        if not b:
            raise LLMError("未填写 API 地址（base_url）")
        if not re.search(r"/v\d+[a-z]*$", b, re.I):
            b += "/v1"
        return b + "/chat/completions"

    def models_url(self) -> str:
        return self.chat_url()[: -len("/chat/completions")] + "/models"

    # -- 展示 / 持久化 ----------------------------------------------
    def masked_key(self) -> str:
        k = self.api_key or ""
        if not k:
            return ""
        if len(k) <= 8:
            return "*" * len(k)
        return k[:4] + "*" * 6 + k[-4:]

    def redact(self, text: str) -> str:
        """把文本里可能出现的密钥**整体**替换成 `***`（用于异常消息 / 日志 / 报告）。"""
        out = str(text)
        if self.api_key and len(self.api_key) >= 6:
            out = out.replace(self.api_key, "***")
        out = re.sub(r"sk-[A-Za-z0-9_\-]{6,}", "sk-***", out)
        out = re.sub(r"(Bearer\s+)\S+", r"\1***", out, flags=re.I)
        return out

    def as_public_dict(self) -> dict:
        """给前端：**不含** api_key 明文。"""
        d = asdict(self)
        d.pop("api_key", None)
        d["has_key"] = bool(self.api_key)
        d["key_masked"] = self.masked_key()
        d["chat_url"] = ""
        d["models_url"] = ""
        try:
            d["chat_url"] = self.chat_url()
            d["models_url"] = self.models_url()
        except LLMError:
            pass
        return d

    def clamp(self) -> "LLMConfig":
        """把参数夹到安全范围。"""
        self.timeout = max(1.0, min(600.0, float(self.timeout or DEFAULT_TIMEOUT)))
        self.retries = max(0, min(6, int(self.retries)))
        self.concurrency = max(1, min(16, int(self.concurrency)))
        self.max_items = max(0, int(self.max_items))       # 0 = 不限
        self.temperature = max(0.0, min(2.0, float(self.temperature or 0.0)))
        self.max_tokens = max(64, min(8192, int(self.max_tokens or 700)))
        return self


# ---------------------------------------------------------------- 持久化
def load_config(path: str = "") -> LLMConfig:
    p = path or config_path()
    try:
        with open(p, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return LLMConfig()
    known = set(LLMConfig.__dataclass_fields__)
    kw = {k: v for k, v in raw.items() if k in known}
    return LLMConfig(**kw).clamp()


def save_config(cfg: LLMConfig, path: str = "") -> str:
    p = path or config_path()
    d = os.path.dirname(p)
    if d:
        os.makedirs(d, exist_ok=True)
    data = asdict(cfg)
    if not cfg.save_key:
        data["api_key"] = ""
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)
    try:                                 # POSIX 上收紧权限；Windows 尽力而为
        os.chmod(p, 0o600)
    except OSError:
        pass
    return p


# ---------------------------------------------------------------- HTTP
def _request(url: str, cfg: LLMConfig, payload=None, method: str | None = None,
             timeout: float | None = None) -> tuple:
    """发一次请求，返回 (status, text)。不重试——重试由上层控制。"""
    data = None
    if method is None:
        method = "POST" if payload is not None else "GET"
    headers = {
        "Accept": "application/json",
        "User-Agent": "adclean/0.2",
    }
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if cfg.api_key:
        headers["Authorization"] = "Bearer " + cfg.api_key
    for k, v in (cfg.extra_headers or {}).items():
        headers[str(k)] = str(v)
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout or cfg.timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")
        except (OSError, ValueError):
            pass
        return e.code, body
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        if isinstance(reason, TimeoutError) or "timed out" in str(reason).lower():
            raise LLMError("请求超时（%.0fs）" % (timeout or cfg.timeout))
        raise LLMError("无法连接：%s" % reason)
    except TimeoutError:
        raise LLMError("请求超时（%.0fs）" % (timeout or cfg.timeout))


def _brief(body: str, limit: int = 300) -> str:
    s = re.sub(r"\s+", " ", (body or "").strip())
    return s[:limit]


def _http_error(status: int, body: str, cfg: LLMConfig) -> LLMError:
    msg = cfg.redact(_brief(body))
    if status == 401:
        return LLMError("401 未授权：API Key 无效或已过期。%s" % msg)
    if status == 403:
        return LLMError("403 拒绝访问：Key 无权限或该模型未开通。%s" % msg)
    if status == 404:
        return LLMError("404 找不到接口：API 地址可能不对（%s）。%s"
                        % (cfg.redact(cfg.chat_url()), msg))
    if status == 429:
        return LLMError("429 触发限流：请降低并发或稍后重试。%s" % msg)
    return LLMError("HTTP %d：%s" % (status, msg))


_RETRY_STATUS = (408, 409, 425, 429, 500, 502, 503, 504)


def _with_retry(cfg: LLMConfig, fn, on_retry=None):
    """按 cfg.retries 次指数退避重试。fn() 返回结果或抛 LLMError。"""
    last = None
    for attempt in range(cfg.retries + 1):
        try:
            return fn()
        except LLMError as e:
            last = e
            if attempt >= cfg.retries:
                break
            wait = RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)]
            if on_retry:
                on_retry(attempt + 1, wait, str(e))
            time.sleep(wait)
    raise last


# ---------------------------------------------------------------- 模型列表
def list_models(cfg: LLMConfig) -> list:
    """GET {base}/v1/models，返回模型 id 列表（排序）。免费调用。"""
    cfg = cfg.clamp()
    url = cfg.models_url()

    def call():
        status, body = _request(url, cfg)
        if status >= 400:
            raise _http_error(status, body, cfg)
        try:
            data = json.loads(body)
        except ValueError:
            raise LLMError("返回不是合法 JSON：%s" % cfg.redact(_brief(body)))
        items = data.get("data") if isinstance(data, dict) else data
        if not isinstance(items, list):
            raise LLMError("返回结构里没有 data 数组：%s" % cfg.redact(_brief(body)))
        ids = []
        for it in items:
            if isinstance(it, dict):
                mid = it.get("id") or it.get("name") or it.get("model")
            else:
                mid = it
            if mid:
                ids.append(str(mid))
        return sorted(set(ids))

    return _with_retry(cfg, call)


# ---------------------------------------------------------------- 单次对话
def chat(cfg: LLMConfig, messages: list, json_mode: bool | None = None,
         max_tokens: int | None = None) -> str:
    """POST /chat/completions，返回 assistant 文本。含重试与 json 模式降级。"""
    cfg = cfg.clamp()
    url = cfg.chat_url()
    use_json = cfg.json_mode if json_mode is None else json_mode
    payload = {
        "model": cfg.model,
        "messages": messages,
        "temperature": cfg.temperature,
        "max_tokens": max_tokens or cfg.max_tokens,
        "stream": False,
    }
    if not cfg.model:
        raise LLMError("未选择模型（model）")
    if use_json:
        payload["response_format"] = {"type": "json_object"}

    def call():
        status, body = _request(url, cfg, payload)
        if status >= 400:
            # 部分服务不支持 response_format → 去掉再试一次
            if "response_format" in payload and status in (400, 404, 422):
                payload.pop("response_format", None)
                status, body = _request(url, cfg, payload)
            if status >= 400:
                raise _http_error(status, body, cfg)
        try:
            data = json.loads(body)
        except ValueError:
            raise LLMError("返回不是合法 JSON：%s" % cfg.redact(_brief(body)))
        try:
            msg = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError):
            raise LLMError("返回里没有 choices[0].message：%s"
                           % cfg.redact(_brief(body)))
        content = msg.get("content")
        if content is None:
            content = msg.get("reasoning_content") or ""
        if isinstance(content, list):        # 少数服务返回分片数组
            content = "".join(str(c.get("text", "")) if isinstance(c, dict)
                              else str(c) for c in content)
        return str(content)

    return _with_retry(cfg, call)


def probe_chat(cfg: LLMConfig) -> dict:
    """最小连通性测试（会消耗极少 token，由使用者主动触发）。"""
    out = chat(cfg, [
        {"role": "system", "content": "只输出 JSON，不要任何多余文字。"},
        {"role": "user", "content": '回复 {"ok": true}'},
    ], max_tokens=24)
    return {"model": cfg.model, "raw": out[:300],
            "parsed": parse_decision(out)}


# ---------------------------------------------------------------- Resolver
class OpenAICompatResolver:
    """实现 `adclean.review.LLMResolver`：并发预解析审查队列。

    pipeline 先调 `prepare()` 把所有条目并发问完并缓存，之后逐条 `resolve()`
    只读缓存，不产生网络请求。
    """

    name = "openai-compat"

    def __init__(self, cfg: LLMConfig, progress=None, cancel=None) -> None:
        self.cfg = cfg.clamp()
        self.decisions: dict = {}
        self.progress = progress          # callable(done, total, ok, fail)
        self.cancel = cancel              # callable() -> bool
        self.stats = {"total": 0, "ok": 0, "fail": 0, "skipped": 0, "retried": 0}
        self.errors: list = []
        self._lock = threading.Lock()

    # -- 批量预解析 --------------------------------------------------
    def prepare(self, items: list, progress=None) -> dict:
        cap = self.cfg.max_items
        todo = items if not cap else items[:cap]
        self.stats["total"] = len(items)
        self.stats["skipped"] = len(items) - len(todo)
        done = 0
        cb = progress or self.progress
        with ThreadPoolExecutor(max_workers=self.cfg.concurrency) as ex:
            futs = {ex.submit(self._one, it): it for it in todo}
            for fut in as_completed(futs):
                it = futs[fut]
                try:
                    dec = fut.result()
                    with self._lock:
                        self.decisions[it.item_id] = dec
                        self.stats["ok"] += 1
                except Exception as e:                      # noqa: BLE001
                    msg = self.cfg.redact(str(e))
                    with self._lock:
                        # 失败条目 → uncertain（保持原文，绝不猜）
                        self.decisions[it.item_id] = {
                            "decision": DECISION_UNCERTAIN, "confidence": 0.0,
                            "reason": "调用失败：%s" % msg}
                        self.stats["fail"] += 1
                        if len(self.errors) < 30:
                            self.errors.append({"item_id": it.item_id,
                                                "error": msg})
                done += 1
                if cb:
                    try:
                        cb(done, len(todo), self.stats["ok"], self.stats["fail"])
                    except Exception:                        # noqa: BLE001
                        pass
        return dict(self.stats)

    def _one(self, item) -> dict:
        if self.cancel and self.cancel():
            return {"decision": DECISION_UNCERTAIN, "confidence": 0.0,
                    "reason": "已取消"}
        prompt = build_prompt(item)
        raw = chat(self.cfg, [
            {"role": "system",
             "content": "你是中文小说文本校对助手。严格按要求只输出 JSON。"},
            {"role": "user", "content": prompt},
        ])
        return parse_decision(raw)

    # -- 协议方法 ----------------------------------------------------
    def review(self, item) -> dict:
        """稳定入口：review(review_item) -> structured decision。"""
        return self.resolve(item)

    def resolve(self, item) -> dict:
        d = self.decisions.get(item.item_id)
        if d is not None:
            return d
        return {"decision": DECISION_UNCERTAIN, "confidence": 0.0,
                "reason": "未送审（超出单次上限或未启用 LLM）"}

    @property
    def model(self) -> str:
        return self.cfg.model

    def as_dict(self) -> dict:
        return {"resolver": self.name, "model": self.cfg.model,
                "chat_url": self.cfg.redact(self.cfg.chat_url()),
                **self.stats, "errors": self.errors[:10]}
