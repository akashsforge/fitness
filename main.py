from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import os
import datetime
from dotenv import load_dotenv
from supabase import create_client
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

load_dotenv()

url = os.environ.get("SUPABASE_URL")
key = os.environ.get("SUPABASE_KEY")
supabase = create_client(url, key)

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")
REDIRECT_URI = "https://silver-acorn-97rwv5v76prjc7v9v-8000.app.github.dev/auth/google/callback"

CLIENT_CONFIG = {
    "web": {
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": [REDIRECT_URI],
    }
}

SCOPES = ["https://www.googleapis.com/auth/fitness.activity.read"]

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


def fetch_google_steps(access_token: str):
    try:
        creds = Credentials(token=access_token)
        service = build("fitness", "v1", credentials=creds)

        now = datetime.datetime.utcnow()
        start_of_day = datetime.datetime(now.year, now.month, now.day)
        start_millis = int(start_of_day.timestamp() * 1000)
        end_millis = int(now.timestamp() * 1000)

        result = service.users().dataset().aggregate(
            userId="me",
            body={
                "aggregateBy": [{"dataTypeName": "com.google.step_count.delta"}],
                "bucketByTime": {"durationMillis": 86400000},
                "startTimeMillis": start_millis,
                "endTimeMillis": end_millis,
            }
        ).execute()

        steps = 0
        for bucket in result.get("bucket", []):
            for ds in bucket.get("dataset", []):
                for point in ds.get("point", []):
                    for value in point.get("value", []):
                        steps += value.get("intVal", 0)
        return steps
    except Exception:
        return None


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse(request, "home.html", {})


@app.get("/coach-login", response_class=HTMLResponse)
async def coach_login_page(request: Request):
    return templates.TemplateResponse(request, "coach_login.html", {})


@app.get("/client-login", response_class=HTMLResponse)
async def client_login_page(request: Request):
    return templates.TemplateResponse(request, "client_login.html", {})


@app.get("/signup", response_class=HTMLResponse)
async def signup_page(request: Request):
    return templates.TemplateResponse(request, "signup.html", {})


@app.post("/signup")
async def signup(name: str = Form(...), email: str = Form(...), password: str = Form(...)):
    try:
        auth_response = supabase.auth.sign_up({"email": email, "password": password})
        user_id = auth_response.user.id
        supabase.table("users").insert({
            "id": user_id,
            "email": email,
            "name": name,
            "role": "coach"
        }).execute()
        return RedirectResponse(url="/coach-login", status_code=303)
    except Exception as e:
        return HTMLResponse(f"Signup failed: {str(e)}")


@app.get("/client-signup", response_class=HTMLResponse)
async def client_signup_page(request: Request):
    coaches = supabase.table("users").select("id, name").eq("role", "coach").execute()
    return templates.TemplateResponse(request, "client_signup.html", {"coaches": coaches.data})


@app.post("/client-signup")
async def client_signup(name: str = Form(...), email: str = Form(...), password: str = Form(...), coach_id: str = Form(...)):
    try:
        auth_response = supabase.auth.sign_up({"email": email, "password": password})
        user_id = auth_response.user.id

        supabase.table("users").insert({
            "id": user_id,
            "email": email,
            "name": name,
            "role": "client"
        }).execute()

        supabase.table("coach_clients").insert({
            "coach_id": coach_id,
            "client_id": user_id
        }).execute()

        return RedirectResponse(url="/client-login", status_code=303)
    except Exception as e:
        return HTMLResponse(f"Signup failed: {str(e)}")


@app.post("/login")
async def login(email: str = Form(...), password: str = Form(...)):
    try:
        auth_response = supabase.auth.sign_in_with_password({"email": email, "password": password})
        user_id = auth_response.user.id

        user_result = supabase.table("users").select("role").eq("id", user_id).execute()
        role = user_result.data[0]["role"] if user_result.data else "coach"

        destination = "/dashboard" if role == "coach" else "/client-dashboard"
        response = RedirectResponse(url=destination, status_code=303)
        response.set_cookie(key="user_id", value=user_id, httponly=True, max_age=3600)
        return response
    except Exception as e:
        return HTMLResponse(f"Login failed: {str(e)}")


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    coach_id = request.cookies.get("user_id")
    if not coach_id:
        return RedirectResponse(url="/coach-login", status_code=303)

    links = supabase.table("coach_clients").select("client_id").eq("coach_id", coach_id).execute()
    client_ids = [row["client_id"] for row in links.data]

    clients = []
    if client_ids:
        result = supabase.table("users").select("*").in_("id", client_ids).execute()
        clients = result.data

    return templates.TemplateResponse(request, "dashboard.html", {"clients": clients})


