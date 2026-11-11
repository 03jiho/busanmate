"""BaseTool 인터페이스 (SDD §3.2, NFR-009). 도구는 @register로 레지스트리에 올리고, 에이전트는 이름으로 찾는다."""
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from settings import config

log = logging.getLogger("tools")
_pool = ThreadPoolExecutor(max_workers=8)


@dataclass
class ToolResult:
    ok: bool
    data: Any = None                                     # LLM에 돌려줄 작은 요약
    sources: list[dict] = field(default_factory=list)    # {"label", "url", "license"}
    notices: list[str] = field(default_factory=list)     # 사용자 안내
    stale: bool = False                                  # DB·추정 등 대체 데이터로 응답했는지
    updates: dict = field(default_factory=dict)          # 성공 시 에이전트 상태에 반영할 값 (후보, 코스 등)


class BaseTool:
    name = ""
    description = ""
    args_schema: dict = {"type": "object", "properties": {}}
    timeout_s: float | None = None                       # None이면 config tools.timeout_s

    def run(self, args: dict, ctx: dict) -> ToolResult:
        raise NotImplementedError

    def fallback(self, args: dict, ctx: dict, err: Exception) -> ToolResult:
        return ToolResult(ok=False, data={"error": f"{self.name} 실행 실패"}, stale=True)

    def call(self, args: dict, ctx: dict) -> ToolResult:
        """run을 시간 제한 안에 실행하고, 예외·시간 초과면 fallback (Template Method). ctx는 읽기만 한다."""
        try:
            return _pool.submit(self.run, args, ctx).result(timeout=self.timeout_s or config()["tools"]["timeout_s"])
        except Exception as e:
            log.warning("tool %s 실패: %s", self.name, type(e).__name__)
            return self.fallback(args, ctx, e)


REGISTRY: dict[str, BaseTool] = {}


def register(cls):
    REGISTRY[cls.name] = cls()
    return cls


def declarations() -> list[dict]:
    return [{"name": t.name, "description": t.description, "parameters": t.args_schema} for t in REGISTRY.values()]


def steps_schema() -> dict:
    """도구 선택 구조화 출력: [{"name": 도구 이름, ...그 도구의 인자}] (실행 순서). 인자 칸은 등록된 도구의 args_schema를 합친다."""
    args = {k: v for t in REGISTRY.values() for k, v in t.args_schema.get("properties", {}).items()}
    return {"type": "array", "items": {"type": "object", "required": ["name"],
                                       "properties": {"name": {"type": "string", "enum": list(REGISTRY)}, **args}}}
