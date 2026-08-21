from zoneinfo import ZoneInfo
from datetime import datetime, timezone, timedelta
from calendar import monthrange
import secrets, random
from fastapi import FastAPI, Depends, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, or_, func, text
from sqlalchemy.orm import Session
from db import Base, engine, get_db
from models import Academy,AdminCredential,Student,StudentAcademy,AttendanceEvent,ParentDevice,ParentLink,Notice
from security import hash_password,verify_password,token,read_token
from auth_adapter import verify_license_key,AuthUnavailable
from push import send_push
KST=ZoneInfo("Asia/Seoul"); LOCKOUT_SECONDS=600

def now_kst():
    return datetime.now(KST)

def to_utc(dt: datetime):
    if dt.tzinfo is None:
        # PC/iOS에서 timezone 없이 보낸 값은 한국시간으로 간주
        dt = dt.replace(tzinfo=KST)
    return dt.astimezone(timezone.utc)

def to_kst(dt: datetime):
    if dt.tzinfo is None:
        # DB의 timezone 없는 값은 UTC로 간주
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(KST)


