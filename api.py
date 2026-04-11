import os, re, json, base64, io, uuid, time
from datetime import datetime, timezone
from collections import defaultdict
from fastapi import FastAPI, File, UploadFile, Form, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from typing import Optional
import anthropic
import jwt as pyjwt

# ── ENV ───────────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
JWT_SECRET        = os.environ.get("JWT_SECRET", "change-me-in-production")
ADMIN_SECRET      = os.environ.get("ADMIN_SECRET", "admin-secret-change-me")
ALLOWED_ORIGIN    = os.environ.get("ALLOWED_ORIGIN", "https://app.saleslab.cc,https://www.saleslab.cc,https://saleslab.cc,https://sales-lab-lovat.vercel.app,https://saleslab-website.vercel.app")
ALLOWED_ORIGINS   = [o.strip() for o in ALLOWED_ORIGIN.split(",") if o.strip()]

if not ANTHROPIC_API_KEY:
    raise RuntimeError("ANTHROPIC_API_KEY not set")

# ── PLAN CONFIGS ──────────────────────────────────────────────────────────────
PLAN_CONFIGS = {
    "free":    {"modules": 1, "analyses": 1,    "chatbot": False, "label": "Free"},
    "starter": {"modules": 2, "analyses": 15,   "chatbot": False, "label": "Starter"},
    "pro":     {"modules": 4, "analyses": 30,   "chatbot": False, "label": "Pro"},
    "team":    {"modules": 4, "analyses": 9999, "chatbot": True,  "label": "Team"},
    "admin":   {"modules": 4, "analyses": 9999, "chatbot": True,  "label": "Admin"},
    "test":    {"modules": 4, "analyses": 9999, "chatbot": True,  "label": "Teste"},
}

# ── IN-MEMORY STORES (reset on server restart — acceptable for MVP) ───────────
free_fingerprints: set   = set()         # fingerprints that already used free tier
free_ips: dict           = defaultdict(int)  # ip → number of free registrations
free_leads: list         = []            # [{email, ip, fp, ts}] — lead collection
usage_store: dict        = {}            # tid → {"month": "YYYY-MM", "count": N}

# ── APP ───────────────────────────────────────────────────────────────────────
limiter = Limiter(key_func=get_remote_address)
app = FastAPI(title="SalesLab API", docs_url=None, redoc_url=None)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
)

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
MAX_FILE_SIZE = 5 * 1024 * 1024
ALLOWED_EXTS  = {".csv", ".txt", ".xlsx", ".xls", ".pdf"}

# ── JWT HELPERS ───────────────────────────────────────────────────────────────

def issue_token(plan: str, email: str, test_days: int = 30) -> str:
    cfg = PLAN_CONFIGS[plan]
    now = int(time.time())
    if plan in ("admin", "free"):
        exp = now + 365 * 10 * 86400          # 10 years
    elif plan == "test":
        exp = now + test_days * 86400
    else:
        exp = now + 32 * 86400                 # ~1 month for paid plans
    payload = {
        "plan":     plan,
        "email":    email,
        "tid":      str(uuid.uuid4()),
        "iat":      now,
        "exp":      exp,
        "modules":  cfg["modules"],
        "analyses": cfg["analyses"],
        "chatbot":  cfg["chatbot"],
    }
    return pyjwt.encode(payload, JWT_SECRET, algorithm="HS256")

def decode_token(token: str) -> dict:
    if not token:
        raise HTTPException(status_code=401, detail="Token em falta. Faça login.")
    try:
        return pyjwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    except pyjwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Sessão expirada. Renove a sua subscrição.")
    except pyjwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Token inválido.")

