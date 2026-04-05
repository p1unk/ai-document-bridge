import json
import os
import httpx
import ollama
from fastapi import FastAPI, Request, Form, Body
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
from groq import Groq
from typing import Optional

app = FastAPI()

# Umbrel Proxy Support
app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")

# Session config
app.add_middleware(SessionMiddleware, secret_key="p1unknown-bridge-secret-key", max_age=1800)

# --- UMBREL PERSISTENCE PATHS ---
DATA_DIR = os.path.join(os.getcwd(), "data")
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
HISTORY_FILE = os.path.join(DATA_DIR, "history.json")
templates = Jinja2Templates(directory="templates")

# Ensure the persistent data directory exists
if not os.path.exists(DATA_DIR):
    os.makedirs(DATA_DIR, exist_ok=True)

# --- 1. HELPERS ---
def load_config():
    default = {
        "admin_user": "admin", 
        "admin_pass": "password123", 
        "umbrel_ip": "paperless-ngx", 
        "paperless_token": "",
        "groq_key": "",
        "tag_map": {"Receipt": 1, "Invoice": 2},
        "ollama_host": "http://ollama:11434"
    }
    if not os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "w") as f:
            json.dump(default, f, indent=4)
        return default
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)

def add_to_history(entry):
    history = []
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r") as f:
                history = json.load(f)
        except:
            history = []
    history.insert(0, entry)
    with open(HISTORY_FILE, "w") as f:
        json.dump(history[:10], f, indent=4)

# --- 2. AUTH ROUTES ---
@app.get("/", response_class=RedirectResponse)
async def root():
    return RedirectResponse(url="/dashboard")

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return """
    <html>
        <head><link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/water.css@2/out/water.css"></head>
        <body style="display:flex; justify-content:center; align-items:center; height:100vh;">
            <form action="/login" method="post" style="width:300px;">
                <h2 style="text-align:center;">🔐 AI Bridge Login</h2>
                <label>Username</label><input type="text" name="username" required>
                <label>Password</label><input type="password" name="password" required>
                <button type="submit" style="width:100%;">Login</button>
            </form>
        </body>
    </html>
    """

@app.post("/login")
async def do_login(request: Request, username: str = Form(...), password: str = Form(...)):
    conf = load_config()
    if username == conf.get("admin_user") and password == conf.get("admin_pass"):
        request.session["user"] = username
        return RedirectResponse(url="/dashboard", status_code=303)
    return RedirectResponse(url="/login", status_code=303)

@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login")

# --- 3. DASHBOARD ---
@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    if not request.session.get("user"):
        return RedirectResponse(url="/login")
    
    conf = load_config()
    history = []
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "r") as f:
            history = json.load(f)

    all_docs = []
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            headers = {"Authorization": f"Token {conf.get('paperless_token')}"}
            p_url = f"http://{conf['umbrel_ip']}:2349/api/documents/?page_size=15"
            p_resp = await client.get(p_url, headers=headers)
            all_docs = p_resp.json().get('results', [])
    except Exception as e:
        print(f"Fetch Error: {e}", flush=True)

    options = "".join([f"<option value='{d['id']}'>{d['title']} (ID: {d['id']})</option>" for d in all_docs])
    rows = "".join([f"<tr><td>{i['doc_id']}</td><td>{i.get('vendor', 'N/A')}</td><td>{i.get('amount', '0.00')}</td><td>{i.get('method', 'Cloud')}</td></tr>" for i in history])

    return f"""
    <html>
        <head><link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/water.css@2/out/water.css"></head>
        <body>
            <div style="display:flex; justify-content:space-between; align-items:center;">
                <h1>📊 AI Activity</h1>
                <nav><a href="/settings">Settings</a> | <a href="/logout">Logout</a></nav>
            </div>
            <h3>🚀 Manual Analyze</h3>
            <form action="/analyze-manual" method="post" style="display:flex; gap:10px; align-items: flex-end;">
                <div style="flex-grow:1;">
                    <select name="doc_id">{options or "<option>No documents found</option>"}</select>
                </div>
                <button type="submit">Analyze</button>
            </form>
            <hr>
            <table>
                <thead><tr><th>ID</th><th>Vendor</th><th>Amount</th><th>Method</th></tr></thead>
                <tbody>{rows or "<tr><td colspan='4'>Waiting for first document...</td></tr>"}</tbody>
            </table>
        </body>
    </html>
    """

