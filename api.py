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
app = FastAPI(title="LabSuite API", docs_url=None, redoc_url=None)
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


# ── PROMPTS POR TIPO DE RELATÓRIO ────────────────────────────────────────────

REPORT_PROMPTS = {
    "sales": {
        "role": "elite sales analyst producing internal management reports",
        "tone": "direct, data-driven, and commercially sharp. No fluff.",
        "schema": """{
  "report_type": "sales",
  "period": "<period label>",
  "executive_summary": "<3-5 sentence sharp executive summary>",
  "kpis": [
    {"label": "<KPI name>", "value": "<formatted value>", "change": "<% or absolute change>", "trend": "up|down|neutral"}
  ],
  "top_products": [{"rank": 1, "name": "<name>", "value": "<revenue>", "note": "<insight>"}],
  "top_clients": [{"rank": 1, "name": "<name>", "value": "<revenue>", "note": "<insight>"}],
  "top_salespeople": [{"rank": 1, "name": "<name>", "value": "<revenue>", "note": "<insight>"}],
  "alerts": [{"level": "critical|warning|positive", "message": "<concise alert>"}],
  "forecast": {
    "next_period": "<label>",
    "revenue_estimate": "<value with currency>",
    "confidence": "high|medium|low",
    "rationale": "<2-3 sentences>",
    "actions": ["<action 1>", "<action 2>"]
  },
  "comparison_narrative": "<2-3 sentences comparing periods>"
}"""
    },

    "efficiency": {
        "role": "operations efficiency analyst specialising in workforce and process optimisation",
        "tone": "analytical, precise, focused on output-per-cost ratios. No fluff.",
        "schema": """{
  "report_type": "efficiency",
  "period": "<period label>",
  "executive_summary": "<3-5 sentence summary of operational efficiency>",
  "kpis": [
    {"label": "<KPI name>", "value": "<formatted value>", "change": "<% or absolute change>", "trend": "up|down|neutral"}
  ],
  "team_performance": [
    {
      "unit": "<team / employee / location name>",
      "output": "<what they produce — revenue, tasks, rooms, units>",
      "cost": "<labour cost or hours>",
      "efficiency_ratio": "<output per cost unit, e.g. €42 revenue / hour>",
      "vs_average": "<above/below/at average — how much>",
      "note": "<insight or recommendation>"
    }
  ],
  "bottlenecks": [
    {"area": "<process or team>", "issue": "<what is causing inefficiency>", "impact": "<estimated cost or time lost>", "recommendation": "<what to do>"}
  ],
  "alerts": [{"level": "critical|warning|positive", "message": "<concise alert>"}],
  "optimisation_actions": [
    {"priority": "high|medium|low", "action": "<specific action>", "expected_gain": "<estimated saving or improvement>"}
  ],
  "summary_narrative": "<2-3 sentences on overall efficiency health>"
}"""
    },

    "cost": {
        "role": "cost reduction analyst specialising in identifying unnecessary expenditure and optimisation opportunities",
        "tone": "direct, commercially ruthless, focused on margin protection. No fluff.",
        "schema": """{
  "report_type": "cost",
  "period": "<period label>",
  "executive_summary": "<3-5 sentence summary of cost health>",
  "total_costs": "<total expenditure in the period>",
  "cost_breakdown": [
    {"category": "<cost category>", "amount": "<value>", "pct_of_total": "<% of total>", "vs_previous": "<change>", "status": "ok|review|critical"}
  ],
  "unnecessary_costs": [
    {
      "item": "<specific cost item>",
      "current_spend": "<amount>",
      "benchmark": "<what it should be or competitor/industry reference>",
      "excess": "<how much is being wasted>",
      "action": "<exactly what to do to cut this>"
    }
  ],
  "savings_opportunities": [
    {"opportunity": "<description>", "estimated_saving": "<amount or %>", "effort": "low|medium|high", "timeframe": "<when savings are felt>"}
  ],
  "alerts": [{"level": "critical|warning|positive", "message": "<concise alert>"}],
  "total_recoverable": "<total estimated savings if all opportunities acted on>",
  "priority_actions": ["<action 1>", "<action 2>", "<action 3>"]
}"""
    },

    "financial": {
        "role": "CFO-level financial analyst producing a P&L-style management overview",
        "tone": "authoritative, precise, structured for board-level consumption. No fluff.",
        "schema": """{
  "report_type": "financial",
  "period": "<period label>",
  "executive_summary": "<3-5 sentence financial health summary>",
  "income_statement": {
    "revenue": "<total revenue>",
    "cogs": "<cost of goods/services sold>",
    "gross_profit": "<revenue minus COGS>",
    "gross_margin_pct": "<gross margin %>",
    "operating_expenses": "<total opex>",
    "ebitda": "<earnings before interest, tax, depreciation, amortisation>",
    "net_result": "<net profit or loss>"
  },
  "kpis": [
    {"label": "<KPI>", "value": "<value>", "change": "<vs previous>", "trend": "up|down|neutral"}
  ],
  "cash_flow_summary": "<narrative on cash position and movement>",
  "cost_structure": [
    {"category": "<category>", "amount": "<value>", "pct_of_revenue": "<%>"}
  ],
  "alerts": [{"level": "critical|warning|positive", "message": "<alert>"}],
  "forecast": {
    "next_period": "<label>",
    "revenue_estimate": "<value>",
    "ebitda_estimate": "<value>",
    "confidence": "high|medium|low",
    "rationale": "<2-3 sentences>",
    "actions": ["<action 1>", "<action 2>"]
  },
  "financial_narrative": "<3-4 sentences on overall financial position and trajectory>"
}"""
    }
}