def check_quota(payload: dict) -> int:
    """Consume one analysis. Returns remaining count. Raises if limit exceeded."""
    plan = payload["plan"]
    tid  = payload["tid"]
    limit = payload.get("analyses", PLAN_CONFIGS.get(plan, {}).get("analyses", 1))

    if plan in ("admin", "test", "team"):
        return 9999  # unlimited

    current_month = datetime.now(timezone.utc).strftime("%Y-%m")

    if tid not in usage_store or usage_store[tid]["month"] != current_month:
        usage_store[tid] = {"month": current_month, "count": 0}

    if usage_store[tid]["count"] >= limit:
        if plan == "free":
            raise HTTPException(
                status_code=429,
                detail="A sua análise gratuita já foi utilizada. Faça upgrade para continuar."
            )
        raise HTTPException(
            status_code=429,
            detail=f"Limite de {limit} análises mensais atingido. Faça upgrade para continuar."
        )

    usage_store[tid]["count"] += 1
    remaining = limit - usage_store[tid]["count"]
    return remaining

def sanitize(text: str, max_len: int) -> str:
    return (text or "").strip()[:max_len]

# ── FILE PROCESSING ───────────────────────────────────────────────────────────

async def process_file(file: UploadFile):
    """Returns (text_content, pdf_bytes) — one will be None."""
    content = await file.read()
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(status_code=400, detail=f"Ficheiro demasiado grande (máx. 5MB)")
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
            return "\n".join(rows[:400]), None
        except Exception:
            raise HTTPException(status_code=400, detail=f"Não foi possível processar {file.filename}")
    else:
        return content.decode("utf-8", errors="replace")[:15000], None

# ── REPORT PROMPTS ────────────────────────────────────────────────────────────

FORMATS_NOTICE = """DATA FORMATS — you accept all of these without exception:
- Excel or CSV files (from any software: ERP, CRM, accounting, custom sheets)
- PDF reports (management reports, P&L statements, invoicing summaries)
- Text pasted from any source (CRM dashboards, email reports, WhatsApp messages)
- Free-form descriptions with numbers embedded in prose
- Partial data — always analyse what is available; note clearly what is missing
- Mixed languages (Portuguese, English, Spanish — handle all)

CRITICAL RULE: Never refuse to analyse because the format is unusual or data is incomplete.
Extract whatever is present. Estimate or flag gaps. Always produce a useful output."""

