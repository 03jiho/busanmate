"""설정 로더: 수치는 config.yaml, 비밀값은 .env(환경변수). 비밀값은 출력·로그 금지."""
import os
from functools import lru_cache
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")  # 이미 있는 환경변수(docker env_file)는 덮어쓰지 않는다


@lru_cache
def config() -> dict:
    return yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))


def secret(name: str) -> str:
    """환경변수 값을 돌려준다. 없으면 이름만 담아 오류를 낸다(값은 절대 메시지에 넣지 않음)."""
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"환경변수 {name}이(가) 설정되지 않았습니다 (.env 확인)")
    return value
