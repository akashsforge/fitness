from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import os
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

url = os.environ.get("SUPABASE_URL")
key = os.environ.get("SUPABASE_KEY")
supabase = create_client(url, key)

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse(request, "login.html", {})


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
        return RedirectResponse(url="/", status_code=303)
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

        return RedirectResponse(url="/", status_code=303)
    except Exception as e:
        return HTMLResponse(f"Signup failed: {str(e)}")


@app.post("/login")
async def login(email: str = Form(...), password: str = Form(...)):
    try:
        auth_response = supabase.auth.sign_in_with_password({"email": email, "password": password})
        user_id = auth_response.user.id

        response = RedirectResponse(url="/dashboard", status_code=303)
        response.set_cookie(key="user_id", value=user_id, httponly=True, max_age=3600)
        return response
    except Exception as e:
        return HTMLResponse(f"Login failed: {str(e)}")


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    coach_id = request.cookies.get("user_id")
    if not coach_id:
        return RedirectResponse(url="/", status_code=303)

    links = supabase.table("coach_clients").select("client_id").eq("coach_id", coach_id).execute()
    client_ids = [row["client_id"] for row in links.data]

    clients = []
    if client_ids:
        result = supabase.table("users").select("*").in_("id", client_ids).execute()
        clients = result.data

    return templates.TemplateResponse(request, "dashboard.html", {"clients": clients})


@app.get("/logout")
async def logout():
    response = RedirectResponse(url="/", status_code=303)
    response.delete_cookie("user_id")
    return response