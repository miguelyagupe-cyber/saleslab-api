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
from typing import Optional
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
MAX_FILES     = 3
ALLOWED_EXTS  = {".csv", ".txt", ".xlsx", ".xls", ".pdf"}

def verify_access(code: str | None):
    if not code or code.strip() != ACCESS_CODE:
        raise HTTPException(status_code=401, detail="Unauthorized")

def sanitize(text: str, max_len: int) -> str:
    return (text or "").strip()[:max_len]


# ── REPORT PROMPTS ────────────────────────────────────────────────────────────

REPORT_PROMPTS = {
    "sales": {
        "role": "elite sales analyst producing internal management reports",
        "tone": "direct, data-driven, and commercially sharp. No fluff.",
        "required_fields": ["period or date range", "revenue figures", "at least one of: products, clients, or salespeople"],
        "schema": """{
  "report_type": "sales",
  "period": "<period label>",
  "executive_summary": "<3-5 sentence sharp executive summary>",
  "kpis": [{"label": "<KPI name>", "value": "<formatted value>", "change": "<% or absolute change>", "trend": "up|down|neutral"}],
  "top_products": [{"rank": 1, "name": "<n>", "value": "<revenue>", "note": "<insight>"}],
  "top_clients": [{"rank": 1, "name": "<n>", "value": "<revenue>", "note": "<insight>"}],
  "top_salespeople": [{"rank": 1, "name": "<n>", "value": "<revenue>", "note": "<insight>"}],
  "alerts": [{"level": "critical|warning|positive", "message": "<concise alert>"}],
  "forecast": {
    "next_period": "<label>",
    "revenue_estimate": "<value with currency>",
    "confidence": "high|medium|low",
    "rationale": "<2-3 sentences>",
    "actions": ["<action 1>", "<action 2>"]
  },
  "comparison_narrative": "<2-3 sentences comparing periods>",
  "data_quality": "good|partial|insufficient",
  "missing_data": ["<field missing if any>"]
}"""
    },
    "efficiency": {
        "role": "operations efficiency analyst specialising in workforce and process optimisation",
        "tone": "analytical, precise, focused on output-per-cost ratios. No fluff.",
        "required_fields": ["team or employee names", "hours worked or labour cost", "output or productivity metric"],
        "schema": """{
  "report_type": "efficiency",
  "period": "<period label>",
  "executive_summary": "<3-5 sentence summary of operational efficiency>",
  "kpis": [{"label": "<KPI name>", "value": "<formatted value>", "change": "<% or absolute change>", "trend": "up|down|neutral"}],
  "team_performance": [
    {
      "unit": "<team / employee / location name>",
      "output": "<what they produce>",
      "cost": "<labour cost or hours>",
      "efficiency_ratio": "<output per cost unit>",
      "vs_average": "<above/below/at average>",
      "note": "<insight or recommendation>"
    }
  ],
  "bottlenecks": [{"area": "<process or team>", "issue": "<cause>", "impact": "<cost or time lost>", "recommendation": "<action>"}],
  "alerts": [{"level": "critical|warning|positive", "message": "<concise alert>"}],
  "optimisation_actions": [{"priority": "high|medium|low", "action": "<specific action>", "expected_gain": "<saving or improvement>"}],
  "summary_narrative": "<2-3 sentences on overall efficiency health>",
  "data_quality": "good|partial|insufficient",
  "missing_data": ["<field missing if any>"]
}"""
    },
    "cost": {
        "role": "cost reduction analyst specialising in identifying unnecessary expenditure",
        "tone": "direct, commercially ruthless, focused on margin protection. No fluff.",
        "required_fields": ["cost categories with amounts", "time period"],
        "schema": """{
  "report_type": "cost",
  "period": "<period label>",
  "executive_summary": "<3-5 sentence summary of cost health>",
  "total_costs": "<total expenditure>",
  "cost_breakdown": [{"category": "<category>", "amount": "<value>", "pct_of_total": "<%>", "vs_previous": "<change>", "status": "ok|review|critical"}],
  "unnecessary_costs": [{"item": "<cost item>", "current_spend": "<amount>", "benchmark": "<what it should be>", "excess": "<waste amount>", "action": "<what to do>"}],
  "savings_opportunities": [{"opportunity": "<description>", "estimated_saving": "<amount or %>", "effort": "low|medium|high", "timeframe": "<when>"}],
  "alerts": [{"level": "critical|warning|positive", "message": "<concise alert>"}],
  "total_recoverable": "<total estimated savings>",
  "priority_actions": ["<action 1>", "<action 2>", "<action 3>"],
  "data_quality": "good|partial|insufficient",
  "missing_data": ["<field missing if any>"]
}"""
    },
    "financial": {
        "role": "CFO-level financial analyst producing a P&L-style management overview",
        "tone": "authoritative, precise, structured for board-level consumption. No fluff.",
        "required_fields": ["revenue", "costs or expenses", "time period"],
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
    "ebitda": "<EBITDA>",
    "net_result": "<net profit or loss>"
  },
  "kpis": [{"label": "<KPI>", "value": "<value>", "change": "<vs previous>", "trend": "up|down|neutral"}],
  "cash_flow_summary": "<narrative on cash position>",
  "cost_structure": [{"category": "<category>", "amount": "<value>", "pct_of_revenue": "<%>"}],
  "alerts": [{"level": "critical|warning|positive", "message": "<alert>"}],
  "forecast": {
    "next_period": "<label>",
    "revenue_estimate": "<value>",
    "ebitda_estimate": "<value>",
    "confidence": "high|medium|low",
    "rationale": "<2-3 sentences>",
    "actions": ["<action 1>", "<action 2>"]
  },
  "financial_narrative": "<3-4 sentences on financial position and trajectory>",
  "data_quality": "good|partial|insufficient",
  "missing_data": ["<field missing if any>"]
}"""
    }
}


# ── FILE PROCESSING ───────────────────────────────────────────────────────────

async def process_file(file: UploadFile):
    """Returns (text_content, pdf_bytes) — one will be None"""
    content = await file.read()
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(status_code=400, detail=f"Ficheiro {file.filename} demasiado grande (máx. 5MB)")
    ext = os.path.splitext(file.filename or "")[-1].lower()
    if ext not in ALLOWED_EXTS:
        raise HTTPException(status_code=400, detail=f"Formato não suportado: {ext}")

    if ext == ".pdf":
        return None, content
    elif ext in (".xlsx", ".xls"):
        try:
            import openpyxl
            wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True)
            rows = []
            for ws in wb.worksheets:
                rows.append(f"[Folha: {ws.title}]")
                for row in ws.iter_rows(values_only=True):
                    rows.append("\t".join(str(c) if c is not None else "" for c in row))
            return "\n".join(rows[:300]), None
        except Exception:
            raise HTTPException(status_code=400, detail=f"Não foi possível processar {file.filename}")
    else:
        return content.decode("utf-8", errors="replace")[:15000], None


# ── HEALTH ────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "service": "LabSuite API"}


# ── ANALYSE ───────────────────────────────────────────────────────────────────

@app.post("/analyze-sales")
@limiter.limit("15/hour")
async def analyze_sales(
    request: Request,
    input_mode: str = Form("text"),
    report_type: str = Form("sales"),
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
    op_costs: str = Form(""),
    extra_notes: str = Form(""),
    company_name: str = Form(""),
    currency: str = Form("EUR"),
    file1: Optional[UploadFile] = File(default=None),
    file2: Optional[UploadFile] = File(default=None),
    file3: Optional[UploadFile] = File(default=None),
    x_access_code: str | None = Header(default=None),
):
    verify_access(x_access_code)

    company_name = sanitize(company_name, 100)
    currency     = sanitize(currency, 10) or "EUR"
    report_type  = report_type if report_type in REPORT_PROMPTS else "sales"
    cfg          = REPORT_PROMPTS[report_type]

    message_content = []
    text_blocks     = []

    if input_mode == "file":
        files = [f for f in [file1, file2, file3] if f and f.filename]
        if not files:
            raise HTTPException(status_code=400, detail="Por favor seleccione pelo menos um ficheiro.")

        for i, f in enumerate(files[:MAX_FILES]):
            text, pdf_bytes = await process_file(f)
            label = f"Ficheiro {i+1}: {f.filename}"
            if pdf_bytes:
                encoded = base64.standard_b64encode(pdf_bytes).decode("utf-8")
                message_content.append({
                    "type": "document",
                    "source": {"type": "base64", "media_type": "application/pdf", "data": encoded},
                })
                message_content.append({"type": "text", "text": label + " (PDF acima)"})
            else:
                text_blocks.append(f"=== {label} ===\n{text}")

        if text_blocks:
            message_content.append({"type": "text", "text": "Dados em texto:\n\n" + "\n\n".join(text_blocks)})

        if len(files) > 1:
            message_content.append({"type": "text", "text": f"{len(files)} ficheiros fornecidos. Compara e analisa todos os períodos/fontes."})

    elif input_mode == "text":
        data = sanitize(raw_data, 15000)
        if not data:
            raise HTTPException(status_code=400, detail="Por favor cole os dados.")
        message_content = f"Dados fornecidos:\n\n{data}"

    else:  # form
        parts = []
        if period_current:   parts.append(f"Período actual: {period_current}")
        if period_previous:  parts.append(f"Período anterior: {period_previous}")
        if revenue_current:  parts.append(f"Receita/Volume actual: {revenue_current} {currency}")
        if revenue_previous: parts.append(f"Receita/Volume anterior: {revenue_previous} {currency}")
        if units_current:    parts.append(f"Output/Unidades actual: {units_current}")
        if units_previous:   parts.append(f"Output/Unidades anterior: {units_previous}")
        if top_products:     parts.append(f"Top produtos/serviços: {top_products}")
        if top_clients:      parts.append(f"Top clientes/unidades: {top_clients}")
        if top_salespeople:  parts.append(f"Top colaboradores/equipas: {top_salespeople}")
        if op_costs:         parts.append(f"Custos operacionais: {op_costs}")
        if extra_notes:      parts.append(f"Notas: {extra_notes}")
        if not parts:
            raise HTTPException(status_code=400, detail="Por favor preencha pelo menos alguns campos.")
        message_content = "Dados do formulário:\n\n" + "\n".join(parts)

    required_str = "\n".join(f"- {r}" for r in cfg["required_fields"])
    system_prompt = f"""You are an {cfg['role']}.
