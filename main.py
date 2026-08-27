from fastapi import FastAPI, Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import os
import uuid
import json
import datetime
from collections import defaultdict
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

UNIT_MAP = {
    "pushup": "reps",
    "jump": "reps",
    "running": "meters",
    "walking": "steps",
    "other": "reps",
}

# Streak milestones (days) that unlock a badge on the client dashboard
BADGE_MILESTONES = [3, 7, 14, 30, 60, 100]

# Where proof photos/videos get saved. Served back out via the /static mount below.
UPLOAD_DIR = "static/uploads/proofs"
os.makedirs(UPLOAD_DIR, exist_ok=True)

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


def fetch_google_steps(access_token: str, refresh_token: str):
    try:
        creds = Credentials(
            token=access_token,
            refresh_token=refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=GOOGLE_CLIENT_ID,
            client_secret=GOOGLE_CLIENT_SECRET,
        )
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
    except Exception as e:
        print("STEPS FETCH ERROR:", repr(e))
        return None


def compute_client_stats(client_id: str):
    """Aggregates a client's assigned_tasks + exercise_logs into stats for the
    progress page: completed/missed/pending counts, a 14-day activity time
    series, log counts by exercise type, and current/longest streaks."""

    tasks_result = supabase.table("assigned_tasks").select("*").eq("client_id", client_id).execute()
    tasks = tasks_result.data or []

    logs_result = supabase.table("exercise_logs").select("*").eq("client_id", client_id).order("created_at").execute()
    logs = logs_result.data or []

    today = datetime.date.today()

    # --- completed vs missed vs pending tasks ---
    completed_count = 0
    missed_count = 0
    pending_count = 0
    for t in tasks:
        if t.get("status") == "completed":
            completed_count += 1
        else:
            due = None
            try:
                due = datetime.date.fromisoformat(t["due_date"])
            except Exception:
                pass
            if due and due < today:
                missed_count += 1
            else:
                pending_count += 1

    # --- daily activity totals (last 14 days) + counts by type ---
    daily_totals = defaultdict(float)
    type_counts = defaultdict(int)
    log_dates = set()

    for log in logs:
        created = log.get("created_at")
        if not created:
            continue
        try:
            log_date = datetime.datetime.fromisoformat(created.replace("Z", "+00:00")).date()
        except Exception:
            continue
        log_dates.add(log_date)
        daily_totals[log_date] += log.get("value") or 0
        etype = log.get("exercise_type") or "other"
        type_counts[etype] += 1

    day_labels = []
    day_values = []
    for i in range(13, -1, -1):
        d = today - datetime.timedelta(days=i)
        day_labels.append(d.strftime("%b %d"))
        day_values.append(daily_totals.get(d, 0))

    # --- streaks (consecutive calendar days with at least one log) ---
    def calc_streaks(dates_set):
        if not dates_set:
            return 0, 0
        sorted_dates = sorted(dates_set)
        longest = 1
        run = 1
        for i in range(1, len(sorted_dates)):
            if (sorted_dates[i] - sorted_dates[i - 1]).days == 1:
                run += 1
                longest = max(longest, run)
            else:
                run = 1

        current = 0
        # allow "grace day": streak still counts if yesterday had a log
        # even if today hasn't been logged yet
        cursor = today if today in dates_set else (today - datetime.timedelta(days=1))
        if cursor in dates_set:
            while cursor in dates_set:
                current += 1
                cursor -= datetime.timedelta(days=1)
        return current, longest

    current_streak, longest_streak = calc_streaks(log_dates)

    return {
        "completed_count": completed_count,
        "missed_count": missed_count,
        "pending_count": pending_count,
        "day_labels": day_labels,
        "day_values": day_values,
        "type_labels": list(type_counts.keys()),
        "type_values": list(type_counts.values()),
        "current_streak": current_streak,
        "longest_streak": longest_streak,
    }


def get_coach_id_for_client(client_id: str):
    link = supabase.table("coach_clients").select("coach_id").eq("client_id", client_id).execute()
    return link.data[0]["coach_id"] if link.data else None


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

    logs_result = supabase.table("exercise_logs").select("*").eq("client_id", client_id).order("created_at", desc=True).limit(20).execute()
    logs = logs_result.data

    steps_today = None
    if client.get("google_access_token") and client.get("google_refresh_token"):
        steps_today = fetch_google_steps(client["google_access_token"], client["google_refresh_token"])

    return templates.TemplateResponse(request, "client_detail.html", {
        "client": client,
        "tasks": tasks,
        "logs": logs,
        "steps_today": steps_today
    })