REPORT_PROMPTS = {
    "sales": {
        "role": "elite sales analyst producing internal management reports",
        "tone": "direct, data-driven, commercially sharp. No fluff. Focus on what matters.",
        "context": "Companies submit data in any format they already have — CRM exports, Excel sheets, pasted tables, PDF invoicing reports, or free-form text. Extract whatever is available.",
        "schema": """{
  "report_type": "sales",
  "period": "<period label>",
  "executive_summary": "<3-5 sentence sharp executive summary>",
  "kpis": [{"label": "<KPI name>", "value": "<formatted value>", "change": "<% or absolute change>", "trend": "up|down|neutral"}],
  "top_products": [{"rank": 1, "name": "<n>", "value": "<revenue or units>", "note": "<insight>"}],
  "top_clients": [{"rank": 1, "name": "<n>", "value": "<revenue>", "note": "<insight>"}],
  "top_salespeople": [{"rank": 1, "name": "<n>", "value": "<revenue or deals>", "note": "<insight>"}],
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
        "context": "Companies submit any operational data they track — hours worked, units produced, team sheets, shift logs, productivity reports from any system.",
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
      "vs_average": "above|below|at",
      "note": "<insight or recommendation>"
    }
  ],
  "bottlenecks": [{"area": "<process or team>", "issue": "<cause>", "impact": "<cost or time lost>", "recommendation": "<action>"}],
  "alerts": [{"level": "critical|warning|positive", "message": "<concise alert>"}],
  "optimisation_actions": [{"priority": "high|medium|low", "action": "<specific action>", "expected_gain": "<saving or improvement in euros>"}],
  "summary_narrative": "<2-3 sentences on overall efficiency health>",
  "data_quality": "good|partial|insufficient",
  "missing_data": ["<field missing if any>"]
}"""
    },
    "cost": {
        "role": "cost reduction analyst specialising in identifying unnecessary expenditure",
        "tone": "direct, commercially ruthless, focused on margin protection. No fluff.",
        "context": "Companies submit any cost data they have — bank statements, accounting exports, invoices by category, expense sheets from any software (Primavera, SAP, Sage, PHC, Excel).",
        "schema": """{
  "report_type": "cost",
  "period": "<period label>",
  "executive_summary": "<3-5 sentence summary of cost health>",
  "total_costs": "<total expenditure>",
  "cost_breakdown": [{"category": "<category>", "amount": "<value>", "pct_of_total": "<%>", "vs_previous": "<change>", "status": "ok|review|critical"}],
  "unnecessary_costs": [{"item": "<cost item>", "current_spend": "<amount>", "benchmark": "<what it should be>", "excess": "<waste amount>", "action": "<what to do>"}],
  "savings_opportunities": [{"opportunity": "<description>", "estimated_saving": "<amount or %>", "effort": "low|medium|high", "timeframe": "<when>"}],
  "alerts": [{"level": "critical|warning|positive", "message": "<concise alert>"}],
  "total_recoverable": "<total estimated savings per year>",
  "priority_actions": ["<action 1>", "<action 2>", "<action 3>"],
  "data_quality": "good|partial|insufficient",
  "missing_data": ["<field missing if any>"]
}"""
    },
    "financial": {
        "role": "CFO-level financial analyst producing a P&L-style management overview",
        "tone": "authoritative, precise, structured for board-level consumption. No fluff.",
        "context": "Companies submit financial data in any form — P&L statements, accounting software exports (Primavera, SAP, Sage, PHC, QuickBooks), balance sheets, bank statements, or free-form summaries.",
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

# ── HEALTH ────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "service": "SalesLab API v2"}

# ── FREE TIER REGISTRATION ────────────────────────────────────────────────────

@app.post("/register-free")
@limiter.limit("10/hour")
async def register_free(
    request: Request,
    email: str = Form(...),
    fingerprint: str = Form(""),
):
    ip = get_remote_address(request)
    email = sanitize(email, 200).lower()
    fingerprint = sanitize(fingerprint, 200)

    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="Email inválido.")

    # Fingerprint abuse check
    if fingerprint and fingerprint in free_fingerprints:
        raise HTTPException(
            status_code=429,
            detail="Já utilizou a versão gratuita neste dispositivo. Faça upgrade para continuar."
        )

    # IP abuse check (max 2 free registrations per IP)
    if free_ips[ip] >= 2:
        raise HTTPException(
            status_code=429,
            detail="Limite de versões gratuitas atingido neste endereço. Faça upgrade para continuar."
        )

    # Register
    if fingerprint:
        free_fingerprints.add(fingerprint)
    free_ips[ip] += 1
    free_leads.append({"email": email, "ip": ip, "fp": fingerprint, "ts": int(time.time())})

    token = issue_token("free", email)
    return JSONResponse({"token": token, "plan": "free", "message": "Bem-vindo ao SalesLab!"})

# ── VALIDATE TOKEN ────────────────────────────────────────────────────────────

@app.post("/validate-token")
async def validate_token(request: Request):
    body = await request.json()
    token = body.get("token", "")
    payload = decode_token(token)
    tid = payload["tid"]
    plan = payload["plan"]

    # Calculate remaining analyses
    current_month = datetime.now(timezone.utc).strftime("%Y-%m")
    if plan in ("admin", "test", "team"):
        remaining = 9999
    else:
        usage = usage_store.get(tid, {"month": current_month, "count": 0})
        if usage["month"] != current_month:
            usage = {"month": current_month, "count": 0}
        limit = payload.get("analyses", PLAN_CONFIGS.get(plan, {}).get("analyses", 1))
        remaining = max(0, limit - usage["count"])

    return JSONResponse({
        "valid":     True,
        "plan":      plan,
        "label":     PLAN_CONFIGS.get(plan, {}).get("label", plan.capitalize()),
        "modules":   payload["modules"],
        "analyses":  payload["analyses"],
        "chatbot":   payload["chatbot"],
        "remaining": remaining,
        "email":     payload.get("email", ""),
    })

# ── ADMIN: GENERATE TOKEN ─────────────────────────────────────────────────────

@app.post("/admin/generate-token")
async def generate_token(
    request: Request,
    plan: str = Form(...),
    email: str = Form(""),
    test_days: int = Form(30),
    x_admin_secret: str | None = Header(default=None),
):
    if not x_admin_secret or x_admin_secret.strip() != ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="Acesso negado.")
    if plan not in PLAN_CONFIGS:
        raise HTTPException(status_code=400, detail=f"Plano inválido: {plan}")

    token = issue_token(plan, email or f"{plan}@saleslab.test", test_days=test_days)
    payload = decode_token(token)
    exp_dt = datetime.fromtimestamp(payload["exp"]).strftime("%d/%m/%Y")

    if email and "@" in email and RESEND_API_KEY:
        try:
            send_access_email(email, plan, token)
        except Exception as e:
            print(f"Erro ao enviar email admin: {e}")

    return JSONResponse({
        "token":   token,
        "plan":    plan,
        "email":   email,
        "expires": exp_dt,
        "modules": payload["modules"],
        "chatbot": payload["chatbot"],
        "email_sent": bool(email and "@" in email and RESEND_API_KEY),
    })

