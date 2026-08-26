"""Shared DeepSeek vision client for the ecom-video-seedance-prompt skill.

封装了实测验证过的所有 API 细节与坑（详见 references/api-notes.md）：
- deepseek-v4-flash-vision-exp 默认开启推理，reasoning tokens 计入 max_tokens，
  小 max_tokens 会得到空 content；用 extra_body={"thinking": {"type": "disabled"}} 关闭。
- JSON Output 偶发空 content，必须重试。
- 视频场景实测（video-input-best-practice 项目）：开推理时 max_tokens 必须 ≥32768，
  否则 reasoning 会吃光预算返回空 content。
- Files API：purpose=user_data，file_id 引用单图最大 64MiB，绕开 48MiB 请求体限制。
"""
import json
import os
import random
import re
import sys
import time
from pathlib import Path

from openai import OpenAI

BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
MODEL = os.environ.get("DEEPSEEK_VISION_MODEL", "deepseek-v4-flash-vision-exp")

THINKING_OFF = {"thinking": {"type": "disabled"}}


def _load_api_key() -> str:
    """密钥来源（按优先级）：环境变量 DEEPSEEK_API_KEY > ~/.deepseek_api_key 首行。
    本 skill 会公开分发，密钥绝不能硬编码在代码里。"""
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not key:
        kf = Path.home() / ".deepseek_api_key"
        if kf.is_file():
            key = kf.read_text(encoding="utf-8").strip().splitlines()[0].strip()
    if not key:
        sys.exit("[error] 未找到 DeepSeek API key，二选一：\n"
                 "  1) 设置环境变量 DEEPSEEK_API_KEY\n"
                 "  2) 把 key 写入 ~/.deepseek_api_key 文件首行（仅存在于本机，不随 skill 分发）")
    return key


class ChatError(RuntimeError):
    pass


def parse_json_lenient(text):
    """解析模型返回的 JSON，容忍代码围栏、None、尾逗号等常见毛病。"""
    if not text or not text.strip():
        raise ChatError("no content to parse")
    s = text.strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    m = re.search(r"```(?:json)?\s*(.+?)\s*```", s, re.DOTALL)
    if m:
        s = m.group(1)
    s = s.strip()
    # 截掉围栏外多余文字：取第一个 { 或 [ 到最后一个 } 或 ]
    starts = [i for i in (s.find("{"), s.find("[")) if i != -1]
    if starts:
        s = s[min(starts):]
        for closer in (s.rfind("}"), s.rfind("]")):
            if closer != -1:
                s = s[: closer + 1]
                break
    s = re.sub(r"\bNone\b", "null", s)
    s = re.sub(r",\s*([\]}])", r"\1", s)
    try:
        return json.loads(s)
    except json.JSONDecodeError as e:
        raise ChatError(f"unparseable JSON: {e}; head={s[:120]!r}")


class DSClient:
    def __init__(self, api_key=None, base_url=None, model=None):
        self.client = OpenAI(
            api_key=api_key or _load_api_key(),
            base_url=base_url or BASE_URL,
            max_retries=0,  # 重试与退避由本类统一控制
        )
        self.model = model or MODEL

    def chat(self, blocks, system=None, thinking=False, json_mode=False,
             max_tokens=8192, temperature=0.1, retries=4, timeout=600):
        """调用 vision 模型，返回 (text, finish_reason)。自动重试空 content 与网络错误。"""
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": blocks})
        kwargs = dict(model=self.model, messages=messages, max_tokens=max_tokens, timeout=timeout)
        if not thinking:
            kwargs["temperature"] = temperature
            kwargs["extra_body"] = THINKING_OFF
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        delay = 2.0
        last = None
        for attempt in range(1, retries + 1):
            try:
                resp = self.client.chat.completions.create(**kwargs)
                text = (resp.choices[0].message.content or "").strip()
                if not text:
                    last = ChatError(f"empty content (attempt {attempt})")
                else:
                    return text, (resp.choices[0].finish_reason or "stop")
            except Exception as e:  # 网络 / 429 / 5xx / 超时
                last = e
            if attempt < retries:
                time.sleep(delay + random.random())
                delay = min(delay * 2, 30)
        raise ChatError(f"chat failed after {retries} attempts: {last}")

    def chat_json(self, blocks, **kw):
        text, finish = self.chat(blocks, json_mode=True, **kw)
        return parse_json_lenient(text), finish

    def upload_image(self, path):
        """上传图片到 Files API，返回 file_id。带指数退避重试。"""
        delay = 2.0
        last = None
        for attempt in range(1, 5):
            try:
                with open(path, "rb") as f:
                    return self.client.files.create(file=f, purpose="user_data").id
            except Exception as e:
                last = e
            if attempt < 4:
                time.sleep(delay + random.random())
                delay = min(delay * 2, 30)
        raise ChatError(f"upload failed for {path}: {last}")

    def file_exists(self, file_id):
        try:
            self.client.files.retrieve(file_id)
            return True
        except Exception:
            return False
