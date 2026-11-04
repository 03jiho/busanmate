"""docs/*.docx -> docs/*.md 사본 (그림은 docs/img/<문서명>/). 실행: python docs/docx2md.py"""
import re
from pathlib import Path

from docx import Document
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph

DOCS = Path(__file__).parent


def heading_level(p):
    name = p.style.name if p.style is not None else ""
    m = re.search(r"(?:Heading|제목)\s*(\d)", name)
    if m:
        return int(m.group(1))
    return 1 if name in ("Title", "제목") else 0


def cell_text(cell):
    return "<br>".join(p.text.strip() for p in cell.paragraphs if p.text.strip()).replace("|", "\\|")


def table_md(tbl):
    rows = []
    for row in tbl.rows:
        seen, cells = set(), []
        for c in row.cells:  # 가로 병합 셀은 같은 _tc가 반복된다
            if id(c._tc) not in seen:
                seen.add(id(c._tc))
                cells.append(cell_text(c))
        rows.append(cells)
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    lines = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * width]
    lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(lines)


def images_md(p, doc, img_dir):
    out = []
    for blip in p._p.iter(qn("a:blip")):
        part = doc.part.related_parts[blip.get(qn("r:embed"))]
        name = Path(part.partname).name
        img_dir.mkdir(parents=True, exist_ok=True)
        (img_dir / name).write_bytes(part.blob)
        out.append(f"![{name}](img/{img_dir.name}/{name})")
    return out


def convert(src):
    doc = Document(src)
    img_dir = DOCS / "img" / src.stem
    out = [f"<!-- {src.name}에서 자동 생성 (docs/docx2md.py). 직접 수정하지 말 것 -->"]
    for el in doc.element.body.iterchildren():
        if el.tag == qn("w:tbl"):
            out.append(table_md(Table(el, doc)))
        elif el.tag == qn("w:p"):
            p = Paragraph(el, doc)
            out += images_md(p, doc, img_dir)
            text = p.text.strip()
            if text:
                lvl = heading_level(p)
                out.append(("#" * lvl + " " if lvl else "") + text)
    src.with_suffix(".md").write_text("\n\n".join(out) + "\n", encoding="utf-8")
    print(src.with_suffix(".md").name)


if __name__ == "__main__":
    for f in sorted(DOCS.glob("*.docx")):
        if not f.name.startswith("~$"):
            convert(f)
