"""Minimal server-side HTML rendering shared by all app dashboards."""

from __future__ import annotations

from html import escape
from typing import Any

CSS = """
body{font-family:system-ui,-apple-system,sans-serif;margin:0;background:#f6f7f9;color:#1a202c}
header{background:#0f3d2e;color:#fff;padding:14px 28px;display:flex;gap:24px;align-items:center}
header a{color:#c6f6d5;text-decoration:none;font-size:14px}
header h1{font-size:17px;margin:0}
main{padding:28px;max-width:1200px;margin:0 auto}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:16px}
.card{background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:18px;text-decoration:none;color:inherit;display:block}
.card:hover{border-color:#0f3d2e}
.card h2{margin:0 0 6px;font-size:16px}
.card p{margin:0;color:#4a5568;font-size:13.5px}
table{border-collapse:collapse;width:100%;background:#fff;font-size:13.5px}
th,td{border:1px solid #e2e8f0;padding:7px 10px;text-align:left}
th{background:#edf2f7;font-weight:600}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
.sev-high{background:#fed7d7}
.sev-med{background:#feebc8}
.sev-low{background:#fefcbf}
.ok{color:#276749;font-weight:600}
.breach{color:#c53030;font-weight:600}
.badge{display:inline-block;padding:2px 8px;border-radius:10px;font-size:12px;background:#e2e8f0}
h2{margin-top:32px}
form.inline{display:inline}
.kpis{display:flex;gap:14px;flex-wrap:wrap;margin:14px 0}
.kpi{background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:12px 18px;min-width:150px}
.kpi .v{font-size:22px;font-weight:700}
.kpi .l{font-size:12px;color:#4a5568}
button,input[type=submit]{background:#0f3d2e;color:#fff;border:0;border-radius:6px;padding:8px 14px;cursor:pointer}
input,select{padding:7px;border:1px solid #cbd5e0;border-radius:6px}
"""

NAV = '<a href="/">Home</a>'


def page(title: str, body: str) -> str:
    return (
        f"<!doctype html><html><head><meta charset='utf-8'><title>{escape(title)}</title>"
        f"<style>{CSS}</style></head><body><header><h1>Invoice Audit Engine</h1>{NAV}</header>"
        f"<main><h1>{escape(title)}</h1>{body}</main></body></html>"
    )


def kpi(label: str, value: str) -> str:
    return (
        f'<div class="kpi"><div class="v">{escape(value)}</div>'
        f'<div class="l">{escape(label)}</div></div>'
    )


def table(headers: list[str], rows: list[list[Any]], row_classes: list[str] | None = None) -> str:
    head = "".join(f"<th>{escape(h)}</th>" for h in headers)
    body_rows = []
    for i, row in enumerate(rows):
        cls = f' class="{row_classes[i]}"' if row_classes and row_classes[i] else ""
        cells = "".join(
            f'<td class="num">{escape(f"{c:,.2f}")}</td>'
            if isinstance(c, int | float) and not isinstance(c, bool)
            else f"<td>{escape(str(c))}</td>"
            for c in row
        )
        body_rows.append(f"<tr{cls}>{cells}</tr>")
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body_rows)}</tbody></table>"
