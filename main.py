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
        <head>
            <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/water.css@2/out/water.css">
            <style>
                body.dark { background: #10121a; color: #e5e9f0; }
                body.dark form { background: #1b2232; border: 1px solid #32406b; }
                body.dark input, body.dark button { background: #1d2738; color: #e5e9f0; border-color: #32406b; }
                body.dark label { color: #c9d1e8; }
                body.dark a { color: #95c5ff; }
                .theme-toggle { position: absolute; right: 20px; top: 20px; padding: 8px 12px; border: 1px solid #888; border-radius: 6px; background: transparent; cursor: pointer; }
            </style>
        </head>
        <body style="display:flex; justify-content:center; align-items:center; height:100vh; position:relative;">
            <button id="themeToggle" class="theme-toggle" type="button">Switch to Dark Mode</button>
            <form action="/login" method="post" style="width:300px;">
                <h2 style="text-align:center;">🔐 AI Bridge Login</h2>
                <label>Username</label><input type="text" name="username" required>
                <label>Password</label><input type="password" name="password" required>
                <button type="submit" style="width:100%;">Login</button>
            </form>
            <script>
                const themeToggle = document.getElementById('themeToggle');
                const setTheme = theme => {
                    document.body.classList.toggle('dark', theme === 'dark');
                    themeToggle.textContent = theme === 'dark' ? 'Switch to Light Mode' : 'Switch to Dark Mode';
                };
                const savedTheme = localStorage.getItem('ai_bridge_theme') || 'light';
                setTheme(savedTheme);
                themeToggle?.addEventListener('click', () => {
                    const nextTheme = document.body.classList.contains('dark') ? 'light' : 'dark';
                    localStorage.setItem('ai_bridge_theme', nextTheme);
                    setTheme(nextTheme);
                });
            </script>
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

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={"request": request, "all_docs": all_docs, "history": history}
    )

@app.post("/analyze-manual")
async def analyze_manual(request: Request, doc_id: int = Form(...)):
    if not request.session.get("user"): return RedirectResponse(url="/login")
    await analyze_document(doc_id)
    return RedirectResponse(url="/dashboard", status_code=303)

@app.get("/settings", response_class=HTMLResponse)
async def get_settings(request: Request):
    if not request.session.get("user"): return RedirectResponse(url="/login")
    conf = load_config()
    success = None
    if request.query_params.get("success") == "1":
        success = "Settings saved successfully."
    return templates.TemplateResponse(request=request, name="settings.html", context={"config": conf, "success": success})

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

    if new_data == conf:
        return templates.TemplateResponse(request=request, name="settings.html", context={"config": conf, "success": "No changes were made."})

    with open(CONFIG_FILE, "w") as f:
        json.dump(new_data, f, indent=4)
    return RedirectResponse(url="/settings?success=1", status_code=303)

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