# ── ADMIN: LIST LEADS ─────────────────────────────────────────────────────────

@app.get("/admin/leads")
async def list_leads(
    request: Request,
    x_admin_secret: str | None = Header(default=None),
):
    if not x_admin_secret or x_admin_secret.strip() != ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="Acesso negado.")
    leads = [
        {**lead, "ts": datetime.fromtimestamp(lead["ts"]).strftime("%d/%m/%Y %H:%M")}
        for lead in free_leads
    ]
    return JSONResponse({"leads": leads, "total": len(leads)})

# ── ANALYSE ───────────────────────────────────────────────────────────────────

@app.post("/analyze-sales")
@limiter.limit("20/hour")
async def analyze_sales(
    request: Request,
    input_mode:      str  = Form("text"),
    report_type:     str  = Form("sales"),
    raw_data:        str  = Form(""),
    period_current:  str  = Form(""),
    period_previous: str  = Form(""),
    revenue_current: str  = Form(""),
    revenue_previous:str  = Form(""),
    units_current:   str  = Form(""),
    units_previous:  str  = Form(""),
    top_products:    str  = Form(""),
    top_clients:     str  = Form(""),
    top_salespeople: str  = Form(""),
    op_costs:        str  = Form(""),
    extra_notes:     str  = Form(""),
    company_name:    str  = Form(""),
    currency:        str  = Form("EUR"),
    file1: Optional[UploadFile] = File(default=None),
    file2: Optional[UploadFile] = File(default=None),
    file3: Optional[UploadFile] = File(default=None),
    x_token: str | None = Header(default=None),
):
    payload     = decode_token(x_token or "")
    remaining   = check_quota(payload)

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
        for i, f in enumerate(files[:3]):
            text, pdf_bytes = await process_file(f)
            label = f"Ficheiro {i+1}: {f.filename}"
            if pdf_bytes:
                encoded = base64.standard_b64encode(pdf_bytes).decode("utf-8")
                message_content.append({"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": encoded}})
                message_content.append({"type": "text", "text": label + " (PDF acima)"})
            else:
                text_blocks.append(f"=== {label} ===\n{text}")
        if text_blocks:
            message_content.append({"type": "text", "text": "Dados em texto:\n\n" + "\n\n".join(text_blocks)})
        if len(files) > 1:
            message_content.append({"type": "text", "text": f"{len(files)} ficheiros fornecidos. Compara e analisa todos."})

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

    system_prompt = f"""You are an {cfg['role']}.
Tone: {cfg['tone']}
Currency: {currency}. Company: {company_name or "the company"}.

{FORMATS_NOTICE}

Context: {cfg['context']}

LANGUAGE: All text values in the JSON MUST be written in European Portuguese (Portugal).
This includes summaries, KPI labels, alert messages, narratives, recommendations, and all text fields.
Only keep English for JSON keys and enum values (trend, up, critical, etc.).

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
        raise HTTPException(status_code=500, detail="Serviço de análise indisponível.")

    raw = "".join(b.text for b in response.content if hasattr(b, "text")).strip()
    raw = re.sub(r"^```(?:json)?", "", raw).strip()
    raw = re.sub(r"```$", "", raw).strip()

    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="Erro ao processar resultado.")

    result["analyses_remaining"] = remaining
    return JSONResponse(content=result)

# ── CHAT (Team / Admin / Test only) ──────────────────────────────────────────

@app.post("/chat")
@limiter.limit("60/hour")
async def chat(
    request: Request,
    x_token: str | None = Header(default=None),
):
    payload = decode_token(x_token or "")
    if not payload.get("chatbot"):
        raise HTTPException(
            status_code=403,
            detail="O chatbot está disponível apenas no plano Team. Faça upgrade para aceder."
        )

    body           = await request.json()
    messages       = body.get("messages", [])
    all_reports    = body.get("all_reports", {})
    report_context = body.get("report_context", None)
    company_name   = body.get("company_name", "a empresa")
    currency       = body.get("currency", "EUR")
    asking_for_data = body.get("asking_for_data", False)

    if not messages:
        raise HTTPException(status_code=400, detail="Sem mensagens.")

    messages = messages[-24:]

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
        system_prompt = """És um assistente de recolha de dados. O relatório teve dados insuficientes.
