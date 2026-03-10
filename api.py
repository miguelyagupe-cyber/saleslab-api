import os
import re
import json
import base64
import io
from fastapi import FastAPI, File, UploadFile, Form, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
import anthropic

# ── ENV ──────────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
ACCESS_CODE       = os.environ.get("ACCESS_CODE")
ALLOWED_ORIGIN    = os.environ.get("ALLOWED_ORIGIN", "https://saleslab.vercel.app")

if not ANTHROPIC_API_KEY:
    raise RuntimeError("ANTHROPIC_API_KEY not set")
if not ACCESS_CODE:
    raise RuntimeError("ACCESS_CODE not set")

# ── APP ──────────────────────────────────────────────────────────────────────
limiter = Limiter(key_func=get_remote_address)
app = FastAPI(title="SalesLab API", docs_url=None, redoc_url=None)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[ALLOWED_ORIGIN],
    allow_credentials=True,
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
)

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

MAX_FILE_SIZE = 5 * 1024 * 1024

def verify_access(code: str | None):
    if not code or code.strip() != ACCESS_CODE:
        raise HTTPException(status_code=401, detail="Unauthorized")

def sanitize(text: str, max_len: int) -> str:
    return (text or "").strip()[:max_len]

@app.get("/health")
def health():
    return {"status": "ok", "service": "SalesLab API"}


@app.post("/analyze-sales")
@limiter.limit("10/hour")
async def analyze_sales(
    request: Request,
    # Input mode: "file" | "text" | "form"
    input_mode: str = Form("text"),
    # Free text / CSV pasted
    raw_data: str = Form(""),
    # Manual form fields
    period_current: str = Form(""),
    period_previous: str = Form(""),
    revenue_current: str = Form(""),
    revenue_previous: str = Form(""),
    units_current: str = Form(""),
    units_previous: str = Form(""),
    top_products: str = Form(""),
    top_clients: str = Form(""),
    top_salespeople: str = Form(""),
    extra_notes: str = Form(""),
    # Company context
    company_name: str = Form(""),
    currency: str = Form("EUR"),
    # File upload
    file: UploadFile | None = File(default=None),
    x_access_code: str | None = Header(default=None),
):
    verify_access(x_access_code)

    company_name = sanitize(company_name, 100)
    currency     = sanitize(currency, 10) or "EUR"

    # Build data block depending on input mode
    pdf_content = None  # will hold raw bytes if PDF

    if input_mode == "file" and file:
        content = await file.read()
        if len(content) > MAX_FILE_SIZE:
            raise HTTPException(status_code=400, detail="File too large (max 5MB)")
        ext = os.path.splitext(file.filename or "")[-1].lower()
        if ext not in (".csv", ".txt", ".xlsx", ".xls", ".pdf"):
            raise HTTPException(status_code=400, detail="Only CSV, TXT, Excel or PDF files allowed")

        if ext == ".pdf":
            # Pass PDF directly to Claude as a document — handles tables natively
            pdf_content = content
            data_block = ""  # not used for PDF path
        elif ext in (".xlsx", ".xls"):
            try:
                import openpyxl
                wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True)
                ws = wb.active
                rows = []
                for row in ws.iter_rows(values_only=True):
                    rows.append("\t".join(str(c) if c is not None else "" for c in row))
                data_block = "\n".join(rows[:200])
            except Exception:
                raise HTTPException(status_code=400, detail="Could not parse Excel file")
        else:
            data_block = content.decode("utf-8", errors="replace")[:12000]

    elif input_mode == "text":
        data_block = sanitize(raw_data, 12000)
        if not data_block:
            raise HTTPException(status_code=400, detail="No data provided")

    else:  # form
        parts = []
        if period_current:  parts.append(f"Current period: {period_current}")
        if period_previous: parts.append(f"Previous period: {period_previous}")
        if revenue_current: parts.append(f"Revenue (current): {revenue_current} {currency}")
        if revenue_previous:parts.append(f"Revenue (previous): {revenue_previous} {currency}")
        if units_current:   parts.append(f"Units sold (current): {units_current}")
        if units_previous:  parts.append(f"Units sold (previous): {units_previous}")
        if top_products:    parts.append(f"Top products/services: {top_products}")
        if top_clients:     parts.append(f"Top clients: {top_clients}")
        if top_salespeople: parts.append(f"Top salespeople: {top_salespeople}")
        if extra_notes:     parts.append(f"Additional notes: {extra_notes}")
        data_block = "\n".join(parts)
        if not data_block.strip():
            raise HTTPException(status_code=400, detail="No form data provided")

    system_prompt = f"""You are an elite sales analyst producing internal management reports.
Your tone is direct, data-driven, and commercially sharp. No fluff.
Currency: {currency}. Company: {company_name or "the company"}.

Respond ONLY with a valid JSON object — no markdown, no preamble.

Required structure:
{{
  "period": "<period label, e.g. March 2026 vs February 2026>",
  "executive_summary": "<3-5 sentence sharp executive summary of performance>",
  "kpis": [
    {{"label": "<KPI name>", "value": "<formatted value>", "change": "<% or absolute change vs previous>", "trend": "up|down|neutral"}}
  ],
  "top_products": [{{"rank": 1, "name": "<name>", "value": "<revenue or units>", "note": "<optional insight>"}}],
  "top_clients": [{{"rank": 1, "name": "<name>", "value": "<revenue>", "note": "<optional insight>"}}],
  "top_salespeople": [{{"rank": 1, "name": "<name>", "value": "<revenue or deals>", "note": "<optional insight>"}}],
  "alerts": [
    {{"level": "critical|warning|positive", "message": "<concise alert or highlight>"}}
  ],
  "forecast": {{
    "next_period": "<period label>",
    "revenue_estimate": "<value with currency>",
    "confidence": "high|medium|low",
    "rationale": "<2-3 sentences explaining the forecast>",
    "actions": ["<recommended action 1>", "<recommended action 2>"]
  }},
  "comparison_narrative": "<2-3 sentences comparing current vs previous period, highlighting what changed and why>"
}}

If certain data (e.g. top_clients) is not provided, return an empty array [] for that field.
Always populate kpis, executive_summary, alerts, and forecast — infer or estimate where needed, flagging assumptions.
"""

    # Build message content — PDF gets sent as base64 document, everything else as text
    if pdf_content:
        encoded = base64.standard_b64encode(pdf_content).decode("utf-8")
        message_content = [
            {
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": encoded,
                },
            },
            {"type": "text", "text": "Here is the sales data to analyse. Extract all relevant data from this document and produce the report."}
        ]
    else:
        message_content = f"Here is the sales data to analyse:\n\n{data_block}"

    try:
        response = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=2500,
            system=system_prompt,
            messages=[{"role": "user", "content": message_content}],
        )
    except Exception:
        raise HTTPException(status_code=500, detail="Analysis service unavailable")

    raw = "".join(b.text for b in response.content if hasattr(b, "text")).strip()
    raw = re.sub(r"^```(?:json)?", "", raw).strip()
    raw = re.sub(r"```$", "", raw).strip()

    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="Failed to parse analysis result")

    return JSONResponse(content=result)