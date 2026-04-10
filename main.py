from fastapi import FastAPI, Request, Form, Body, BackgroundTasks
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
from groq import Groq
from typing import Optional
from PIL import Image
from PIL import ImageEnhance
from ollama import Client
import json
import os
import sys
import io
from click import prompt
import httpx
import ollama
import io



app = FastAPI()

# Umbrel Proxy Support
app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")

# Session config
app.add_middleware(SessionMiddleware, secret_key="p1unknown-bridge-secret-key", max_age=1800)

# Global status for progress tracking
status = {"stage": "idle", "doc_id": None}

# Progress stages mapping
stages = {
    "idle": 0,
    "Starting analysis": 10,
    "Fetching document": 20,
    "Checking OCR": 30,
    "Processing with local AI": 50,
    "Requesting image": 40,
    "Optimizing image": 60,
    "Sending to AI vision": 70,
    "Parsing response": 80,
    "Updating document": 90,
    "Completed": 100
}

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
        "groq_model": "",
        "tag_map": {"Receipt": 1, "Invoice": 2},
        "ollama_host": "",
        "AI_model": "",
        "AI_vision_model": "",
        "Prompt": ""
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
    return templates.TemplateResponse(request=request, name="login.html")

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
            p_url = f"http://{conf['umbrel_ip']}/api/documents/?page_size=15"
            p_resp = await client.get(p_url, headers=headers)
            all_docs = p_resp.json().get('results', [])
    except Exception as e:
        print(f"Fetch Error: {e}", flush=True)

    return templates.TemplateResponse(
        request,
        name="dashboard.html",
        context={"all_docs": all_docs, "history": history}
    )
@app.post("/webhook")
async def paperless_webhook(request: Request, background_tasks: BackgroundTasks):
    try:
        data = await request.json()
        doc_id = data.get('document_id')
        if doc_id:
            print(f"Webhook received for document ID: {doc_id}", flush=True)
            background_tasks.add_task(analyze_document_background, doc_id)
            return {"status": "success", "message": f"Analysis started for document ID: {doc_id}"}
        return {"status": "error", "message": "No document_id found"}
    except Exception as e:
        print(f"Webhook Error: {e}", flush=True)
        return {"status": "error", "message": str(e)}  


@app.post("/analyze-manual")
async def analyze_manual(request: Request, background_tasks: BackgroundTasks, doc_id: int = Form(...)):
    if not request.session.get("user"): return RedirectResponse(url="/login")
    global status
    status = {"stage": "Starting analysis", "doc_id": doc_id}
    background_tasks.add_task(analyze_document_background, doc_id)
    return HTMLResponse('<div id="progress" hx-get="/status" hx-trigger="every 1s" hx-target="#progress" hx-swap="innerHTML"><progress value="10" max="100"></progress> Starting analysis...</div>')

async def analyze_document_background(doc_id):
    await analyze_document(doc_id)

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
                        groq_key: str = Form(default=""), groq_model: str = Form(default=""), tag_map: str = Form(...),
                        ollama_host: str = Form(...), AI_model: str = Form(...),
                        AI_vision_model: str = Form(...), Prompt: str = Form(...)):
    if not request.session.get("user"): return RedirectResponse(url="/login")
    conf = load_config()
    if current_password != conf.get("admin_pass"):
        return templates.TemplateResponse(request=request, name="settings.html", context={"config": conf, "error": "Invalid password."})
    
    if not AI_model.strip() or len(AI_model) > 100:
        return templates.TemplateResponse(request=request, name="settings.html", context={"config": conf, "error": "AI Model must be non-empty and less than 100 characters."})
    
    if not AI_vision_model.strip() or len(AI_vision_model) > 100:
        return templates.TemplateResponse(request=request, name="settings.html", context={"config": conf, "error": "AI Vision Model must be non-empty and less than 100 characters."})
    
    if not Prompt.strip() or len(Prompt) > 2000:
        return templates.TemplateResponse(request=request, name="settings.html", context={"config": conf, "error": "Prompt must be non-empty and less than 2000 characters."})
    
    try:
        parsed_tags = json.loads(tag_map)
    except:
        return templates.TemplateResponse(request=request, name="settings.html", context={"config": conf, "error": "Invalid JSON in Tag Map."})

    new_data = {
        "admin_user": admin_user, "admin_pass": admin_pass, "umbrel_ip": umbrel_ip,
        "paperless_token": paperless_token, "groq_key": groq_key, "groq_model": groq_model,
        "tag_map": parsed_tags, "ollama_host": ollama_host,
        "AI_model": AI_model, "AI_vision_model": AI_vision_model, "Prompt": Prompt
    }

    if new_data == conf:
        return templates.TemplateResponse(request=request, name="settings.html", context={"config": conf, "success": "No changes were made."})

    with open(CONFIG_FILE, "w") as f:
        json.dump(new_data, f, indent=4)
    return RedirectResponse(url="/settings?success=1", status_code=303)

# --- STATUS ENDPOINT ---
@app.get("/status", response_class=HTMLResponse)
async def get_status():
    global status
    stage = status["stage"]
    if stage == "completed":
        status["stage"] = "idle"
        return HTMLResponse('<div>Analysis completed. Reloading page...</div><div hx-get="/dashboard" hx-trigger="load delay:1s" hx-target="body" hx-swap="innerHTML"></div>')
    elif "Error" in stage:
        return HTMLResponse(f'<div style="color:red;">{stage}</div>')
    else:
        value = stages.get(stage, 0)
        return HTMLResponse(f'<progress value="{value}" max="100"></progress> {stage}')