@app.get("/client/{client_id}/stats", response_class=HTMLResponse)
async def client_stats_page(request: Request, client_id: str):
    coach_id = request.cookies.get("user_id")
    if not coach_id:
        return RedirectResponse(url="/coach-login", status_code=303)

    # confirm this coach actually owns this client before showing their data
    link_check = supabase.table("coach_clients").select("client_id") \
        .eq("coach_id", coach_id).eq("client_id", client_id).execute()
    if not link_check.data:
        return RedirectResponse(url="/dashboard", status_code=303)

    client_result = supabase.table("users").select("*").eq("id", client_id).execute()
    if not client_result.data:
        return RedirectResponse(url="/dashboard", status_code=303)
    client = client_result.data[0]

    stats = compute_client_stats(client_id)

    return templates.TemplateResponse(request, "client_stats.html", {
        "client": client,
        "stats": stats,
        "stats_json": json.dumps(stats),
        "back_url": f"/client/{client_id}",
        "is_coach_view": True,
    })


@app.get("/client/{client_id}/messages", response_class=HTMLResponse)
async def client_messages_page(request: Request, client_id: str):
    coach_id = request.cookies.get("user_id")
    if not coach_id:
        return RedirectResponse(url="/coach-login", status_code=303)

    link_check = supabase.table("coach_clients").select("client_id") \
        .eq("coach_id", coach_id).eq("client_id", client_id).execute()
    if not link_check.data:
        return RedirectResponse(url="/dashboard", status_code=303)

    client_result = supabase.table("users").select("*").eq("id", client_id).execute()
    if not client_result.data:
        return RedirectResponse(url="/dashboard", status_code=303)
    client = client_result.data[0]

    messages_result = supabase.table("messages").select("*") \
        .eq("coach_id", coach_id).eq("client_id", client_id).order("created_at").execute()

    return templates.TemplateResponse(request, "messages.html", {
        "other_name": client["name"],
        "messages": messages_result.data or [],
        "is_coach_view": True,
        "has_coach": True,
        "current_user_id": coach_id,
        "send_url": f"/client/{client_id}/messages/send",
        "poll_client_id": client_id,
        "back_url": f"/client/{client_id}",
    })


@app.post("/client/{client_id}/messages/send")
async def send_message_as_coach(request: Request, client_id: str, body: str = Form(...)):
    coach_id = request.cookies.get("user_id")
    if not coach_id:
        return RedirectResponse(url="/coach-login", status_code=303)

    link_check = supabase.table("coach_clients").select("client_id") \
        .eq("coach_id", coach_id).eq("client_id", client_id).execute()
    if not link_check.data:
        return RedirectResponse(url="/dashboard", status_code=303)

    if body.strip():
        supabase.table("messages").insert({
            "coach_id": coach_id,
            "client_id": client_id,
            "sender_id": coach_id,
            "body": body.strip(),
        }).execute()

    return RedirectResponse(url=f"/client/{client_id}/messages", status_code=303)


@app.get("/my-messages", response_class=HTMLResponse)
async def my_messages_page(request: Request):
    client_id = request.cookies.get("user_id")
    if not client_id:
        return RedirectResponse(url="/client-login", status_code=303)

    coach_id = get_coach_id_for_client(client_id)

    other_name = "No coach yet"
    messages = []
    if coach_id:
        coach_result = supabase.table("users").select("*").eq("id", coach_id).execute()
        if coach_result.data:
            other_name = coach_result.data[0]["name"]
        messages_result = supabase.table("messages").select("*") \
            .eq("coach_id", coach_id).eq("client_id", client_id).order("created_at").execute()
        messages = messages_result.data or []

    return templates.TemplateResponse(request, "messages.html", {
        "other_name": other_name,
        "messages": messages,
        "is_coach_view": False,
        "has_coach": bool(coach_id),
        "current_user_id": client_id,
        "send_url": "/my-messages/send",
        "poll_client_id": client_id,
        "back_url": "/client-dashboard",
    })


@app.post("/my-messages/send")
async def send_message_as_client(request: Request, body: str = Form(...)):
    client_id = request.cookies.get("user_id")
    if not client_id:
        return RedirectResponse(url="/client-login", status_code=303)

    coach_id = get_coach_id_for_client(client_id)
    if coach_id and body.strip():
        supabase.table("messages").insert({
            "coach_id": coach_id,
            "client_id": client_id,
            "sender_id": client_id,
            "body": body.strip(),
        }).execute()

    return RedirectResponse(url="/my-messages", status_code=303)


@app.get("/api/messages/{client_id}")
async def api_get_messages(request: Request, client_id: str):
    viewer_id = request.cookies.get("user_id")
    if not viewer_id:
        return JSONResponse({"messages": []}, status_code=401)

    authorized = viewer_id == client_id
    if not authorized:
        link_check = supabase.table("coach_clients").select("client_id") \
            .eq("coach_id", viewer_id).eq("client_id", client_id).execute()
        authorized = bool(link_check.data)

    if not authorized:
        return JSONResponse({"messages": []}, status_code=403)

    coach_id = get_coach_id_for_client(client_id)
    if not coach_id:
        return JSONResponse({"messages": []})

    query = supabase.table("messages").select("*").eq("coach_id", coach_id).eq("client_id", client_id)
    after = request.query_params.get("after")
    if after:
        query = query.gt("created_at", after)
    result = query.order("created_at").execute()

    return JSONResponse({"messages": result.data or []})