# ── HEALTH ────────────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    return {"status": "ok", "service": "LabSuite API"}


# ── ANALYSE REPORT ────────────────────────────────────────────────────────────
@app.post("/analyze-sales")
@limiter.limit("10/hour")
async def analyze_sales(
    request: Request,
    input_mode: str = Form("text"),
    report_type: str = Form("sales"),          # NEW: sales | efficiency | cost | financial
    raw_data: str = Form(""),
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
    company_name: str = Form(""),
    currency: str = Form("EUR"),
    file: UploadFile | None = File(default=None),
    x_access_code: str | None = Header(default=None),
):
    verify_access(x_access_code)

    company_name = sanitize(company_name, 100)
    currency     = sanitize(currency, 10) or "EUR"
    report_type  = report_type if report_type in REPORT_PROMPTS else "sales"

    pdf_content = None

    if input_mode == "file" and file:
        content = await file.read()
        if len(content) > MAX_FILE_SIZE:
            raise HTTPException(status_code=400, detail="File too large (max 5MB)")
        ext = os.path.splitext(file.filename or "")[-1].lower()
        if ext not in (".csv", ".txt", ".xlsx", ".xls", ".pdf"):
            raise HTTPException(status_code=400, detail="Only CSV, TXT, Excel or PDF files allowed")

        if ext == ".pdf":
            pdf_content = content
            data_block = ""
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
        if period_current:   parts.append(f"Current period: {period_current}")
        if period_previous:  parts.append(f"Previous period: {period_previous}")
        if revenue_current:  parts.append(f"Revenue (current): {revenue_current} {currency}")
        if revenue_previous: parts.append(f"Revenue (previous): {revenue_previous} {currency}")
        if units_current:    parts.append(f"Units sold (current): {units_current}")
        if units_previous:   parts.append(f"Units sold (previous): {units_previous}")
        if top_products:     parts.append(f"Top products/services: {top_products}")
        if top_clients:      parts.append(f"Top clients: {top_clients}")
        if top_salespeople:  parts.append(f"Top salespeople: {top_salespeople}")
        if extra_notes:      parts.append(f"Additional notes: {extra_notes}")
        data_block = "\n".join(parts)
        if not data_block.strip():
            raise HTTPException(status_code=400, detail="No form data provided")

    cfg = REPORT_PROMPTS[report_type]

    system_prompt = f"""You are an {cfg['role']}.
Your tone is {cfg['tone']}
Currency: {currency}. Company: {company_name or "the company"}.

Respond ONLY with a valid JSON object — no markdown, no preamble, no explanation.

Required schema:
{cfg['schema']}

If certain data is not provided, return an empty array [] or null for that field.
Always populate executive_summary, kpis, and alerts — infer or estimate where needed, clearly flagging any assumptions.
For efficiency and cost reports: be specific and actionable. Name exact areas, give concrete numbers, do not be vague.
"""

    if pdf_content:
        encoded = base64.standard_b64encode(pdf_content).decode("utf-8")
        message_content = [
            {
                "type": "document",
                "source": {"type": "base64", "media_type": "application/pdf", "data": encoded},
            },
            {"type": "text", "text": "Here is the data to analyse. Extract all relevant information and produce the report."}
        ]
    else:
        message_content = f"Here is the data to analyse:\n\n{data_block}"

    try:
        response = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=3000,
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


# ── CHAT (contextual — knows the report) ─────────────────────────────────────
@app.post("/chat")
@limiter.limit("30/hour")
async def chat(
    request: Request,
    x_access_code: str | None = Header(default=None),
):
    verify_access(x_access_code)

    body = await request.json()
    messages = body.get("messages", [])
    report_context = body.get("report_context", None)  # the full report JSON
    company_name   = body.get("company_name", "the company")
    currency       = body.get("currency", "EUR")

    if not messages:
        raise HTTPException(status_code=400, detail="No messages provided")

    messages = messages[-20:]

    report_block = ""
    if report_context:
        report_block = f"""
The user has just generated the following business report. Use it as your primary source of truth.
Do not invent data — all your insights, calculations, and recommendations must be grounded in this report.

REPORT DATA:
{json.dumps(report_context, indent=2, ensure_ascii=False)}
"""

    system_prompt = f"""You are an expert financial and operational advisor embedded inside LabSuite, a business intelligence platform.
You are speaking directly with a business owner or manager.

Your role:
- Analyse the report they've just generated and answer their questions
- Identify specific savings, inefficiencies, and risks using the numbers in the report
- Give concrete, actionable financial advice: "if you cut X, you save Y over Z months"
- Calculate projections when asked (e.g. "if I fix this over 6 months, what do I save?")
- Be direct, precise, and commercially sharp — like a CFO who charges €500/hour
- Never be vague. Always reference specific numbers from the report.
- Keep responses concise but substantive — no filler, no fluff.

Company: {company_name}
Currency: {currency}
{report_block}

If no report has been provided, ask the user to generate a report first using the SalesLab tools.
Respond in the same language the user writes in.
"""

    try:
        response = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=1500,
            system=system_prompt,
            messages=messages,
        )
    except Exception:
        raise HTTPException(status_code=500, detail="Chat service unavailable")

    reply = "".join(b.text for b in response.content if hasattr(b, "text")).strip()

    return JSONResponse({"message": reply})