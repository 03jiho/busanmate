"""FestivalTool (FR-010): 방문 기간과 겹치는 부산 축제·행사. 기간 밖 행사는 돌려주지 않는다."""
from psycopg.rows import dict_row

import db
from settings import config
from tools.base import BaseTool, ToolResult, register


@register
class GetFestivals(BaseTool):
    name = "get_festivals"
    description = "방문 기간에 열리는 부산 축제·행사를 조회한다."
    timeout_s = 5

    def run(self, args, ctx):
        dates = ctx.get("dates") or []
        if not dates:
            return ToolResult(ok=False, data={"error": "방문 날짜가 없습니다."})
        with db.connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT festival_id, name, start_date, end_date, address, url, license FROM festival "
                        "WHERE start_date <= %s AND end_date >= %s ORDER BY start_date LIMIT 5",
                        (max(dates), min(dates)))
            rows = cur.fetchall()
        label = config()["sources"]["busan_festival"]["label"]
        return ToolResult(ok=True, data={"festivals": [{"name": f["name"], "start": str(f["start_date"]),
                                                        "end": str(f["end_date"])} for f in rows]},
                          sources=[{"label": f"{label} - {f['name']}", "url": f["url"], "license": f["license"],
                                    "portal_url": config()["sources"]["busan_festival"].get("portal_url")}
                                   for f in rows],
                          updates={"festivals": rows})

    def fallback(self, args, ctx, err):
        return ToolResult(ok=False, data={"error": "축제 조회 실패"}, stale=True,
                          notices=["축제 정보를 불러오지 못했습니다."])