# --- 4. THE AI ENGINE ---
@app.post("/analyze/{doc_id}")
@app.post("/analyze/")
@app.get("/analyze/{doc_id}")
async def analyze_document(doc_id: Optional[int] = None, payload: dict = Body(None)):
    global status
    if doc_id is None and payload:
        doc_id = payload.get("document_id") or payload.get("id")
    
    if doc_id is None:
        return {"status": "error", "message": "No ID provided"}

    print(f"START: Processing Doc {doc_id}", flush=True)
    status["stage"] = "Fetching document"
    conf = load_config()
    p_url = f"http://{conf['umbrel_ip']}"
    headers = {"Authorization": f"Token {conf['paperless_token']}", "Content-Type": "application/json"}
    
    async with httpx.AsyncClient(timeout=120.0) as client:
        try:
            doc_resp = await client.get(f"{p_url}/api/documents/{doc_id}/", headers=headers)
            doc_data = doc_resp.json()
            
            ai_data, method = {}, "None"

            local_client = Client(host=conf['ollama_host'], timeout=1200.0)
            # Attempt Local Vision
            try:
                print(">>> Step 1: Requesting image from server...", flush=True)
                ocr_text = doc_data.get('content', '').strip()
                is_ocr_useful = len(ocr_text) > 200 and any(x in ocr_text.lower() for x in ['total', 'amount', 'balance', 'sum'] )
                print(f"DEBUG: OCR Text content: {ocr_text[:2000]}...", flush=True)
                status["stage"] = "Checking OCR"
                if ocr_text:
                    print(f">>> OCR text found, using text. ({len(ocr_text)} chars)", flush=True)
                    status["stage"] = "Processing with local OCR"
                    res = local_client.generate(
                        model=conf['AI_model'], 
                        prompt=f"{conf['Prompt']}\n\nText:\n{ocr_text[:2000]}",
                        format='json', 
                        options={'temperature': 0.0}
                    )
                    method = "Local OCR"
                else:
                    print(">>> OCR looks incomplete or missing. Forcing Vision scan...", flush=True)
                    status["stage"] = "OCR looks incomplete or missing. Forcing Vision scan..."
                    img_res = await client.get(f"{p_url}/api/documents/{doc_id}/thumb/", headers=headers)
                    image = Image.open(io.BytesIO(img_res.content))
                    image.thumbnail((750, 750))
                    image = ImageEnhance.Contrast(image).enhance(1.5)

                    img_byte_arr = io.BytesIO()
                    image.save(img_byte_arr, format='JPEG', quality=85)
                    optimized_img = img_byte_arr.getvalue()
                    
                    print(">>> Step 2: Optimizing image...", flush=True)
                    status["stage"] = "Optimizing image"
                    
                    print(">>> Step 3: Sending to AI model...", flush=True)
                    status["stage"] = "Sending to AI vision"
                    res = local_client.generate(
                        model=conf['AI_vision_model'], 
                        prompt=conf['Prompt'],
                        images=[optimized_img],
                        format='json', 
                        options={'temperature': 0.0}
                    )
                    method = "Local Vision"

                print(f"DEBUG: AI Output: {res['response']}", flush=True)
                status["stage"] = "Parsing response"
                ai_data, method = json.loads(res['response']), "Local OCR"

                raw_total = ai_data.get('total') or ai_data.get('amount') or ai_data.get('grand_total') or 0.0
                if isinstance(raw_total, str):
                    import re
                    cleaned = re.sub(r'[^\d\.\-]', '', raw_total)
                    ai_data['total'] = float(cleaned) if cleaned else 0.0
                else:
                    ai_data['total'] = float(raw_total)
           
            except Exception as e:
                print(f"!!! LOCAL AI CRASHED at Step {method}: {e}", flush=True)
                status["stage"] = f"Error activating: {e}"
                if conf.get('groq_key') and conf.get('groq_model'):
                    print(f"Falling back to Groq: {e}", flush=True)
                    g_client = Groq(api_key=conf['groq_key'])
                    completion = g_client.chat.completions.create(
                        model=conf['groq_model'],
                        response_format={"type": "json_object"},
                        messages=[{"role": "user", "content": f"{conf['Prompt']}: {doc_data.get('content', '')[:3000]}"}]
                    )
                    ai_data, method = json.loads(completion.choices[0].message.content), "Groq Cloud"
                else:
                    print("No Groq fallback configured, raising error", flush=True)
                    raise e

            print(f"SUCCESS: Handled via {method}. Total: {ai_data.get('total')}, Vendor: {ai_data.get('vendor')}, Date: {ai_data.get('date')}", flush=True)

            # Apply updates
            status["stage"] = "Updating document"
            
            tag_id = conf['tag_map'].get(ai_data.get('document_type'))
            tags_payload = [tag_id] if tag_id else []
            resp = await client.patch(f"{p_url}/api/documents/{doc_id}/", headers=headers,
                               json={"title": f"{ai_data.get('date')} - {ai_data.get('vendor')} - {ai_data.get('total')}", "tags": tags_payload})
            if resp.status_code not in (200, 201, 204):
                raise Exception(f"Failed to update document: {resp.status_code} {resp.text}")
            
            add_to_history({
                "doc_id": doc_id, 
                "vendor": ai_data.get('vendor'), 
                "amount": ai_data.get('total'), 
                "method": method
            })
            
            status["stage"] = "Completed"
            print(f"SUCCESS: Doc {doc_id} handled via {method}", flush=True)
            return {"status": "success", "method": method}
        except Exception as e:
            status["stage"] = f"Error: {e}"
            print(f"FATAL ERROR: {e}", flush=True)
            return {"status": "error", "message": str(e)}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