@app.get("/api/notifications")
async def get_notifications(request: Request):
    coach_id = request.cookies.get("user_id")
    if not coach_id:
        return JSONResponse({"notifications": []})

    links = supabase.table("coach_clients").select("client_id").eq("coach_id", coach_id).execute()
    client_ids = [row["client_id"] for row in links.data]

    if not client_ids:
        return JSONResponse({"notifications": []})

    cutoff = (datetime.datetime.utcnow() - datetime.timedelta(seconds=60)).isoformat()

    recent = supabase.table("assigned_tasks").select("*").in_("client_id", client_ids) \
        .eq("status", "completed") \
        .gte("completed_at", cutoff) \
        .execute()

    notifications = []
    for row in recent.data:
        client_result = supabase.table("users").select("name").eq("id", row["client_id"]).execute()
        client_name = client_result.data[0]["name"] if client_result.data else "A client"
        notifications.append({"client_name": client_name, "task": row["title"]})

    return JSONResponse({"notifications": notifications})


@app.get("/client/{client_id}", response_class=HTMLResponse)
async def client_detail(request: Request, client_id: str):
    coach_id = request.cookies.get("user_id")
    if not coach_id:
        return RedirectResponse(url="/coach-login", status_code=303)

    client_result = supabase.table("users").select("*").eq("id", client_id).execute()
    if not client_result.data:
        return RedirectResponse(url="/dashboard", status_code=303)
    client = client_result.data[0]

    tasks_result = supabase.table("assigned_tasks").select("*").eq("client_id", client_id).order("due_date").execute()
    tasks = tasks_result.data

    steps_today = None
    if client.get("google_access_token"):
        steps_today = fetch_google_steps(client["google_access_token"])

    return templates.TemplateResponse(request, "client_detail.html", {
        "client": client,
        "tasks": tasks,
        "steps_today": steps_today
    })


@app.post("/client/{client_id}/assign")
async def assign_task(request: Request, client_id: str, title: str = Form(...), due_date: str = Form(...), notes: str = Form("")):
    coach_id = request.cookies.get("user_id")
    supabase.table("assigned_tasks").insert({
        "coach_id": coach_id,
        "client_id": client_id,
        "title": title,
        "due_date": due_date,
        "notes": notes
    }).execute()
    return RedirectResponse(url=f"/client/{client_id}", status_code=303)


@app.get("/client-dashboard", response_class=HTMLResponse)
async def client_dashboard(request: Request):
    client_id = request.cookies.get("user_id")
    if not client_id:
        return RedirectResponse(url="/client-login", status_code=303)

    result = supabase.table("users").select("*").eq("id", client_id).execute()
    client = result.data[0] if result.data else None

    tasks = []
    if client:
        tasks_result = supabase.table("assigned_tasks").select("*").eq("client_id", client_id).order("due_date").execute()
        tasks = tasks_result.data

    steps_today = None
    if client and client.get("google_access_token"):
        steps_today = fetch_google_steps(client["google_access_token"])

    return templates.TemplateResponse(request, "client_dashboard.html", {
        "client": client,
        "tasks": tasks,
        "steps_today": steps_today
    })


@app.post("/task/{task_id}/complete")
async def complete_task(task_id: str):
    supabase.table("assigned_tasks").update({
        "status": "completed",
        "completed_at": datetime.datetime.utcnow().isoformat()
    }).eq("id", task_id).execute()
    return RedirectResponse(url="/client-dashboard", status_code=303)


@app.get("/auth/google/login")
async def google_login(request: Request):
    client_id = request.cookies.get("user_id")
    if not client_id:
        return RedirectResponse(url="/client-login", status_code=303)

    flow = Flow.from_client_config(CLIENT_CONFIG, scopes=SCOPES, redirect_uri=REDIRECT_URI)
    auth_url, state = flow.authorization_url(access_type="offline", prompt="consent")

    response = RedirectResponse(url=auth_url)
    response.set_cookie(key="oauth_state", value=state, httponly=True, max_age=600)
    response.set_cookie(key="oauth_code_verifier", value=flow.code_verifier, httponly=True, max_age=600)
    return response


@app.get("/auth/google/callback")
async def google_callback(request: Request, code: str = None):
    client_id = request.cookies.get("user_id")
    if not client_id or not code:
        return RedirectResponse(url="/client-login", status_code=303)

    code_verifier = request.cookies.get("oauth_code_verifier")

    flow = Flow.from_client_config(CLIENT_CONFIG, scopes=SCOPES, redirect_uri=REDIRECT_URI)
    flow.code_verifier = code_verifier
    flow.fetch_token(code=code)
    credentials = flow.credentials

    supabase.table("users").update({
        "google_access_token": credentials.token,
        "google_refresh_token": credentials.refresh_token
    }).eq("id", client_id).execute()

    response = RedirectResponse(url="/client-dashboard", status_code=303)
    response.delete_cookie("oauth_code_verifier")
    return response


@app.get("/logout")
async def logout():
    response = RedirectResponse(url="/", status_code=303)
    response.delete_cookie("user_id")
    return response
