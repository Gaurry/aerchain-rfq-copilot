"""Turn any vendor file into content the model can read.

PDFs and images go to Claude natively (it sees the page, footnotes,
handwriting). Excel, Word and email are converted to text that keeps a
location tag on every line, so evidence can point back to a sheet/row or
paragraph."""
from __future__ import annotations

import base64
import email
from email import policy
from pathlib import Path

MEDIA = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}


def _xlsx_text(path: Path) -> str:
    from openpyxl import load_workbook
    wb = load_workbook(path, data_only=True)
    out = []
    for ws in wb.worksheets:
        out.append(f"=== Sheet: {ws.title} ===")
        for row in ws.iter_rows():
            cells = [f"{c.column_letter}={c.value}" for c in row if c.value not in (None, "")]
            if cells:
                out.append(f"[{ws.title}!R{row[0].row}] " + " | ".join(cells))
    return "\n".join(out)


def _docx_text(path: Path) -> str:
    import docx
    d = docx.Document(path)
    out = [f"[¶{i}] {p.text}" for i, p in enumerate(d.paragraphs, 1) if p.text.strip()]
    for t_i, t in enumerate(d.tables, 1):
        for r_i, row in enumerate(t.rows, 1):
            out.append(f"[table{t_i} R{r_i}] " + " | ".join(c.text for c in row.cells))
    return "\n".join(out)


def _eml_text(path: Path) -> str:
    msg = email.message_from_bytes(path.read_bytes(), policy=policy.default)
    body = msg.get_body(preferencelist=("plain", "html")) or msg
    raw = body.get_payload(decode=True) or b""
    charset = body.get_content_charset() or "utf-8"   # phones often omit it; UTF-8 is the safe guess
    try:
        text = raw.decode(charset)
    except (LookupError, UnicodeDecodeError):
        text = raw.decode("utf-8", errors="replace")
    head = "\n".join(f"{h}: {msg[h]}" for h in ("From", "To", "Date", "Subject") if msg[h])
    return f"{head}\n\n{text}"


def to_content_blocks(path: Path) -> list[dict]:
    """Anthropic message content blocks for one file, labelled with its name."""
    ext = path.suffix.lower()
    label = {"type": "text", "text": f"--- FILE: {path.name} ---"}
    if ext == ".pdf":
        data = base64.standard_b64encode(path.read_bytes()).decode()
        return [label, {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": data}}]
    if ext in MEDIA:
        data = base64.standard_b64encode(path.read_bytes()).decode()
        return [label, {"type": "image", "source": {"type": "base64", "media_type": MEDIA[ext], "data": data}}]
    if ext in (".xlsx", ".xlsm"):
        text = _xlsx_text(path)
    elif ext == ".docx":
        text = _docx_text(path)
    elif ext == ".eml":
        text = _eml_text(path)
    elif ext == ".csv":
        text = path.read_text(errors="replace")
    else:
        text = path.read_text(errors="replace")
    return [label, {"type": "text", "text": text}]
