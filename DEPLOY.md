# SalesLab — Guia de Deployment

## Ficheiros
- `api.py`           → Backend FastAPI
- `index.html`       → Frontend (Vercel)
- `requirements.txt` → Dependências Python

---

## 1. Backend → Railway (novo projecto)

```bash
mkdir saleslab-api && cd saleslab-api
cp /path/api.py .
cp /path/requirements.txt .

# Procfile
echo "web: uvicorn api:app --host 0.0.0.0 --port \$PORT" > Procfile

git init && git add . && git commit -m "init"
# Criar repo no GitHub → push → Railway conecta
```

### Variables no Railway
| Variable          | Valor                         |
|-------------------|-------------------------------|
| ANTHROPIC_API_KEY | sk-ant-...                    |
| ACCESS_CODE       | (código secreto)              |
| ALLOWED_ORIGIN    | https://saleslab-xxx.vercel.app |

---

## 2. Frontend → Vercel

1. Editar `index.html` linhas:
   - `BACKEND_URL` → URL do Railway
   - `ACCESS_CODE` → mesmo código do Railway

2. Deploy:
```bash
vercel --prod
```

3. Copiar URL Vercel → atualizar `ALLOWED_ORIGIN` no Railway

---

## 3. Arrancar local (porta 8002 para não colidir)

```bash
source ~/crewai-env/bin/activate
pip install -r requirements.txt --break-system-packages
export ANTHROPIC_API_KEY="sk-ant-..."
export ACCESS_CODE="teste123"
export ALLOWED_ORIGIN="*"
uvicorn api:app --reload --port 8002
```

---

## 4. Testar

```bash
# Com CSV
curl -X POST http://localhost:8002/analyze-sales \
  -H "X-Access-Code: teste123" \
  -F "input_mode=file" \
  -F "file=@vendas.csv" \
  -F "company_name=Empresa Teste" \
  -F "currency=EUR"

# Com texto livre
curl -X POST http://localhost:8002/analyze-sales \
  -H "X-Access-Code: teste123" \
  -F "input_mode=text" \
  -F "raw_data=Março 2026: €142k receita. Fev 2026: €118k. Top: Software Pro €54k" \
  -F "company_name=Empresa Teste"
```

---

## Portas locais — suite completa
| Agente     | Porta |
|------------|-------|
| RecruitLab | 8000  |
| SalesLab   | 8002  |