Faz UMA pergunta de cada vez para recolher a informação em falta. Sê específico sobre o formato.
Quando tiveres informação suficiente, começa a resposta exactamente com: DADOS_SUFICIENTES"""
    else:
        system_prompt = f"""És um consultor financeiro e operacional integrado no SalesLab.
Falas directamente com o gestor ou dono da empresa.

FORMATAÇÃO — OBRIGATÓRIA:
- Usa SEMPRE markdown
- Começa SEMPRE com ## que resume o tema
- Usa bullet points para listas de KPIs, factos e recomendações
- Destaca números com **bold** — ex: **€12.400**, **+23%**, **CRÍTICO**
- Termina SEMPRE com ## Próximo Passo com 1 acção concreta

CONTEÚDO:
- Usa os números reais dos relatórios — nunca inventes valores
- Cruza informação entre módulos quando disponível
- Dá conselhos com valores concretos: "se cortares X poupas Y em Z meses"
- Sê directo como um CFO que cobra €500/hora

Empresa: {company_name} | Moeda: {currency}{reports_block}

Se não houver relatórios, pede para gerar um primeiro.
Responde sempre no idioma em que o utilizador escreve."""

    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1500,
            system=system_prompt,
            messages=messages,
        )
    except Exception:
        raise HTTPException(status_code=500, detail="Serviço de chat indisponível.")

    reply = "".join(b.text for b in response.content if hasattr(b, "text")).strip()
    has_enough_data = reply.startswith("DADOS_SUFICIENTES")
    if has_enough_data:
        reply = reply.replace("DADOS_SUFICIENTES", "").strip()

    return JSONResponse({"message": reply, "has_enough_data": has_enough_data})

# ── STRIPE + RESEND ───────────────────────────────────────────────────────────

import stripe as stripe_lib
import resend as resend_lib

STRIPE_SECRET_KEY     = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
RESEND_API_KEY        = os.environ.get("RESEND_API_KEY", "")
FRONTEND_URL          = os.environ.get("FRONTEND_URL", "https://app.saleslab.cc")

stripe_lib.api_key = STRIPE_SECRET_KEY
resend_lib.api_key = RESEND_API_KEY

PRICE_TO_PLAN = {
    "price_1TKmgsF6K4YHMTtaxgCA7l9o": "team",
    "price_1TKmgcF6K4YHMTta3AKnAx7S": "pro",
    "price_1TKmgqF6K4YHMTtazLgx1vfD": "starter",
}

PLAN_LABELS = {
    "starter": "Starter",
    "pro":     "Pro",
    "team":    "Team",
}


def send_access_email(email: str, plan: str, token: str):
    """Send access token to customer via Resend."""
    label = PLAN_LABELS.get(plan, plan.capitalize())
    html = f"""
    <div style="background:#080808;padding:48px 32px;font-family:Georgia,serif;max-width:520px;margin:0 auto">
      <div style="margin-bottom:32px">
        <span style="color:#f0ede8;font-size:24px;font-weight:700">Sales</span>
        <span style="color:#c9a84c;font-size:24px;font-weight:700">Lab</span>
      </div>
      <h1 style="color:#f0ede8;font-size:28px;font-weight:300;margin-bottom:8px">
        Bem-vindo ao plano {label}.
      </h1>
      <p style="color:#888;font-size:15px;line-height:1.7;margin-bottom:32px">
        O teu acesso está pronto. Copia o código abaixo e usa-o para entrar na plataforma.
      </p>
      <div style="background:#161616;border:1px solid rgba(201,168,76,0.25);border-radius:8px;padding:20px;margin-bottom:32px">
        <p style="color:#888;font-size:11px;letter-spacing:0.12em;text-transform:uppercase;margin-bottom:8px">
          Código de acesso
        </p>
        <p style="color:#c9a84c;font-size:13px;font-family:monospace;word-break:break-all;margin:0">
          {token}
        </p>
      </div>
      <a href="{FRONTEND_URL}" style="display:inline-block;background:#c9a84c;color:#000;padding:14px 28px;border-radius:6px;text-decoration:none;font-size:14px;font-weight:700;margin-bottom:32px">
        Entrar na plataforma →
      </a>
      <p style="color:#444;font-size:12px;line-height:1.6">
        Guarda este email — o código é necessário sempre que queiras aceder.<br>
        Dúvidas? Responde a este email ou contacta geralsaleslab@gmail.com
      </p>
    </div>
    """
    resend_lib.Emails.send({
        "from":    "SalesLab <noreply@saleslab.cc>",
        "to":      [email],
        "subject": f"O teu acesso SalesLab {label} está pronto",
        "html":    html,
    })


@app.post("/create-checkout-session")
@limiter.limit("10/hour")
async def create_checkout_session(request: Request):
    body = await request.json()
    price_id = body.get("price_id", "")
    email    = body.get("email", "")

    if price_id not in PRICE_TO_PLAN:
        raise HTTPException(status_code=400, detail="Plano inválido.")

    if not STRIPE_SECRET_KEY:
        raise HTTPException(status_code=500, detail="Stripe não configurado.")

    try:
        session = stripe_lib.checkout.Session.create(
            mode="subscription",
            payment_method_types=["card"],
            line_items=[{"price": price_id, "quantity": 1}],
            customer_email=email if email else None,
            success_url=f"{FRONTEND_URL}?payment=success&session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{FRONTEND_URL}?payment=cancelled",
            metadata={"price_id": price_id},
        )
        return JSONResponse({"url": session.url})
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao criar sessão: {str(e)}")


@app.post("/stripe-webhook")
async def stripe_webhook(request: Request):
    payload    = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    if not STRIPE_WEBHOOK_SECRET:
        raise HTTPException(status_code=500, detail="Webhook secret não configurado.")

    try:
        event = stripe_lib.Webhook.construct_event(
            payload, sig_header, STRIPE_WEBHOOK_SECRET
        )
    except stripe_lib.errors.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Assinatura inválida.")

    if event["type"] == "checkout.session.completed":
        session  = event["data"]["object"]
        email    = session.get("customer_email") or session.get("customer_details", {}).get("email", "")
        price_id = session.get("metadata", {}).get("price_id", "")
        plan     = PRICE_TO_PLAN.get(price_id)

        if email and plan:
            token = issue_token(plan, email)
            try:
                send_access_email(email, plan, token)
            except Exception as e:
                print(f"Erro ao enviar email: {e}")

    return JSONResponse({"status": "ok"})