Tone: {cfg['tone']}
Currency: {currency}. Company: {company_name or "the company"}.

LANGUAGE: All text values in the JSON response MUST be written in European Portuguese (Portugal).
This includes: executive_summary, all KPI labels, alert messages, narratives, recommendations,
forecast rationale, actions, notes, and any other text field.
Only keep English for the fixed JSON keys and enum values (e.g. "trend", "up", "critical").

Data Quality Assessment — after analysing, set "data_quality":
- "good" — all required fields present
- "partial" — some fields missing, limited analysis
- "insufficient" — critical data missing

Required fields for this report type:
{required_str}

List exactly what is missing in "missing_data" (empty array [] if nothing missing).

If multiple files or periods are provided, compare them directly.

Respond ONLY with valid JSON — no markdown, no preamble.
Schema:
{cfg['schema']}
"""

    if isinstance(message_content, list):
        message_content.append({"type": "text", "text": "Analisa todos os dados e gera o relatório JSON."})
    else:
        message_content += "\n\nGera o relatório no formato JSON especificado."

    try:
        response = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=3500,
            system=system_prompt,
            messages=[{"role": "user", "content": message_content}],
        )
    except Exception:
        raise HTTPException(status_code=500, detail="Serviço de análise indisponível")

    raw = "".join(b.text for b in response.content if hasattr(b, "text")).strip()
    raw = re.sub(r"^```(?:json)?", "", raw).strip()
    raw = re.sub(r"```$", "", raw).strip()

    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="Erro ao processar resultado")

    return JSONResponse(content=result)


# ── CHAT ──────────────────────────────────────────────────────────────────────

@app.post("/chat")
@limiter.limit("60/hour")
async def chat(
    request: Request,
    x_access_code: str | None = Header(default=None),
):
    verify_access(x_access_code)

    body            = await request.json()
    messages        = body.get("messages", [])
    all_reports     = body.get("all_reports", {})
    report_context  = body.get("report_context", None)
    company_name    = body.get("company_name", "a empresa")
    currency        = body.get("currency", "EUR")
    asking_for_data = body.get("asking_for_data", False)

    if not messages:
        raise HTTPException(status_code=400, detail="Sem mensagens")

    messages = messages[-24:]

    # Build reports context
    reports_block = ""
    if all_reports:
        generated = {k: v for k, v in all_reports.items() if v}
        if generated:
            parts = []
            for rtype, rdata in generated.items():
                parts.append(f"\n--- RELATÓRIO {rtype.upper()} ---\n" + json.dumps(rdata, indent=2, ensure_ascii=False))
            reports_block = "\n\nRELATÓRIOS DA SESSÃO:" + "".join(parts)
    elif report_context:
        reports_block = "\n\nRELATÓRIO:\n" + json.dumps(report_context, indent=2, ensure_ascii=False)

    if asking_for_data:
        system_prompt = """És um assistente de recolha de dados. O relatório do utilizador teve dados insuficientes.