@app.post("/analyze-manual")
async def analyze_manual(request: Request, doc_id: int = Form(...)):
    if not request.session.get("user"): return RedirectResponse(url="/login")
    await analyze_document(doc_id)
    return RedirectResponse(url="/dashboard", status_code=303)

@app.get("/settings", response_class=HTMLResponse)
async def get_settings(request: Request):
    if not request.session.get("user"): return RedirectResponse(url="/login")
    conf = load_config()
    return templates.TemplateResponse(request=request, name="settings.html", context={"config": conf})

@app.post("/settings")
async def post_settings(request: Request, current_password: str = Form(...),
                        admin_user: str = Form(...), admin_pass: str = Form(...),
                        umbrel_ip: str = Form(...), paperless_token: str = Form(...),
                        groq_key: str = Form(...), tag_map: str = Form(...),
                        ollama_host: str = Form(...)):
    if not request.session.get("user"): return RedirectResponse(url="/login")
    conf = load_config()
    if current_password != conf.get("admin_pass"):
        return templates.TemplateResponse(request=request, name="settings.html", context={"config": conf, "error": "Invalid password."})
    
    try:
        parsed_tags = json.loads(tag_map)
    except:
        return templates.TemplateResponse(request=request, name="settings.html", context={"config": conf, "error": "Invalid JSON in Tag Map."})

    new_data = {
        "admin_user": admin_user, "admin_pass": admin_pass, "umbrel_ip": umbrel_ip,
        "paperless_token": paperless_token, "groq_key": groq_key, 
        "tag_map": parsed_tags, "ollama_host": ollama_host
    }
    with open(CONFIG_FILE, "w") as f:
        json.dump(new_data, f, indent=4)
    return RedirectResponse(url="/settings", status_code=303)

# --- 4. THE AI ENGINE ---
@app.post("/analyze/{doc_id}")
@app.post("/analyze/")
@app.get("/analyze/{doc_id}")
async def analyze_document(doc_id: Optional[int] = None, payload: dict = Body(None)):
    if doc_id is None and payload:
        doc_id = payload.get("document_id") or payload.get("id")
    
    if doc_id is None:
        return {"status": "error", "message": "No ID provided"}

    print(f"START: Processing Doc {doc_id}", flush=True)
    conf = load_config()
    p_url = f"http://{conf['umbrel_ip']}:2349"
    headers = {"Authorization": f"Token {conf['paperless_token']}", "Content-Type": "application/json"}
    
    async with httpx.AsyncClient(timeout=120.0) as client:
        try:
            doc_resp = await client.get(f"{p_url}/api/documents/{doc_id}/", headers=headers)
            doc_data = doc_resp.json()
            
            ai_data, method = {}, "None"
            # Attempt Local Vision
            try:
                img = await client.get(f"{p_url}/api/documents/{doc_id}/thumb/", headers=headers)
                res = ollama.generate(model='llama3.2-vision', 
                                      prompt="Return JSON only: vendor, date (YYYY-MM-DD), total_amount, document_type.",
                                      images=[img.content], format='json', host=conf['ollama_host'])
                ai_data, method = json.loads(res['response']), "Local Vision"
            except Exception as e:
                print(f"Falling back to Groq: {e}", flush=True)
                g_client = Groq(api_key=conf['groq_key'])
                completion = g_client.chat.completions.create(
                    model="llama-3.3-70b-versatile",
                    response_format={"type": "json_object"},
                    messages=[{"role": "user", "content": f"Extract JSON (vendor, date, total_amount, document_type) from: {doc_data.get('content', '')[:3000]}"}]
                )
                ai_data, method = json.loads(completion.choices[0].message.content), "Groq Cloud"

            # Apply updates
            tag_id = conf['tag_map'].get(ai_data.get('document_type'), 1)
            await client.patch(f"{p_url}/api/documents/{doc_id}/", headers=headers,
                               json={"title": f"{ai_data.get('date')} - {ai_data.get('vendor')}", "tags": [tag_id]})
            
            add_to_history({
                "doc_id": doc_id, 
                "vendor": ai_data.get('vendor'), 
                "amount": ai_data.get('total_amount'), 
                "method": method
            })
            
            print(f"SUCCESS: Doc {doc_id} handled via {method}", flush=True)
            return {"status": "success", "method": method}
        except Exception as e:
            print(f"FATAL ERROR: {e}", flush=True)
            return {"status": "error", "message": str(e)}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)