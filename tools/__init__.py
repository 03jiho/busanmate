"""도구 어댑터(COMP-07): BaseTool·ToolResult와 Weather/Festival/MapTool.
이 패키지의 모듈을 모두 불러와 @register된 도구를 레지스트리에 올린다. 새 도구는 파일만 추가하면 된다(NFR-009)."""
import importlib
import pkgutil

for _m in pkgutil.iter_modules(__path__):
    importlib.import_module(f"{__name__}.{_m.name}")