A tua função é fazer perguntas claras e amigáveis para recolher a informação em falta.
Faz UMA pergunta de cada vez. Sê específico sobre o formato necessário.
Quando tiveres informação suficiente, começa a resposta exactamente com: DADOS_SUFICIENTES"""
    else:
        system_prompt = f"""És um consultor financeiro e operacional expert integrado no LabSuite.
Estás a falar directamente com o dono ou gestor da empresa.

A tua função:
- Analisa os relatórios e responde usando os números reais
- Cruza informação entre relatórios quando disponível
- Dá conselhos concretos com valores: "se cortares X poupas Y em Z meses"
- Sê directo e preciso — como um CFO que cobra €500/hora
- Nunca sejas vago. Referencia sempre números específicos dos relatórios.
- Respostas concisas mas substanciais — sem rodeios.

Empresa: {company_name}
Moeda: {currency}{reports_block}

Se não houver relatórios, pede ao utilizador para gerar um primeiro.
Responde sempre no idioma em que o utilizador escreve."""

    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1500,
            system=system_prompt,
            messages=messages,
        )
    except Exception:
        raise HTTPException(status_code=500, detail="Serviço de chat indisponível")

    reply = "".join(b.text for b in response.content if hasattr(b, "text")).strip()
    has_enough_data = reply.startswith("DADOS_SUFICIENTES")
    if has_enough_data:
        reply = reply.replace("DADOS_SUFICIENTES", "").strip()

    return JSONResponse({"message": reply, "has_enough_data": has_enough_data})