@app.post("/client/{client_id}/assign")
async def assign_task(
    request: Request,
    client_id: str,
    title: str = Form(...),
    due_date: str = Form(...),
    notes: str = Form(""),
    task_type: str = Form("other"),
    target_value: int = Form(None),
):
    coach_id = request.cookies.get("user_id")
    unit = UNIT_MAP.get(task_type, "reps")
    supabase.table("assigned_tasks").insert({
        "coach_id": coach_id,
        "client_id": client_id,
        "title": title,
        "due_date": due_date,
        "notes": notes,
        "task_type": task_type,
        "unit": unit,
        "target_value": target_value,
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
    if client and client.get("google_access_token") and client.get("google_refresh_token"):
        steps_today = fetch_google_steps(client["google_access_token"], client["google_refresh_token"])

    # Streak + badges, computed from the same exercise_logs data /my-stats uses
    current_streak = 0
    longest_streak = 0
    earned_badges = []
    if client:
        streak_stats = compute_client_stats(client_id)
        current_streak = streak_stats["current_streak"]
        longest_streak = streak_stats["longest_streak"]
        earned_badges = [m for m in BADGE_MILESTONES if longest_streak >= m]

    return templates.TemplateResponse(request, "client_dashboard.html", {
        "client": client,
        "tasks": tasks,
        "steps_today": steps_today,
        "current_streak": current_streak,
        "longest_streak": longest_streak,
        "earned_badges": earned_badges,
    })


@app.get("/my-stats", response_class=HTMLResponse)
async def my_stats_page(request: Request):
    client_id = request.cookies.get("user_id")
    if not client_id:
        return RedirectResponse(url="/client-login", status_code=303)

    client_result = supabase.table("users").select("*").eq("id", client_id).execute()
    if not client_result.data:
        return RedirectResponse(url="/client-login", status_code=303)
    client = client_result.data[0]

    stats = compute_client_stats(client_id)

    return templates.TemplateResponse(request, "client_stats.html", {
        "client": client,
        "stats": stats,
        "stats_json": json.dumps(stats),
        "back_url": "/client-dashboard",
        "is_coach_view": False,
    })


@app.post("/task/{task_id}/log")
async def log_progress(
    request: Request,
    task_id: str,
    action: str = Form("log"),
    value: int = Form(None),
    proof: UploadFile = File(None),
):
    client_id = request.cookies.get("user_id")
    if not client_id:
        return RedirectResponse(url="/client-login", status_code=303)

    task_result = supabase.table("assigned_tasks").select("*").eq("id", task_id).execute()
    if not task_result.data:
        return RedirectResponse(url="/client-dashboard", status_code=303)
    task = task_result.data[0]

    # Walking tasks auto-fill from Google Fit instead of requiring manual entry
    if task["task_type"] == "walking":
        client_result = supabase.table("users").select("*").eq("id", client_id).execute()
        client = client_result.data[0] if client_result.data else None
        if client and client.get("google_access_token") and client.get("google_refresh_token"):
            fetched = fetch_google_steps(client["google_access_token"], client["google_refresh_token"])
            if fetched is not None:
                value = fetched

    # Save the proof file if one was attached, regardless of log vs complete
    proof_url = None
    if proof and proof.filename:
        ext = os.path.splitext(proof.filename)[1]
        filename = f"{uuid.uuid4().hex}{ext}"
        filepath = os.path.join(UPLOAD_DIR, filename)
        with open(filepath, "wb") as f:
            f.write(await proof.read())
        proof_url = f"/{filepath}"

    # Record a log entry and update the task's current_value whenever a number came in.
    # This is what makes the coach's dashboard able to show progress at all.
    if value is not None:
        supabase.table("exercise_logs").insert({
            "task_id": task_id,
            "client_id": client_id,
            "exercise_type": task["task_type"],
            "value": value,
            "unit": task["unit"],
        }).execute()

        update_data = {"current_value": value}
        if proof_url:
            update_data["proof_url"] = proof_url
        supabase.table("assigned_tasks").update(update_data).eq("id", task_id).execute()
    elif proof_url:
        # No numeric value this time (e.g. just attaching proof on mark-complete), still save the file
        supabase.table("assigned_tasks").update({"proof_url": proof_url}).eq("id", task_id).execute()

    # Complete if the client explicitly clicked "Mark complete", or the target was hit
    should_complete = action == "complete" or (
        task.get("target_value") and value is not None and value >= task["target_value"]
    )
    if should_complete:
        supabase.table("assigned_tasks").update({
            "status": "completed",
            "completed_at": datetime.datetime.utcnow().isoformat(),
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