from fastapi import APIRouter, Request, HTTPException, Depends
from fastapi.security import HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from app.database.connection import files_col, users_col, logs_col
from app.admin.auth import require_panel_admin
import psutil
import time

router = APIRouter(prefix="/admin")
templates = Jinja2Templates(directory=str(__import__("pathlib").Path(__file__).resolve().parents[1] / "templates"))

async def is_admin(_: bool = Depends(require_panel_admin)):
    return True

@router.get("/")
async def admin_dashboard(request: Request, _: bool = Depends(is_admin)):
        
    total_users = await users_col.count_documents({})
    total_files = await files_col.count_documents({})
    
    # System Stats
    cpu_usage = psutil.cpu_percent()
    ram_usage = psutil.virtual_memory().percent
    
    return templates.TemplateResponse(
        request=request,
        name="admin/dashboard.html",
        context={
            "request": request,
            "total_users": total_users,
            "total_files": total_files,
            "cpu": cpu_usage,
            "ram": ram_usage,
        },
    )

@router.get("/users")
async def admin_users(request: Request, _: bool = Depends(is_admin)):
    users = await users_col.find().to_list(100)
    return templates.TemplateResponse(request=request, name="admin/users.html", context={"request": request, "users": users})

@router.get("/files")
async def admin_files(request: Request, q: str = None, _: bool = Depends(is_admin)):
    query = {}
    if q:
        query = {"filename": {"$regex": __import__("re").escape(q), "$options": "i"}}
    
    files = await files_col.find(query).sort("created_at", -1).to_list(100)
    return templates.TemplateResponse(request=request, name="admin/files.html", context={"request": request, "files": files, "query": q})

@router.post("/files/delete/{short_code}")
async def delete_file(request: Request, short_code: str, _: bool = Depends(is_admin)):
        
    await files_col.delete_one({"short_code": short_code})
    return {"status": "success"}
