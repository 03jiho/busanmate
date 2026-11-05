"""에이전트 계층: AgentOrchestrator(COMP-03), QueryParser(COMP-04), ResponseBuilder(COMP-09), SessionStore(COMP-10). 프롬프트는 agent/prompts/."""
from pathlib import Path

from settings import config


def prompt(name: str) -> str:
    """agent/prompts/<name>_<버전>.md (버전은 config llm.prompt_version)."""
    return (Path(__file__).parent / "prompts" / f"{name}_{config()['llm']['prompt_version']}.md").read_text(encoding="utf-8")
