#!/usr/bin/env python3
"""
LLM 生成层

通过 OpenAI 兼容的 /chat/completions 接口调用大模型，用于将 Router 组装的
系统提示词转化为最终回复。当前默认接 Kimi K3（``https://api.kimi.com/coding/v1``）。

换供应商只需改 .env 里的 LLM_BASE_URL / LLM_MODEL / LLM_API_KEY，代码不用动。
"""

from typing import Any, Optional
import os
import httpx
import json
from collections.abc import Generator


class LLMProvider:
    """LLM 生成基类"""

    def generate(self, system_prompt: str, user_message: str, temperature: float = 0.7, stream: bool = False) -> str:
        raise NotImplementedError


class OpenAICompatProvider(LLMProvider):
    """OpenAI 兼容接口的通用 Provider（默认 Kimi K3）"""

    DEFAULT_BASE_URL = "https://api.kimi.com/coding/v1/chat/completions"
    DEFAULT_MODEL = "k3"

    # K3 是强制推理模型，**只接受 temperature=1**，传 0.7 会被接口直接拒绝
    # （invalid temperature: only 1 is allowed for this model）。
    # 想换回可调温度的模型时用 .env 的 LLM_TEMPERATURE 覆盖即可。
    DEFAULT_TEMPERATURE = 1.0

    # 推理模型的思考过程也计入 completion tokens，额度要比非推理模型给得宽，
    # 否则正文容易被截断（finish_reason=length）。
    DEFAULT_MAX_TOKENS = 8192

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        temperature: float | None = None,
    ):
        # 支持 LLM_API_KEY 或 ANTHROPIC_API_KEY
        self.api_key = api_key or os.getenv("LLM_API_KEY", "") or os.getenv("ANTHROPIC_API_KEY", "")
        self.base_url: str = base_url or os.getenv("LLM_BASE_URL") or self.DEFAULT_BASE_URL
        self.model = model or os.getenv("LLM_MODEL", self.DEFAULT_MODEL)

        if temperature is not None:
            self.temperature = temperature
        else:
            env_temp = os.getenv("LLM_TEMPERATURE", "").strip()
            self.temperature = float(env_temp) if env_temp else self.DEFAULT_TEMPERATURE

        env_max = os.getenv("LLM_MAX_TOKENS", "").strip()
        self.max_tokens = int(env_max) if env_max else self.DEFAULT_MAX_TOKENS

        if not self.api_key:
            raise ValueError("LLM_API_KEY not set. Please configure LLM_API_KEY in .env")

    def _build_payload(
        self, system_prompt: str, user_message: str, temperature: float | None, stream: bool
    ) -> dict[str, Any]:
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": user_message})

        return {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature if temperature is None else temperature,
            "stream": stream,
            "max_tokens": self.max_tokens,
        }

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def generate(
        self, system_prompt: str, user_message: str, temperature: float | None = None, stream: bool = False
    ) -> str:
        """同步生成。temperature 传 None 表示用 .env / 模型默认值。"""
        payload = self._build_payload(system_prompt, user_message, temperature, stream)

        try:
            resp = httpx.post(
                self.base_url,
                headers=self._headers,
                json=payload,
                timeout=180.0,
            )
            resp.raise_for_status()
            data = resp.json()

            if "choices" in data and data["choices"]:
                # 推理模型把思考过程放在 reasoning_content，content 已是干净正文
                return data["choices"][0]["message"].get("content") or ""
            return f"[LLM API 返回格式异常] {str(data)[:200]}"
        except httpx.HTTPStatusError as e:
            # 把响应体带出来，否则 400 只能看到一个状态码，排查全靠猜
            return f"[LLM API 请求失败] {e} | {e.response.text[:300]}"
        except httpx.HTTPError as e:
            return f"[LLM API 请求失败] {e}"
        except Exception as e:
            return f"[LLM 生成异常] {e}"

    def generate_stream(
        self, system_prompt: str, user_message: str, temperature: float | None = None
    ) -> Generator[str, None, None]:
        """流式生成（只吐 content，不吐 reasoning_content）"""
        payload = self._build_payload(system_prompt, user_message, temperature, stream=True)

        try:
            with httpx.stream(
                "POST",
                self.base_url,
                headers=self._headers,
                json=payload,
                timeout=180.0,
            ) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    if line.startswith("data: "):
                        json_str = line[6:]
                        if json_str.strip() == "[DONE]":
                            break
                        try:
                            data = json.loads(json_str)
                            if "choices" in data and data["choices"]:
                                delta = data["choices"][0].get("delta", {})
                                content = delta.get("content", "")
                                if content:
                                    yield content
                        except json.JSONDecodeError:
                            pass
        except Exception as e:
            yield f"[LLM 流式生成异常] {e}"


# 旧名保留：docs/archive/corpus/dual_axis_review.py 等历史脚本仍按这个名字导入
MiniMaxProvider = OpenAICompatProvider
