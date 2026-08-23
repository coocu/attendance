from zoneinfo import ZoneInfo
from datetime import datetime, timezone, timedelta
from calendar import monthrange
import secrets, random
from fastapi import FastAPI, Depends, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, or_, and_, func, text
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
        # timezone 없이 전달된 값은 한국시간으로 해석
        dt = dt.replace(tzinfo=KST)
    return dt.astimezone(timezone.utc)

def to_kst(dt: datetime):
    if dt.tzinfo is None:
        # DB의 naive datetime은 UTC로 해석
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(KST)

def hhmm(v:str,label:str):
    try:
        h,m=[int(x) for x in v.split(":",1)]
        if not (0<=h<=23 and 0<=m<=59): raise ValueError
    except Exception:
        raise HTTPException(400,f"{label} 형식이 올바르지 않습니다.")
    return h,m

def academy_business_start_utc(a:Academy,when_utc:datetime):
    local=to_kst(when_utc)
    if getattr(a,"is_24_hours",True):
        return local.replace(hour=0,minute=0,second=0,microsecond=0).astimezone(timezone.utc)
    h,m=hhmm(getattr(a,"open_time","09:00"),"영업 시작시간")
    start=local.replace(hour=h,minute=m,second=0,microsecond=0)
    if local < start:
        start-=timedelta(days=1)
    return start.astimezone(timezone.utc)
app=FastAPI(title="CodeNote Attendance V3 API",version="3.0.0")
app.add_middleware(CORSMiddleware,allow_origins=["*"],allow_methods=["*"],allow_headers=["*"])
@app.on_event("startup")
def startup():
    Base.metadata.create_all(engine)
    # 기존 DB 유지 + NFC 미등록 학생 허용
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE students ALTER COLUMN nfc_token DROP NOT NULL"))
        conn.execute(text("ALTER TABLE students ALTER COLUMN nfc_active SET DEFAULT FALSE"))
        conn.execute(text("ALTER TABLE students ALTER COLUMN phone_last4 TYPE VARCHAR(11)"))
        conn.execute(text("ALTER TABLE academies ADD COLUMN IF NOT EXISTS is_24_hours BOOLEAN NOT NULL DEFAULT TRUE"))
        conn.execute(text("ALTER TABLE academies ADD COLUMN IF NOT EXISTS open_time VARCHAR(5) NOT NULL DEFAULT '09:00'"))
        conn.execute(text("ALTER TABLE academies ADD COLUMN IF NOT EXISTS close_time VARCHAR(5) NOT NULL DEFAULT '20:00'"))
    with next(get_db()) as db:
        for t in ("regular","emergency"):
            if db.get(Notice,t) is None: db.add(Notice(notice_type=t))
        db.commit()
def digits(v,n,label):
    v=v.strip()
    if len(v)!=n or not v.isdigit(): raise HTTPException(400,f"{label}는 숫자 {n}자리여야 합니다.")
    return v
def bearer(a):
    if not a or not a.startswith("Bearer "): raise HTTPException(401,"로그인이 필요합니다.")
    return a[7:]
def admin_auth(authorization:str|None=Header(default=None)): return read_token(bearer(authorization),"admin",60*60*24*365)
def parent_auth(authorization:str|None=Header(default=None)): return read_token(bearer(authorization),"parent",60*60*24*365)
def active_academy(db,id):
    a=db.get(Academy,id)
    if not a or not a.is_active: raise HTTPException(404,"사용 가능한 학원이 아닙니다.")
    return a

def refresh_duplicate_codes(db:Session,academy_id:int,name:str,phone:str):
    rows=db.execute(select(StudentAcademy,Student).join(Student,Student.id==StudentAcademy.student_id).where(StudentAcademy.academy_id==academy_id,StudentAcademy.is_active.is_(True),Student.name==name,Student.phone_last4==phone)).all()
    if len(rows)<=1:
        for sa,_ in rows: sa.login_extra_code=None
    else:
        used=set()
        for sa,_ in rows:
            if not sa.login_extra_code:
                c=str(random.randint(100000,999999))
                while c in used: c=str(random.randint(100000,999999))
                sa.login_extra_code=c
            used.add(sa.login_extra_code)
    db.flush()

def sync_parent_family_links(db:Session,phone:str,device_id:int|None=None):
    # 동일 보호자 전화번호의 학생들은 한 보호자 기기에서 함께 조회되도록 연결합니다.
    family_rows=db.execute(
        select(StudentAcademy,Student)
        .join(Student,Student.id==StudentAcademy.student_id)
        .where(Student.phone_last4==phone,StudentAcademy.is_active.is_(True))
    ).all()
    if not family_rows: return
    if device_id is None:
        family_student_ids=[s.id for _,s in family_rows]
        device_ids=list(db.scalars(
            select(ParentLink.device_id)
            .where(ParentLink.student_id.in_(family_student_ids))
            .distinct()
        ).all())
    else:
        device_ids=[device_id]
    if not device_ids:
        return
    family_student_ids=list({student.id for _,student in family_rows})
    existing_keys=set(db.execute(
        select(ParentLink.device_id,ParentLink.student_id,ParentLink.academy_id)
        .where(ParentLink.device_id.in_(device_ids),ParentLink.student_id.in_(family_student_ids))
    ).all())
    for did in device_ids:
        for sa,student in family_rows:
            key=(did,student.id,sa.academy_id)
            if key not in existing_keys:
                db.add(ParentLink(device_id=did,student_id=student.id,academy_id=sa.academy_id))
                existing_keys.add(key)
    db.flush()

class KeyReq(BaseModel): license_key:str
class AcademyCreate(BaseModel): registration_token:str; name:str; region:str; district:str; admin_password:str=Field(min_length=4); recovery_name:str; recovery_phone_last4:str
class AdminLoginReq(BaseModel): academy_id:int; password:str
class ParentLoginReq(BaseModel): academy_id:int; name:str; phone_last4:str; extra_code:str|None=None; installation_id:str; platform:str="android"; push_token:str|None=None
class PushTokenReq(BaseModel): push_token:str
class ChangePw(BaseModel): current_password:str; new_password:str=Field(min_length=4)
class RecoveryVerify(BaseModel): academy_id:int; recovery_name:str; recovery_phone_last4:str; license_key:str
class ResetPw(BaseModel): recovery_token:str; new_password:str=Field(min_length=4)
class NewStudentNfc(BaseModel): name:str; phone_last4:str; attendance_pin:str; memo:str=""; nfc_token:str
class NewStudentNoNfc(BaseModel): name:str; phone_last4:str; attendance_pin:str; memo:str=""
class NfcConfirm(BaseModel): nfc_registration_token:str
class AttachExisting(BaseModel): nfc_token:str; attendance_pin:str; memo:str=""
class EditStudent(BaseModel): name:str; phone_last4:str; attendance_pin:str; memo:str=""; confirm_global:bool=False
class NfcLookup(BaseModel): nfc_token:str
class StudentIdentityReq(BaseModel): name:str; phone_last4:str
class LostNfcPrepareReq(BaseModel): name:str; phone_last4:str; attendance_pin:str; memo:str=""
class NfcReplace(BaseModel): new_nfc_token:str
class AttendanceReq(BaseModel): nfc_token:str|None=None; attendance_pin:str|None=None
class ManualAttendanceReq(BaseModel): student_id:int; event_type:str; occurred_at:datetime
class AcademyHoursReq(BaseModel): is_24_hours:bool; open_time:str="09:00"; close_time:str="20:00"
class NoticeWrite(BaseModel): management_token:str; notice_type:str; content:str; is_active:bool

# 코드노트 공지 앱(기존 MusyncNotice) 호환용 요청 모델
class NoticeManagementSaveReq(BaseModel):
    license_key:str
    notice_type:str
    content:str

class NoticeManagementToggleReq(BaseModel):
    license_key:str
    notice_type:str
    enabled:bool

class NoticeManagementApplyReq(BaseModel):
    license_key:str
    notice_type:str
    content:str
    enabled:bool
class AcademyUpdate(BaseModel): management_token:str; academy_id:int; name:str|None=None; region:str|None=None; district:str|None=None; is_active:bool|None=None
class ManageReq(BaseModel): management_token:str; academy_id:int

@app.get("/health")
def health(): return {"ok":True,"service":"codenote-attendance-v3"}
@app.get("/api/v3/notices")
def notices(db:Session=Depends(get_db)): return [{"type":n.notice_type,"content":n.content,"is_active":n.is_active} for n in db.scalars(select(Notice)).all()]

# -----------------------------------------------------------------------------
# 코드노트 공지 앱(MusyncNotice) 기존 API 호환
# 앱 수정 없이 /api/notice-management/* 경로를 그대로 사용할 수 있게 유지한다.
# -----------------------------------------------------------------------------
def _notice_state(db:Session):
    rows={n.notice_type:n for n in db.scalars(select(Notice)).all()}
    def item(kind:str):
        n=rows.get(kind)
        return {"enabled":bool(n.is_active) if n else False,"content":n.content if n else ""}
    return {"regular":item("regular"),"emergency":item("emergency")}

async def _verify_notice_management_key(license_key:str):
    key=license_key.strip()
    if "kyh" not in key.lower():
        raise HTTPException(401,"공지관리 권한이 없는 인증키입니다.")
    try:
        ok=await verify_license_key(key)
    except AuthUnavailable as exc:
        raise HTTPException(503,str(exc)) from exc
    if not ok:
        raise HTTPException(401,"인증키를 확인해 주세요.")

def _notice_mutation_response(n:Notice):
    return {"notice_type":n.notice_type,"enabled":bool(n.is_active),"content":n.content}

def _get_or_create_notice(db:Session,notice_type:str):
    if notice_type not in {"regular","emergency"}:
        raise HTTPException(400,"공지 종류가 올바르지 않습니다.")
    return db.get(Notice,notice_type) or Notice(notice_type=notice_type)

@app.get("/api/notice-management/state")
def notice_management_state(db:Session=Depends(get_db)):
    return _notice_state(db)

@app.post("/api/notice-management/save")
async def notice_management_save(r:NoticeManagementSaveReq,db:Session=Depends(get_db)):
    await _verify_notice_management_key(r.license_key)
    n=_get_or_create_notice(db,r.notice_type)
    n.content=r.content.strip()
    n.updated_at=now_kst().astimezone(timezone.utc)
    db.add(n); db.commit(); db.refresh(n)
    return _notice_mutation_response(n)

@app.post("/api/notice-management/toggle")
async def notice_management_toggle(r:NoticeManagementToggleReq,db:Session=Depends(get_db)):
    await _verify_notice_management_key(r.license_key)
    n=_get_or_create_notice(db,r.notice_type)
    n.is_active=r.enabled
    n.updated_at=now_kst().astimezone(timezone.utc)
    db.add(n); db.commit(); db.refresh(n)
    return _notice_mutation_response(n)

@app.post("/api/notice-management/apply")
async def notice_management_apply(r:NoticeManagementApplyReq,db:Session=Depends(get_db)):
    await _verify_notice_management_key(r.license_key)
    n=_get_or_create_notice(db,r.notice_type)
    n.content=r.content.strip()
    n.is_active=r.enabled
    n.updated_at=now_kst().astimezone(timezone.utc)
    db.add(n); db.commit(); db.refresh(n)
    return _notice_mutation_response(n)
@app.get("/api/v3/regions")
def regions(db:Session=Depends(get_db)): return list(db.scalars(select(Academy.region).where(Academy.is_active.is_(True)).distinct().order_by(Academy.region)).all())
@app.get("/api/v3/districts")
def districts(region:str,db:Session=Depends(get_db)): return list(db.scalars(select(Academy.district).where(Academy.is_active.is_(True),Academy.region==region).distinct().order_by(Academy.district)).all())
@app.get("/api/v3/academies")
def academies(region:str,district:str,q:str="",db:Session=Depends(get_db)):
    s=select(Academy).where(Academy.is_active.is_(True),Academy.region==region,Academy.district==district)
    if q.strip(): s=s.where(Academy.name.ilike(f"%{q.strip()}%"))
    return [{"id":a.id,"name":a.name,"region":a.region,"district":a.district} for a in db.scalars(s.order_by(Academy.name).limit(300)).all()]

@app.get("/api/v3/academies/search")
def academy_search(q:str="",db:Session=Depends(get_db)):
    q=q.strip()
    if not q: return []
    stmt=select(Academy).where(Academy.is_active.is_(True),Academy.name.ilike(f"%{q}%")).order_by(Academy.region,Academy.district,Academy.name).limit(50)
    return [{"id":a.id,"name":a.name,"region":a.region,"district":a.district} for a in db.scalars(stmt).all()]


ADMIN_WEB_HTML = r"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>CodeNote 출석관리</title>
<style>
:root{--blue:#4169ef;--bg:#f5f5fa;--card:#fff;--line:#e5e7eb;--text:#111827;--muted:#6b7280}
*{box-sizing:border-box}body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Apple SD Gothic Neo","Noto Sans KR",sans-serif;background:var(--bg);color:var(--text)}
.wrap{max-width:1100px;margin:0 auto;padding:34px 18px}.head{display:flex;align-items:center;gap:14px;margin-bottom:24px}
.logo{width:54px;height:54px;border-radius:16px;background:#e8edff;color:var(--blue);display:grid;place-items:center;font-size:28px;font-weight:800}
h1{font-size:26px;margin:0}.sub{color:var(--muted);margin-top:4px}
.card{background:var(--card);border:1px solid var(--line);border-radius:22px;padding:22px;margin-bottom:18px;box-shadow:0 8px 28px rgba(17,24,39,.04)}
.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.grid3{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}
input,select,textarea,button{font:inherit}input,select,textarea{width:100%;padding:13px 14px;border:1px solid #d7dbe5;border-radius:12px;background:#fff}
textarea{min-height:80px;resize:vertical}button{border:0;border-radius:12px;padding:13px 17px;cursor:pointer}
.primary{background:var(--blue);color:#fff;font-weight:700}.secondary{background:#eef2ff;color:var(--blue);font-weight:700}.danger{background:#fff0f0;color:#c62828}
.row{display:flex;gap:10px;align-items:center}.between{display:flex;justify-content:space-between;align-items:center;gap:12px}
.hidden{display:none!important}.msg{margin-top:10px;color:#c62828;font-size:14px}.ok{color:#177245}
.tabs{display:flex;gap:8px;margin:0 0 18px}.tab{background:#e9ebf2;color:#4b5563}.tab.on{background:var(--blue);color:#fff}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:11px 9px;border-bottom:1px solid #eee;font-size:14px}th{color:#6b7280}
.pill{display:inline-block;padding:4px 9px;border-radius:999px;background:#eef2ff;color:#395ad7;font-size:12px}
.small{font-size:13px;color:var(--muted)}.section-title{font-size:18px;font-weight:800;margin:0 0 14px}
.att-calendar{margin-top:16px;border:1px solid var(--line);border-radius:16px;overflow:hidden;background:#fff}.att-week,.att-days{display:grid;grid-template-columns:repeat(7,minmax(0,1fr))}.att-week div{padding:9px 4px;text-align:center;font-size:12px;font-weight:700;color:var(--muted);background:#fafbff;border-bottom:1px solid var(--line)}.att-day{min-height:72px;padding:7px;border:0;border-right:1px solid #f0f1f5;border-bottom:1px solid #f0f1f5;border-radius:0;background:#fff;color:var(--text);text-align:left}.att-day.empty{background:#fafafa;cursor:default}.att-day.selected{background:#eef2ff;box-shadow:inset 0 0 0 2px var(--blue)}.att-day.today .att-num{color:var(--blue);font-weight:900}.att-num{font-weight:700}.att-count{display:block;margin-top:7px;font-size:11px;color:var(--blue);font-weight:700}.att-selected-title{font-size:15px;font-weight:800;margin:16px 0 8px}
@media(max-width:720px){.grid,.grid3{grid-template-columns:1fr}.between{align-items:flex-start;flex-direction:column}.tablewrap{overflow:auto}.att-day{min-height:60px;padding:5px}.att-count{font-size:10px}}
</style>
</head>
<body>
<div class="wrap">
  <div class="head"><div class="logo">C</div><div><h1>CodeNote 출석관리</h1><div class="sub">PC 관리자</div></div></div>

  <div id="loginCard" class="card">
    <div class="section-title">관리자 로그인</div>

    <input id="academyQ" placeholder="학원 이름 검색" autocomplete="off">
    <div id="academySearchResults" class="hidden" style="margin-top:8px;border:1px solid var(--line);border-radius:12px;background:#fff;overflow:hidden"></div>

    <div style="height:12px"></div>
    <div class="grid3">
      <select id="region"><option value="">지역</option></select>
      <select id="district"><option value="">시·군·구</option></select>
      <select id="academy"><option value="">학원</option></select>
    </div>
    <div style="height:12px"></div>
    <input id="password" type="password" placeholder="관리자 비밀번호">
    <div style="height:12px"></div>
    <button class="primary" onclick="login()">관리자 로그인</button>
    <button style="background:transparent;color:var(--blue);padding:10px 0 0" onclick="openForgot()">비밀번호를 잊으셨나요?</button>
    <button class="secondary" style="margin-top:10px" onclick="openAcademyManagement()">학원 관리</button>
    <div id="loginMsg" class="msg"></div>

    <div id="forgotBox" class="hidden" style="margin-top:18px;padding-top:18px;border-top:1px solid var(--line)">
      <div class="section-title">관리자 비밀번호 찾기</div>
      <div class="small" style="margin-bottom:10px">학원 등록 시 입력한 최초 관리자 성함과 전화번호 끝 4자리를 입력해주세요.</div>
      <div class="grid">
        <input id="recoveryName" placeholder="최초 관리자 성함">
        <input id="recoveryPhone" maxlength="4" inputmode="numeric" placeholder="전화번호 끝 4자리">
      </div>
      <div style="height:10px"></div>
      <input id="recoveryLicenseKey" type="password" placeholder="일반 인증키">
      <div class="small" style="margin-top:6px">서버에 등록된 유효한 일반 인증키를 입력해주세요.</div>
      <div style="height:10px"></div>
      <button class="secondary" onclick="verifyRecovery()">본인 확인</button>
      <div id="resetBox" class="hidden" style="margin-top:12px">
        <div class="grid">
          <input id="resetPassword" type="password" placeholder="비밀번호 재설정">
          <input id="resetPassword2" type="password" placeholder="재설정 확인">
        </div>
        <div style="height:10px"></div>
        <button class="primary" onclick="resetPasswordNow()">변경</button>
      </div>
      <div id="forgotMsg" class="msg"></div>
      <div style="height:10px"></div>
      <button class="secondary" onclick="backToLogin()">로그인 화면으로 돌아가기</button>
    </div>
  </div>

  <div id="admin" class="hidden">
    <div class="card between"><div><div id="academyTitle" class="section-title" style="margin:0"></div><div class="small">관리자 관리용 화면</div></div><button class="secondary" onclick="logout()">로그아웃</button></div>
    <div class="tabs">
      <button id="tabAcademy" class="tab on" onclick="showTab('academy')">학원관리</button>
      <button id="tabStudents" class="tab" onclick="showTab('students')">학생관리</button>
      <button id="tabAttendance" class="tab" onclick="showTab('attendance')">출석현황</button>
      <button id="tabPassword" class="tab" onclick="showTab('password')">비밀번호 변경</button>
    </div>

    <section id="academyPanel">
      <div class="card">
        <div class="section-title">학원관리</div>
        <div class="grid" style="grid-template-columns:repeat(4,minmax(0,1fr))">
          <div class="card" style="margin:0"><div class="small">등록 학생</div><div id="academyStatRegistered" style="font-size:28px;font-weight:800;margin-top:8px">-명</div></div>
          <div class="card" style="margin:0"><div class="small">현재 입실</div><div id="academyStatCurrent" style="font-size:28px;font-weight:800;margin-top:8px">-명</div></div>
          <div class="card" style="margin:0"><div class="small">오늘 입실</div><div id="academyStatIn" style="font-size:28px;font-weight:800;margin-top:8px">-명</div></div>
          <div class="card" style="margin:0"><div class="small">오늘 퇴실</div><div id="academyStatOut" style="font-size:28px;font-weight:800;margin-top:8px">-명</div></div>
        </div>
      </div>
      <div class="card">
        <div class="section-title">영업시간 설정</div>
        <label class="row" style="margin-bottom:14px"><input id="academy24Hours" type="checkbox" onchange="academyHoursChanged()" style="width:auto"><span>24시간 운영</span></label>
        <div id="academyHoursFields" class="grid">
          <div><div class="small" style="margin-bottom:6px">영업 시작시간</div><input id="academyOpenTime" type="time" value="09:00"></div>
          <div><div class="small" style="margin-bottom:6px">영업 종료시간</div><input id="academyCloseTime" type="time" value="20:00"></div>
        </div>
        <div style="height:12px"></div>
        <button class="primary" onclick="saveAcademySettings()">영업시간 저장</button>
        <div id="academySettingsMsg" class="msg"></div>
      </div>
    </section>

    <section id="studentsPanel" class="hidden">
      <div class="card">
        <div class="between"><div class="section-title">학생 등록</div><div class="small">NFC 카드가 없어도 먼저 등록할 수 있습니다. NFC는 학생 목록에서 나중에 등록/재등록할 수 있습니다.</div></div>
        <div class="grid3">
          <input id="studentName" placeholder="학생 이름">
          <input id="studentPhone" maxlength="11" inputmode="numeric" placeholder="보호자 전화번호 11자리 (01012345678)">
          <input id="studentPin" maxlength="4" inputmode="numeric" placeholder="출석번호 4자리">
          <input id="studentMemo" placeholder="관리자 메모">
        </div>
        <div style="height:10px"></div>
        <div class="row">
          <button class="primary" onclick="saveStudentNoNfc()">학생 등록</button>
          <button class="secondary" onclick="readExistingStudentNfc()">NFC로 학생 불러오기</button>
          <button id="cancelStudentNfcImport" class="secondary hidden" onclick="cancelExistingStudentNfc()">NFC 불러오기 취소</button>
        </div>
        <div id="studentFormMsg" class="msg"></div>
      </div>
      <div class="card">
        <div class="between"><div class="section-title">학생 목록</div><input id="studentQ" style="max-width:320px" placeholder="이름 / 전화번호 / 출석번호 검색" oninput="loadStudents()"></div>
        <div class="tablewrap"><table><thead><tr><th>이름</th><th>전화 뒤4</th><th>출석번호</th><th>메모</th><th>NFC</th><th></th></tr></thead><tbody id="studentsBody"></tbody></table></div>
      </div>
    </section>

    <section id="attendancePanel" class="hidden">
      <div class="card">
        <div class="between"><div class="section-title">월별 출석현황</div><div class="row"><input id="month" type="month" onchange="attendanceMonthChanged()"><input id="attQ" placeholder="이름/전화 검색"><button class="secondary" onclick="loadAttendance()">조회</button></div></div>
        <div id="attendanceCalendar" class="att-calendar"></div>
        <div id="attendanceSelectedTitle" class="att-selected-title"></div>
        <div class="tablewrap"><table><thead><tr><th>시간</th><th>학생</th><th>전화 뒤4</th><th>상태</th><th>방식</th><th></th></tr></thead><tbody id="attendanceBody"></tbody></table></div>
      </div>
    </section>

    <section id="passwordPanel" class="hidden">
      <div class="card">
        <div class="section-title">관리자 비밀번호 변경</div>
        <div class="grid"><input id="currentPw" type="password" placeholder="현재 비밀번호"><input id="newPw" type="password" placeholder="새 비밀번호"></div>
        <div style="height:12px"></div><button class="primary" onclick="changePassword()">변경</button><div id="pwMsg" class="msg"></div>
      </div>
    </section>
  </div>

  <div id="editStudentBox" class="hidden" style="position:fixed;inset:0;background:rgba(0,0,0,.35);z-index:74;padding:24px;display:flex;align-items:center;justify-content:center">
    <div class="card" style="width:min(520px,100%);margin:0">
      <div class="between">
        <div>
          <div class="section-title" style="margin:0">학생정보 수정</div>
          <div class="small">이름·전화번호 변경은 전체 서버의 동일 학생 정보에 반영됩니다.</div>
        </div>
        <button class="secondary" onclick="closeStudentEdit()">닫기</button>
      </div>

      <input id="editStudentId" type="hidden">
      <input id="editStudentOriginalName" type="hidden">
      <input id="editStudentOriginalPhone" type="hidden">

      <div style="height:14px"></div>
      <div class="grid">
        <div>
          <div class="small" style="margin-bottom:6px">학생 이름</div>
          <input id="editStudentName" placeholder="학생 이름">
        </div>
        <div>
          <div class="small" style="margin-bottom:6px">전화번호 뒷4자리</div>
          <input id="editStudentPhone" maxlength="11" inputmode="numeric" placeholder="보호자 전화번호 11자리 (01012345678)">
        </div>
      </div>

      <div style="height:10px"></div>
      <div class="grid">
        <div>
          <div class="small" style="margin-bottom:6px">출석번호 4자리</div>
          <input id="editStudentPin" maxlength="4" inputmode="numeric" placeholder="출석번호 4자리">
        </div>
        <div>
          <div class="small" style="margin-bottom:6px">관리자 메모</div>
          <input id="editStudentMemo" placeholder="관리자 메모">
        </div>
      </div>

      <div id="editStudentMsg" class="msg"></div>
      <div style="height:14px"></div>
      <button class="primary" style="width:100%" onclick="saveStudentEdit(false)">수정 완료</button>
    </div>
  </div>

  <div id="editManagedAcademyBox" class="hidden" style="position:fixed;inset:0;background:rgba(0,0,0,.35);z-index:75;padding:24px;display:flex;align-items:center;justify-content:center">
    <div class="card" style="width:min(520px,100%);margin:0">
      <div class="between">
        <div class="section-title" style="margin:0">학원 수정</div>
        <button class="secondary" onclick="closeManagedAcademyEdit()">닫기</button>
      </div>
      <input id="editManagedAcademyId" type="hidden">
      <div style="height:14px"></div>
      <div class="small" style="margin-bottom:6px">학원 이름</div>
      <input id="editManagedAcademyName" placeholder="학원 이름">
      <div style="height:10px"></div>
      <div class="grid">
        <div>
          <div class="small" style="margin-bottom:6px">지역</div>
          <select id="editManagedAcademyRegion" onchange="loadEditManagedDistricts()">
            <option value="">지역 선택</option>
          </select>
        </div>
        <div>
          <div class="small" style="margin-bottom:6px">시·군·구</div>
          <select id="editManagedAcademyDistrict">
            <option value="">시·군·구 선택</option>
          </select>
        </div>
      </div>
      <div id="editManagedAcademyMsg" class="msg"></div>
      <div style="height:12px"></div>
      <button class="primary" style="width:100%" onclick="saveManagedAcademyEdit()">수정 완료</button>
    </div>
  </div>

  <div id="academyManagementBox" class="hidden" style="position:fixed;inset:0;background:rgba(0,0,0,.35);z-index:60;padding:24px;display:flex;align-items:center;justify-content:center">
    <div class="card" style="width:min(960px,100%);max-height:90vh;overflow:auto;margin:0">
      <div class="between">
        <div>
          <div class="section-title" style="margin:0">학원 관리</div>
          <div class="small">전체 학원 수정 · 활성/비활성 · 삭제 · 공지 관리</div>
        </div>
        <button class="secondary" onclick="closeAcademyManagement()">닫기</button>
      </div>

      <div id="managementAuthBox" style="margin-top:16px">
        <div class="grid">
          <input id="managementKey" type="password" placeholder="학원 관리 인증키">
          <button class="primary" onclick="verifyAcademyManagement()">인증</button>
        </div>
        <div id="managementAuthMsg" class="msg"></div>
      </div>

      <div id="managementContent" class="hidden" style="margin-top:18px">
<div class="card" style="box-shadow:none">
          <div class="between">
            <div class="section-title" style="margin:0">등록 학원</div>
            <input id="managementSearch" style="max-width:320px" placeholder="학원 검색" oninput="renderManagementAcademies()">
          </div>
          <div class="tablewrap">
            <table>
              <thead><tr><th>학원명</th><th>상태</th><th></th></tr></thead>
              <tbody id="managementAcademiesBody"></tbody>
            </table>
          </div>
          <div id="managementMsg" class="msg"></div>
        </div>
      </div>
    </div>
  </div>

  <div id="manualAttendanceBox" class="hidden" style="position:fixed;inset:0;background:rgba(0,0,0,.35);z-index:50;padding:24px;display:flex;align-items:center;justify-content:center">
    <div class="card" style="width:min(520px,100%);margin:0">
      <div class="between">
        <div><div class="section-title" style="margin:0">출석등록</div><div id="manualStudentName" class="small"></div></div>
        <button class="secondary" onclick="closeManualAttendance()">닫기</button>
      </div>
      <div style="height:14px"></div>
      <div class="grid">
        <select id="manualType"><option value="IN">입실</option><option value="OUT">퇴실</option></select>
        <input id="manualTime" type="datetime-local" step="60">
      </div>
      <div class="small" style="margin-top:8px">시간은 1분 단위로 선택합니다. 과거 시간으로 등록해도 학부모 알림은 즉시 전송됩니다.</div>
      <div style="height:14px"></div>
      <button class="primary" onclick="saveManualAttendance()">등록</button>
      <div id="manualMsg" class="msg"></div>
    </div>
  </div>
</div>

  <div id="sentSuccessBox" class="hidden" style="position:fixed;inset:0;background:rgba(0,0,0,.35);z-index:80;padding:24px;display:flex;align-items:center;justify-content:center">
    <div class="card" style="width:min(360px,100%);margin:0;text-align:center">
      <div class="section-title">전송되었습니다.</div>
      <button class="primary" style="width:100%" onclick="confirmSentSuccess()">확인</button>
    </div>
  </div>
  <div id="adminNoticeBox" class="hidden" style="position:fixed;inset:0;background:rgba(0,0,0,.38);z-index:90;padding:24px;display:flex;align-items:center;justify-content:center">
    <div class="card" style="width:min(520px,100%);margin:0">
      <div id="adminNoticeTitle" class="section-title" style="margin-top:0">공지</div>
      <div id="adminNoticeContent" style="white-space:pre-wrap;line-height:1.6"></div>
      <div style="height:18px"></div>
      <div id="adminNoticeActions" class="row" style="justify-content:flex-end"></div>
    </div>
  </div>

<script>
let token="", academyId=null, academyName="", nfcExisting=false;
const $=id=>document.getElementById(id);
async function api(path,opt={}){const h={"Content-Type":"application/json",...(opt.headers||{})};if(token)h.Authorization="Bearer "+token;const r=await fetch(path,{...opt,headers:h});let d={};try{d=await r.json()}catch{}if(!r.ok)throw new Error(d.detail||"서버 오류");return d}
async function init(){
  const regions=await api("/api/v3/regions"); $("region").innerHTML='<option value="">지역</option>'+regions.map(x=>`<option>${x}</option>`).join("");
  const now=new Date();$("month").value=`${now.getFullYear()}-${String(now.getMonth()+1).padStart(2,"0")}`;
}
$("region").onchange=async()=>{const r=$("region").value;$("district").innerHTML='<option value="">시·군·구</option>';$("academy").innerHTML='<option value="">학원</option>';if(!r)return;const ds=await api("/api/v3/districts?region="+encodeURIComponent(r));$("district").innerHTML='<option value="">시·군·구</option>'+ds.map(x=>`<option>${x}</option>`).join("")}
$("district").onchange=loadAcademies;
$("academyQ").oninput=async()=>{
  const q=$("academyQ").value.trim(),box=$("academySearchResults");
  if(!q){box.innerHTML="";box.classList.add("hidden");return;}
  try{
    const xs=await api("/api/v3/academies/search?q="+encodeURIComponent(q));
    if(!xs.length){box.innerHTML='<div style="padding:12px;color:#6b7280">검색된 학원이 없습니다.</div>';box.classList.remove("hidden");return;}
    box.innerHTML=xs.map(a=>`<button type="button" style="display:block;width:100%;text-align:left;background:#fff;border-radius:0;border-bottom:1px solid #eee" onclick="selectSearchedAcademy(${a.id},'${esc(a.name).replace(/'/g,"&#39;")}','${esc(a.region).replace(/'/g,"&#39;")}','${esc(a.district).replace(/'/g,"&#39;")}')"><b>${esc(a.name)}</b><br><span class="small">${esc(a.region)} / ${esc(a.district)}</span></button>`).join("");
    box.classList.remove("hidden");
  }catch(e){box.innerHTML='<div style="padding:12px;color:#c62828">'+esc(e.message)+'</div>';box.classList.remove("hidden");}
}
async function selectSearchedAcademy(id,name,region,district){
  $("academyQ").value=name;
  $("academySearchResults").classList.add("hidden");
  if(!Array.from($("region").options).some(o=>o.value===region)) $("region").add(new Option(region,region));
  $("region").value=region;
  const ds=await api("/api/v3/districts?region="+encodeURIComponent(region));
  $("district").innerHTML='<option value="">시·군·구</option>'+ds.map(x=>`<option>${x}</option>`).join("");
  $("district").value=district;
  $("academy").innerHTML=`<option value="${id}">${esc(name)}</option>`;
  $("academy").value=String(id);
}
async function loadAcademies(){const r=$("region").value,d=$("district").value;if(!r||!d)return;const xs=await api(`/api/v3/academies?region=${encodeURIComponent(r)}&district=${encodeURIComponent(d)}`);$("academy").innerHTML='<option value="">학원</option>'+xs.map(a=>`<option value="${a.id}" data-name="${esc(a.name)}">${esc(a.name)}</option>`).join("")}
function esc(s){return String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]))}
async function login(){try{const id=Number($("academy").value);if(!id)throw new Error("학원을 선택해주세요.");const d=await api("/api/v3/admin/login",{method:"POST",body:JSON.stringify({academy_id:id,password:$("password").value})});token=d.access_token;academyId=d.academy_id;academyName=d.academy_name;$("academyTitle").textContent=academyName;$("loginCard").classList.add("hidden");$("admin").classList.remove("hidden");$("loginMsg").textContent="";showAdminNoticeIfNeeded();showTab("academy")}catch(e){$("loginMsg").textContent=e.message}}
let recoveryToken="";
function openForgot(){const id=Number($("academy").value);if(!id){$("loginMsg").textContent="학원을 먼저 선택해주세요.";return;}$("loginMsg").textContent="";$("forgotBox").classList.toggle("hidden");}
function backToLogin(){
  $("forgotBox").classList.add("hidden");
  $("resetBox").classList.add("hidden");
  $("recoveryName").value="";
  $("recoveryPhone").value="";
  $("recoveryLicenseKey").value="";
  $("resetPassword").value="";
  $("resetPassword2").value="";
  $("forgotMsg").textContent="";
  recoveryToken="";
}
async function verifyRecovery(){try{
  const id=Number($("academy").value);
  if(!id)throw new Error("학원을 먼저 선택해주세요.");
  const name=$("recoveryName").value.trim();
  const phone=$("recoveryPhone").value.trim();
  const licenseKey=$("recoveryLicenseKey").value.trim();
  if(!name)throw new Error("최초 관리자 성함을 입력해주세요.");
  if(!/^\d{4}$/.test(phone))throw new Error("전화번호 끝 4자리를 입력해주세요.");
  if(!licenseKey)throw new Error("일반 인증키를 입력해주세요.");
  const d=await api("/api/v3/admin/recovery/verify",{
    method:"POST",
    body:JSON.stringify({academy_id:id,recovery_name:name,recovery_phone_last4:phone,license_key:licenseKey})
  });
  recoveryToken=d.recovery_token;
  $("resetBox").classList.remove("hidden");
  $("forgotMsg").textContent="본인 확인이 완료되었습니다.";
  $("forgotMsg").className="msg ok";
}catch(e){
  $("forgotMsg").textContent=e.message;
  $("forgotMsg").className="msg";
}}
async function resetPasswordNow(){try{
  if(!recoveryToken)throw new Error("먼저 본인 확인을 해주세요.");
  const p=$("resetPassword").value;
  const p2=$("resetPassword2").value;
  if(p.length<4)throw new Error("비밀번호는 4자리 이상 입력해주세요.");
  if(p!==p2)throw new Error("재설정 비밀번호가 일치하지 않습니다.");

  await api("/api/v3/admin/recovery/reset",{
    method:"POST",
    body:JSON.stringify({
      recovery_token:recoveryToken,
      new_password:p
    })
  });

  recoveryToken="";
  $("password").value="";
  $("resetPassword").value="";
  $("resetPassword2").value="";
  $("forgotMsg").textContent="";
  $("resetBox").classList.add("hidden");
  $("forgotBox").classList.add("hidden"); // 변경 성공 즉시 비밀번호 찾기 창 닫기
}catch(e){
  $("forgotMsg").textContent=e.message;
  $("forgotMsg").className="msg";
}}

function formatGuardianPhone(v){
  const d=String(v||"").replace(/\D/g,"").slice(0,11);
  return d.length===11?`${d.slice(0,3)}-${d.slice(3,7)}-${d.slice(7)}`:d;
}
function logout(){token="";academyId=null;$("admin").classList.add("hidden");$("loginCard").classList.remove("hidden")}

let adminRegularNotice=null;
let adminEmergencyNotice=null;

function adminNoticeTodayKey(){
  const parts=new Intl.DateTimeFormat("en-CA",{
    timeZone:"Asia/Seoul",
    year:"numeric",
    month:"2-digit",
    day:"2-digit"
  }).formatToParts(new Date());
  const y=parts.find(x=>x.type==="year")?.value||"";
  const m=parts.find(x=>x.type==="month")?.value||"";
  const d=parts.find(x=>x.type==="day")?.value||"";
  return `${y}-${m}-${d}`;
}

function regularNoticeHiddenToday(){
  const academyKey=academyId?String(academyId):"global";
  return localStorage.getItem(`attendance_regular_notice_hidden_date_${academyKey}`)===adminNoticeTodayKey();
}

function hideRegularNoticeToday(){
  const academyKey=academyId?String(academyId):"global";
  localStorage.setItem(`attendance_regular_notice_hidden_date_${academyKey}`,adminNoticeTodayKey());
}

async function showAdminNoticeIfNeeded(){
  try{
    const notices=await api("/api/v3/notices");
    adminRegularNotice=notices.find(n=>n.type==="regular" && n.is_active && String(n.content||"").trim())||null;
    adminEmergencyNotice=notices.find(n=>n.type==="emergency" && n.is_active && String(n.content||"").trim())||null;

    if(adminEmergencyNotice){
      openAdminNotice(adminEmergencyNotice);
      return;
    }

    if(adminRegularNotice && !regularNoticeHiddenToday()){
      openAdminNotice(adminRegularNotice);
    }
  }catch(e){
    console.warn("공지 조회 실패",e);
  }
}

function openAdminNotice(notice){
  const type=notice.type;
  $("adminNoticeTitle").textContent=type==="emergency"?"긴급공지":"일반공지";
  $("adminNoticeContent").textContent=notice.content||"";

  if(type==="emergency"){
    $("adminNoticeActions").innerHTML=
      '<button class="primary" onclick="closeEmergencyAdminNotice()">확인</button>';
  }else{
    $("adminNoticeActions").innerHTML=
      '<button class="secondary" onclick="hideAdminRegularToday()">오늘 하루 보지 않기</button>'+
      '<button class="primary" onclick="closeAdminNotice()">닫기</button>';
  }

  $("adminNoticeBox").classList.remove("hidden");
}

function closeAdminNotice(){
  $("adminNoticeBox").classList.add("hidden");
}

function hideAdminRegularToday(){
  hideRegularNoticeToday();
  closeAdminNotice();
}

function closeEmergencyAdminNotice(){
  closeAdminNotice();
  if(adminRegularNotice && !regularNoticeHiddenToday()){
    setTimeout(()=>openAdminNotice(adminRegularNotice),80);
  }
}


let academyManagementToken="";
let managementAcademies=[];

// 학원 등록 여부와 무관한 대한민국 전국 행정구역 고정 목록
const ALL_KOREA_REGIONS=["서울특별시", "부산광역시", "대구광역시", "인천광역시", "광주광역시", "대전광역시", "울산광역시", "세종특별자치시", "경기도", "강원특별자치도", "충청북도", "충청남도", "전북특별자치도", "전라남도", "경상북도", "경상남도", "제주특별자치도"];
const ALL_KOREA_DISTRICTS={"서울특별시": ["종로구", "중구", "용산구", "성동구", "광진구", "동대문구", "중랑구", "성북구", "강북구", "도봉구", "노원구", "은평구", "서대문구", "마포구", "양천구", "강서구", "구로구", "금천구", "영등포구", "동작구", "관악구", "서초구", "강남구", "송파구", "강동구"], "부산광역시": ["중구", "서구", "동구", "영도구", "부산진구", "동래구", "남구", "북구", "해운대구", "사하구", "금정구", "강서구", "연제구", "수영구", "사상구", "기장군"], "대구광역시": ["중구", "동구", "서구", "남구", "북구", "수성구", "달서구", "달성군", "군위군"], "인천광역시": ["중구", "동구", "미추홀구", "연수구", "남동구", "부평구", "계양구", "서구", "강화군", "옹진군"], "광주광역시": ["동구", "서구", "남구", "북구", "광산구"], "대전광역시": ["동구", "중구", "서구", "유성구", "대덕구"], "울산광역시": ["중구", "남구", "동구", "북구", "울주군"], "세종특별자치시": ["세종특별자치시"], "경기도": ["수원시", "성남시", "의정부시", "안양시", "부천시", "광명시", "평택시", "동두천시", "안산시", "고양시", "과천시", "구리시", "남양주시", "오산시", "시흥시", "군포시", "의왕시", "하남시", "용인시", "파주시", "이천시", "안성시", "김포시", "화성시", "광주시", "양주시", "포천시", "여주시", "연천군", "가평군", "양평군"], "강원특별자치도": ["춘천시", "원주시", "강릉시", "동해시", "태백시", "속초시", "삼척시", "홍천군", "횡성군", "영월군", "평창군", "정선군", "철원군", "화천군", "양구군", "인제군", "고성군", "양양군"], "충청북도": ["청주시", "충주시", "제천시", "보은군", "옥천군", "영동군", "증평군", "진천군", "괴산군", "음성군", "단양군"], "충청남도": ["천안시", "공주시", "보령시", "아산시", "서산시", "논산시", "계룡시", "당진시", "금산군", "부여군", "서천군", "청양군", "홍성군", "예산군", "태안군"], "전북특별자치도": ["전주시", "군산시", "익산시", "정읍시", "남원시", "김제시", "완주군", "진안군", "무주군", "장수군", "임실군", "순창군", "고창군", "부안군"], "전라남도": ["목포시", "여수시", "순천시", "나주시", "광양시", "담양군", "곡성군", "구례군", "고흥군", "보성군", "화순군", "장흥군", "강진군", "해남군", "영암군", "무안군", "함평군", "영광군", "장성군", "완도군", "진도군", "신안군"], "경상북도": ["포항시", "경주시", "김천시", "안동시", "구미시", "영주시", "영천시", "상주시", "문경시", "경산시", "의성군", "청송군", "영양군", "영덕군", "청도군", "고령군", "성주군", "칠곡군", "예천군", "봉화군", "울진군", "울릉군"], "경상남도": ["창원시", "진주시", "통영시", "사천시", "김해시", "밀양시", "거제시", "양산시", "의령군", "함안군", "창녕군", "고성군", "남해군", "하동군", "산청군", "함양군", "거창군", "합천군"], "제주특별자치도": ["제주시", "서귀포시"]};

function openAcademyManagement(){
  $("academyManagementBox").classList.remove("hidden");
}
function closeAcademyManagement(){
  $("academyManagementBox").classList.add("hidden");
  $("managementAuthMsg").textContent="";
  $("managementMsg").textContent="";
}
async function verifyAcademyManagement(){
  try{
    const key=$("managementKey").value.trim();
    if(!key)throw new Error("인증키를 입력해주세요.");
    const d=await api("/api/v3/academy-management/verify",{
      method:"POST",
      body:JSON.stringify({license_key:key})
    });
    academyManagementToken=d.management_token;
    $("managementAuthBox").classList.add("hidden");
    $("managementContent").classList.remove("hidden");
    await loadManagementAcademies();
    await loadManagementNotices();
  }catch(e){
    $("managementAuthMsg").textContent=e.message;
  }
}
async function loadManagementAcademies(){
  try{
    managementAcademies=await api("/api/v3/academy-management/list?management_token="+encodeURIComponent(academyManagementToken));
    renderManagementAcademies();
  }catch(e){$("managementMsg").textContent=e.message}
}
function renderManagementAcademies(){
  const q=$("managementSearch").value.trim().toLowerCase();
  const rows=managementAcademies.filter(a=>
    !q ||
    String(a.name).toLowerCase().includes(q) ||
    String(a.region).toLowerCase().includes(q) ||
    String(a.district).toLowerCase().includes(q)
  );
  $("managementAcademiesBody").innerHTML=rows.map(a=>`
    <tr>
      <td><strong>${esc(a.name)}</strong></td>
      <td>${a.is_active?'<span class="pill">활성</span>':'<span class="pill" style="background:#f3f4f6;color:#6b7280">비활성</span>'}</td>
      <td>
        <div class="row">
          <button class="secondary" onclick="openManagedAcademyEdit(${a.id})">수정</button>
          <button class="secondary" onclick="toggleManagedAcademy(${a.id},${a.is_active?'false':'true'})">${a.is_active?'비활성화':'활성화'}</button>
          <button class="danger" onclick="deleteManagedAcademy(${a.id},'${esc(a.name).replace(/'/g,"&#39;")}')">삭제</button>
        </div>
      </td>
    </tr>`).join("");
}
async function openManagedAcademyEdit(id){
  const a=managementAcademies.find(x=>x.id===id);
  if(!a)return;

  $("editManagedAcademyId").value=String(a.id);
  $("editManagedAcademyName").value=a.name||"";
  $("editManagedAcademyMsg").textContent="";

  // 서버에 학원이 등록되어 있지 않아도 전국 17개 시·도를 항상 표시
  $("editManagedAcademyRegion").innerHTML=
    '<option value="">지역 선택</option>'+
    ALL_KOREA_REGIONS.map(r=>`<option value="${esc(r)}">${esc(r)}</option>`).join("");

  $("editManagedAcademyRegion").value=a.region||"";
  await loadEditManagedDistricts(a.district||"");

  $("editManagedAcademyBox").classList.remove("hidden");
}

async function loadEditManagedDistricts(selectedDistrict=""){
  const region=$("editManagedAcademyRegion").value;
  const select=$("editManagedAcademyDistrict");
  const districts=ALL_KOREA_DISTRICTS[region]||[];

  select.innerHTML=
    '<option value="">시·군·구 선택</option>'+
    districts.map(d=>`<option value="${esc(d)}">${esc(d)}</option>`).join("");

  if(selectedDistrict && districts.includes(selectedDistrict)){
    select.value=selectedDistrict;
  }else if(selectedDistrict && !districts.includes(selectedDistrict)){
    // 기존 데이터가 과거 명칭이면 임시로 현재 값도 선택 가능하게 유지
    select.innerHTML+=`<option value="${esc(selectedDistrict)}">${esc(selectedDistrict)}</option>`;
    select.value=selectedDistrict;
  }
}
function closeManagedAcademyEdit(){
  $("editManagedAcademyBox").classList.add("hidden");
  $("editManagedAcademyMsg").textContent="";
}
async function saveManagedAcademyEdit(){
  const id=Number($("editManagedAcademyId").value);
  const current=managementAcademies.find(x=>x.id===id);
  const name=$("editManagedAcademyName").value.trim();
  const region=$("editManagedAcademyRegion").value;
  const district=$("editManagedAcademyDistrict").value;

  if(!name){
    $("editManagedAcademyMsg").textContent="학원 이름을 입력해주세요.";
    return;
  }
  if(!region || !district){
    $("editManagedAcademyMsg").textContent="지역과 시·군·구를 선택해주세요.";
    return;
  }

  try{
    await api("/api/v3/academy-management/update",{
      method:"POST",
      body:JSON.stringify({
        management_token:academyManagementToken,
        academy_id:id,
        name:name,
        region:region,
        district:district,
        is_active:current?current.is_active:true
      })
    });
    await loadManagementAcademies();
    closeManagedAcademyEdit(); // 수정 완료 즉시 팝업 자동 닫기
  }catch(e){
    $("editManagedAcademyMsg").textContent=e.message;
  }
}
async function toggleManagedAcademy(id,activeValue){
  const a=managementAcademies.find(x=>x.id===id);
  if(!a)return;
  try{
    await api("/api/v3/academy-management/update",{
      method:"POST",
      body:JSON.stringify({
        management_token:academyManagementToken,
        academy_id:id,
        name:a.name,
        region:a.region,
        district:a.district,
        is_active:activeValue
      })
    });
    await loadManagementAcademies();
  }catch(e){$("managementMsg").textContent=e.message;$("managementMsg").className="msg"}
}
async function deleteManagedAcademy(id,name){
  if(!confirm(name+" 학원을 완전히 삭제하시겠습니까?"))return;
  try{
    await api("/api/v3/academy-management/delete",{
      method:"POST",
      body:JSON.stringify({management_token:academyManagementToken,academy_id:id})
    });
    $("managementMsg").textContent="삭제되었습니다.";
    $("managementMsg").className="msg ok";
    await loadManagementAcademies();
  }catch(e){$("managementMsg").textContent=e.message;$("managementMsg").className="msg"}
}
async function loadManagementNotices(){
  try{
    const d=await api("/api/v3/notices");
    const regular=d.regular||{};
    const emergency=d.emergency||{};
    $("regularNotice").value=regular.content||"";
    $("regularNoticeActive").checked=!!regular.is_active;
    $("emergencyNotice").value=emergency.content||"";
    $("emergencyNoticeActive").checked=!!emergency.is_active;
  }catch(e){
    // 공지 조회 실패가 학원관리 자체를 막지는 않음
  }
}
async function saveNotice(type){
  try{
    const emergency=type==="emergency";
    await api("/api/v3/academy-management/notice",{
      method:"POST",
      body:JSON.stringify({
        management_token:academyManagementToken,
        notice_type:type,
        content:$(emergency?"emergencyNotice":"regularNotice").value,
        is_active:$(emergency?"emergencyNoticeActive":"regularNoticeActive").checked
      })
    });
    $("noticeMsg").textContent="공지 저장 완료";
    $("noticeMsg").className="msg ok";
  }catch(e){$("noticeMsg").textContent=e.message;$("noticeMsg").className="msg"}
}
function academyHoursChanged(){
  const is24=$("academy24Hours").checked;
  $("academyHoursFields").classList.toggle("hidden",is24);
}
async function loadAcademySettings(){
  if(!token)return;
  try{
    const d=await api("/api/v3/admin/academy/settings");
    $("academyStatRegistered").textContent=(d.registered_students||0)+"명";
    $("academyStatCurrent").textContent=(d.current_in||0)+"명";
    $("academyStatIn").textContent=(d.today_in||0)+"명";
    $("academyStatOut").textContent=(d.today_out||0)+"명";
    $("academy24Hours").checked=!!d.is_24_hours;
    $("academyOpenTime").value=d.open_time||"09:00";
    $("academyCloseTime").value=d.close_time||"20:00";
    academyHoursChanged();
    $("academySettingsMsg").textContent="";
  }catch(e){$("academySettingsMsg").textContent=e.message}
}
async function saveAcademySettings(){
  if(!token)return;
  try{
    const d=await api("/api/v3/admin/academy/settings",{method:"PUT",body:JSON.stringify({is_24_hours:$("academy24Hours").checked,open_time:$("academyOpenTime").value||"09:00",close_time:$("academyCloseTime").value||"20:00"})});
    $("academySettingsMsg").textContent="저장되었습니다.";
    $("academySettingsMsg").className="msg ok";
    $("academyStatRegistered").textContent=(d.registered_students||0)+"명";
    $("academyStatCurrent").textContent=(d.current_in||0)+"명";
    $("academyStatIn").textContent=(d.today_in||0)+"명";
    $("academyStatOut").textContent=(d.today_out||0)+"명";
    academyHoursChanged();
  }catch(e){$("academySettingsMsg").textContent=e.message;$("academySettingsMsg").className="msg"}
}
function showTab(t){for(const x of ["academy","students","attendance","password"]){$(x+"Panel").classList.toggle("hidden",x!==t);$("tab"+x[0].toUpperCase()+x.slice(1)).classList.toggle("on",x===t)}if(t==="academy")loadAcademySettings();if(t==="students")loadStudents();if(t==="attendance")loadAttendance()}
let currentStudents=[];
async function loadStudents(){
  if(!token)return;
  try{
    const q=$("studentQ").value.trim();
    const xs=await api("/api/v3/admin/students?q="+encodeURIComponent(q));
    currentStudents=xs;
    $("studentsBody").innerHTML=xs.map(s=>`<tr>
      <td>${esc(s.name)}</td>
      <td>${esc(formatGuardianPhone(s.phone_last4))}</td>
      <td>${esc(s.attendance_pin)}</td>
      <td>${esc(s.memo)}</td>
      <td>${s.nfc_registered
        ? '<span class="pill">등록</span> <button class="secondary" onclick="prepareStudentNfc('+s.student_id+',true)">재등록</button>'
        : '<span class="pill" style="background:#f3f4f6;color:#6b7280">미등록</span> <button class="secondary" onclick="prepareStudentNfc('+s.student_id+',false)">NFC 등록</button>'
      }</td>
      <td>
        <div class="row">
          <button class="secondary" onclick="openStudentEdit(${s.student_id})">수정</button>
          <button class="secondary" onclick="openManualAttendance(${s.student_id},'${esc(s.name).replace(/'/g,"&#39;")}')">출석등록</button>
          <button class="danger" onclick="removeStudent(${s.student_id})">퇴원</button>
        </div>
      </td>
    </tr>`).join("");
  }catch(e){
    alert(e.message)
  }
}
function openStudentEdit(studentId){
  const s=currentStudents.find(x=>x.student_id===studentId);
  if(!s)return;

  $("editStudentId").value=String(s.student_id);
  $("editStudentOriginalName").value=s.name||"";
  $("editStudentOriginalPhone").value=s.phone_last4||"";
  $("editStudentName").value=s.name||"";
  $("editStudentPhone").value=s.phone_last4||"";
  $("editStudentPin").value=s.attendance_pin||"";
  $("editStudentMemo").value=s.memo||"";
  $("editStudentMsg").textContent="";
  $("editStudentBox").classList.remove("hidden");
}

function closeStudentEdit(){
  $("editStudentBox").classList.add("hidden");
  $("editStudentMsg").textContent="";
}

async function saveStudentEdit(confirmGlobal){
  try{
    const id=Number($("editStudentId").value);
    const name=$("editStudentName").value.trim();
    const phone=$("editStudentPhone").value.trim();
    const pin=$("editStudentPin").value.trim();
    const memo=$("editStudentMemo").value;
    const originalName=$("editStudentOriginalName").value;
    const originalPhone=$("editStudentOriginalPhone").value;

    if(!name)throw new Error("학생 이름을 입력해주세요.");
    if(!/^\d{11}$/.test(phone))throw new Error("보호자 전화번호 11자리를 입력해주세요.");
    if(!/^\d{4}$/.test(pin))throw new Error("출석번호 4자리를 입력해주세요.");

    const globalChanged=(name!==originalName || phone!==originalPhone);

    if(globalChanged && !confirmGlobal){
      const ok=confirm(
        "이름 또는 전화번호를 변경하면 이 학생의 정보가 전체 서버에 반영됩니다.\n\n"+
        "다른 학원에 등록된 동일 학생의 이름/전화번호도 함께 변경됩니다.\n\n계속하시겠습니까?"
      );
      if(!ok)return;
      confirmGlobal=true;
    }

    await api("/api/v3/admin/students/"+id,{
      method:"PUT",
      body:JSON.stringify({
        name:name,
        phone_last4:phone,
        attendance_pin:pin,
        memo:memo,
        confirm_global:confirmGlobal
      })
    });

    closeStudentEdit();
    await loadStudents();
  }catch(e){
    if(String(e.message).includes("GLOBAL_CONFIRM_REQUIRED")){
      const ok=confirm(
        "이름 또는 전화번호 변경은 전체 서버의 동일 학생 정보에 반영됩니다.\n계속하시겠습니까?"
      );
      if(ok){
        await saveStudentEdit(true);
      }
      return;
    }
    $("editStudentMsg").textContent=e.message;
    $("editStudentMsg").className="msg";
  }
}

let studentImportNfcToken="";

async function readExistingStudentNfc(){
  try{
    if(!("NDEFReader" in window)){
      throw new Error("현재 브라우저는 NFC 읽기를 지원하지 않습니다. Android 관리자 앱에서 NFC로 학생을 불러올 수 있습니다.");
    }
    $("studentFormMsg").textContent="기존 학생 NFC 카드를 태그해주세요.";
    $("studentFormMsg").className="msg";

    const ndef=new NDEFReader();
    await ndef.scan();

    ndef.onreading=async event=>{
      try{
        const decoder=new TextDecoder();
        let tokenValue="";
        for(const record of event.message.records){
          if(record.recordType==="mime" || record.recordType==="text"){
            tokenValue=decoder.decode(record.data);
            if(tokenValue)break;
          }
        }
        if(!tokenValue)throw new Error("NFC 카드 값을 읽지 못했습니다.");

        const d=await api("/api/v3/admin/nfc/lookup",{
          method:"POST",
          body:JSON.stringify({nfc_token:tokenValue})
        });

        if(!d.exists)throw new Error("등록된 학생 NFC 카드가 아닙니다.");
        if(d.already_in_academy)throw new Error("이미 이 학원에 등록된 학생입니다.");

        studentImportNfcToken=tokenValue;
        $("studentName").value=d.name||"";
        $("studentPhone").value=d.phone_last4||"";
        $("studentName").readOnly=true;
        $("studentPhone").readOnly=true;
        $("cancelStudentNfcImport").classList.remove("hidden");
        $("studentFormMsg").textContent="기존 학생 정보를 불러왔습니다. 출석번호와 이 학원 메모를 입력 후 학생 등록을 누르세요.";
        $("studentFormMsg").className="msg ok";
        ndef.onreading=null;
      }catch(e){
        $("studentFormMsg").textContent=e.message;
        $("studentFormMsg").className="msg";
      }
    };
  }catch(e){
    $("studentFormMsg").textContent=e.message;
    $("studentFormMsg").className="msg";
  }
}

function cancelExistingStudentNfc(){
  studentImportNfcToken="";
  $("studentName").value="";
  $("studentPhone").value="";
  $("studentName").readOnly=false;
  $("studentPhone").readOnly=false;
  $("cancelStudentNfcImport").classList.add("hidden");
  $("studentFormMsg").textContent="";
}

async function saveStudentNoNfc(){try{
  const name=$("studentName").value.trim(),phone=$("studentPhone").value.trim(),pin=$("studentPin").value.trim(),memo=$("studentMemo").value;
  if(!name)throw new Error("학생 이름을 입력해주세요.");
  if(!/^\d{11}$/.test(phone))throw new Error("보호자 전화번호 11자리를 입력해주세요.");
  if(!/^\d{4}$/.test(pin))throw new Error("출석번호 4자리를 입력해주세요.");
  if(studentImportNfcToken){
    await api("/api/v3/admin/students/attach-existing",{
      method:"POST",
      body:JSON.stringify({
        nfc_token:studentImportNfcToken,
        attendance_pin:pin,
        memo:memo
      })
    });
    $("studentFormMsg").textContent="NFC 기존 학생 등록 완료";
    $("studentName").readOnly=false;
    $("studentPhone").readOnly=false;
    studentImportNfcToken="";
    $("cancelStudentNfcImport").classList.add("hidden");
  }else{
    await api("/api/v3/admin/students/new",{method:"POST",body:JSON.stringify({name,phone_last4:phone,attendance_pin:pin,memo})});
    $("studentFormMsg").textContent="학생 등록 완료 (NFC 미등록)";
  }
  $("studentFormMsg").className="msg ok";
  for(const id of ["studentName","studentPhone","studentPin","studentMemo"])$(id).value="";
  loadStudents();
}catch(e){$("studentFormMsg").textContent=e.message;$("studentFormMsg").className="msg"}}
async function prepareStudentNfc(studentId,replacing){try{
  if(replacing && !confirm("새 NFC 등록을 시작하면 분실한 기존 NFC는 즉시 사용할 수 없게 됩니다. 계속할까요?"))return;
  const d=await api(`/api/v3/admin/students/${studentId}/nfc/prepare`,{method:"POST"});
  const tokenToWrite=d.nfc_token, registrationToken=d.nfc_registration_token;
  if(!("NDEFReader" in window)){
    alert("새 NFC 토큰이 발급되었고 기존 카드는 비활성화되었습니다. 현재 PC 브라우저는 NFC 카드 직접 쓰기를 지원하지 않습니다. Android 관리자 앱 또는 추후 지원 NFC 리더기로 등록을 완료해주세요. 학생은 NFC 미등록 상태로 유지됩니다.");
    loadStudents();return;
  }
  const ndef=new NDEFReader();
  await ndef.write({records:[{recordType:"mime",mediaType:"application/vnd.codenote.attendance",data:tokenToWrite}]});
  await api(`/api/v3/admin/students/${studentId}/nfc/confirm`,{method:"POST",body:JSON.stringify({nfc_registration_token:registrationToken})});
  alert("NFC 카드 등록이 완료되었습니다.");loadStudents();
}catch(e){alert(e.message)}}
async function removeStudent(id){if(!confirm("이 학원에서 학생을 퇴원 처리할까요? 다른 학원 연결은 유지됩니다."))return;try{await api("/api/v3/admin/students/"+id,{method:"DELETE"});loadStudents()}catch(e){alert(e.message)}}
let manualStudentId=0;
function currentMinuteLocal(){
  const d=new Date();d.setSeconds(0,0);
  const off=d.getTimezoneOffset();const local=new Date(d.getTime()-off*60000);
  return local.toISOString().slice(0,16);
}
function openManualAttendance(id,name){
  manualStudentId=id;$("manualStudentName").textContent=name;$("manualTime").value=currentMinuteLocal();$("manualMsg").textContent="";
  $("manualAttendanceBox").classList.remove("hidden");
}
function closeManualAttendance(){$("manualAttendanceBox").classList.add("hidden");manualStudentId=0;}
async function saveManualAttendance(){try{
  if(!manualStudentId)throw new Error("학생을 선택해주세요.");
  const local=$("manualTime").value;if(!local)throw new Error("출석 시간을 선택해주세요.");
  const dt=new Date(local);if(isNaN(dt.getTime()))throw new Error("출석 시간이 올바르지 않습니다.");
  await api("/api/v3/admin/attendance/manual",{method:"POST",body:JSON.stringify({student_id:manualStudentId,event_type:$("manualType").value,occurred_at:dt.toISOString()})});
  $("manualMsg").textContent="";
  await loadAttendance();
  $("sentSuccessBox").classList.remove("hidden");
}catch(e){$("manualMsg").textContent=e.message;$("manualMsg").className="msg"}}
function confirmSentSuccess(){
  $("sentSuccessBox").classList.add("hidden");
  closeManualAttendance();
}
let attendanceRows=[];
let selectedAttendanceDate="";
function kstDateKey(value){
  const d=value instanceof Date?value:new Date(value);
  const parts=new Intl.DateTimeFormat("ko-KR",{timeZone:"Asia/Seoul",year:"numeric",month:"2-digit",day:"2-digit"}).formatToParts(d);
  const y=parts.find(x=>x.type==="year").value,m=parts.find(x=>x.type==="month").value,day=parts.find(x=>x.type==="day").value;
  return `${y}-${m}-${day}`;
}
function todayKst(){return kstDateKey(new Date())}
function attendanceMonthChanged(){
  const ym=$("month").value;
  if(!ym)return;
  selectedAttendanceDate=(ym===todayKst().slice(0,7))?todayKst():`${ym}-01`;
  loadAttendance();
}
function selectAttendanceDate(dateKey){selectedAttendanceDate=dateKey;renderAttendanceCalendar();renderAttendanceTable()}
function renderAttendanceCalendar(){
  const ym=$("month").value;if(!ym)return;
  const [y,m]=ym.split("-").map(Number);
  const first=new Date(y,m-1,1);const days=new Date(y,m,0).getDate();const start=first.getDay();
  const counts={};attendanceRows.forEach(e=>{const k=kstDateKey(e.occurred_at);counts[k]=(counts[k]||0)+1});
  let cells="";for(let i=0;i<start;i++)cells+='<button class="att-day empty" disabled></button>';
  for(let d=1;d<=days;d++){const key=`${y}-${String(m).padStart(2,"0")}-${String(d).padStart(2,"0")}`;const c=counts[key]||0;const cls=`att-day${key===selectedAttendanceDate?" selected":""}${key===todayKst()?" today":""}`;cells+=`<button class="${cls}" onclick="selectAttendanceDate('${key}')"><span class="att-num">${d}</span>${c?`<span class="att-count">출석 ${c}건</span>`:""}</button>`}
  $("attendanceCalendar").innerHTML='<div class="att-week"><div>일</div><div>월</div><div>화</div><div>수</div><div>목</div><div>금</div><div>토</div></div><div class="att-days">'+cells+'</div>';
}
function renderAttendanceTable(){
  if(!selectedAttendanceDate){$("attendanceBody").innerHTML="";return}
  const xs=attendanceRows.filter(e=>kstDateKey(e.occurred_at)===selectedAttendanceDate);
  const [y,m,d]=selectedAttendanceDate.split("-");$("attendanceSelectedTitle").textContent=`${Number(y)}년 ${Number(m)}월 ${Number(d)}일 출석현황 (${xs.length}건)`;
  $("attendanceBody").innerHTML=xs.length?xs.map(e=>`<tr><td>${new Date(e.occurred_at).toLocaleString("ko-KR",{timeZone:"Asia/Seoul"})}</td><td>${esc(e.student_name)}</td><td>${esc(e.phone_last4)}</td><td>${e.event_type==="IN"?"입실":"퇴실"}</td><td>${esc(e.source)}</td><td><button class="danger" onclick="deleteAttendance(${e.id})">삭제</button></td></tr>`).join(""):'<tr><td colspan="6" class="small">선택한 날짜의 출석 기록이 없습니다.</td></tr>';
}
async function loadAttendance(){if(!token)return;try{const [y,m]=$("month").value.split("-");const q=$("attQ").value.trim();attendanceRows=await api(`/api/v3/admin/attendance?year=${y}&month=${Number(m)}&q=${encodeURIComponent(q)}`);if(!selectedAttendanceDate||!selectedAttendanceDate.startsWith(`${y}-${String(Number(m)).padStart(2,"0")}`))selectedAttendanceDate=($("month").value===todayKst().slice(0,7))?todayKst():`${y}-${String(Number(m)).padStart(2,"0")}-01`;renderAttendanceCalendar();renderAttendanceTable()}catch(e){alert(e.message)}}
async function deleteAttendance(id){
  if(!confirm("이 출석기록을 삭제하시겠습니까?"))return;
  try{
    await api("/api/v3/admin/attendance/"+id,{method:"DELETE"});
    loadAttendance();
  }catch(e){alert(e.message)}
}
async function changePassword(){try{await api("/api/v3/admin/password",{method:"POST",body:JSON.stringify({current_password:$("currentPw").value,new_password:$("newPw").value})});$("pwMsg").textContent="비밀번호가 변경되었습니다.";$("pwMsg").className="msg ok"}catch(e){$("pwMsg").textContent=e.message;$("pwMsg").className="msg"}}
init().catch(e=>$("loginMsg").textContent=e.message);
</script>
</body></html>"""

@app.get("/",response_class=HTMLResponse)
def root_admin_web():
    return ADMIN_WEB_HTML

@app.get("/admin",response_class=HTMLResponse)
def admin_web():
    return ADMIN_WEB_HTML

@app.post("/api/v3/academy-registration/verify")
async def reg_verify(r:KeyReq):
    try: ok=await verify_license_key(r.license_key)
    except AuthUnavailable as e: raise HTTPException(503,str(e))
    if not ok: raise HTTPException(401,"유효하지 않은 인증키입니다.")
    return {"registration_token":token("academy_registration")}
@app.post("/api/v3/academy-registration/register")
def reg(r:AcademyCreate,db:Session=Depends(get_db)):
    read_token(r.registration_token,"academy_registration",600); p=digits(r.recovery_phone_last4,4,"전화번호 뒷자리")
    if db.scalar(select(Academy).where(Academy.region==r.region.strip(),Academy.district==r.district.strip(),Academy.name==r.name.strip())): raise HTTPException(409,"이미 등록된 학원입니다.")
    a=Academy(name=r.name.strip(),region=r.region.strip(),district=r.district.strip(),recovery_name=r.recovery_name.strip(),recovery_phone_last4=p); db.add(a); db.flush(); db.add(AdminCredential(academy_id=a.id,password_hash=hash_password(r.admin_password))); db.commit(); return {"id":a.id}

@app.post("/api/v3/admin/login")
def admin_login(r:AdminLoginReq,db:Session=Depends(get_db)):
    a=active_academy(db,r.academy_id); c=db.get(AdminCredential,a.id)
    if not c or not verify_password(r.password,c.password_hash): raise HTTPException(401,"관리자 비밀번호가 올바르지 않습니다.")
    return {"academy_id":a.id,"academy_name":a.name,"access_token":token("admin",academy_id=a.id)}
@app.post("/api/v3/admin/password")
def change_pw(r:ChangePw,auth=Depends(admin_auth),db:Session=Depends(get_db)):
    c=db.get(AdminCredential,auth["academy_id"])
    if not verify_password(r.current_password,c.password_hash): raise HTTPException(401,"현재 비밀번호가 올바르지 않습니다.")
    c.password_hash=hash_password(r.new_password); db.commit(); return {"ok":True}
@app.post("/api/v3/admin/recovery/verify")
async def recovery(r:RecoveryVerify,db:Session=Depends(get_db)):
    # 관리자 비밀번호 재설정은 등록정보 + 서버의 유효한 일반 인증키를 모두 확인
    try:
        key_ok=await verify_license_key(r.license_key.strip())
    except AuthUnavailable as e:
        raise HTTPException(503,str(e))
    if not key_ok:
        raise HTTPException(401,"유효하지 않은 인증키입니다.")

    a=active_academy(db,r.academy_id)
    if a.recovery_name!=r.recovery_name.strip() or a.recovery_phone_last4!=digits(r.recovery_phone_last4,4,"전화번호 뒷자리"):
        raise HTTPException(401,"등록정보가 일치하지 않습니다.")
    return {"recovery_token":token("recovery",academy_id=a.id)}
@app.post("/api/v3/admin/recovery/reset")
def recovery_reset(r:ResetPw,db:Session=Depends(get_db)):
    d=read_token(r.recovery_token,"recovery",600); c=db.get(AdminCredential,d["academy_id"]); c.password_hash=hash_password(r.new_password); db.commit(); return {"ok":True}

def academy_stats_payload(db:Session,a:Academy):
    active_ids=list(db.scalars(select(StudentAcademy.student_id).where(
        StudentAcademy.academy_id==a.id,
        StudentAcademy.is_active.is_(True)
    )).all())
    registered=len(active_ids)
    now=now_kst().astimezone(timezone.utc)
    start=academy_business_start_utc(a,now)

    today_in=db.scalar(select(func.count(func.distinct(AttendanceEvent.student_id))).where(
        AttendanceEvent.academy_id==a.id,
        AttendanceEvent.occurred_at>=start,
        AttendanceEvent.event_type=="IN"
    )) or 0
    today_out=db.scalar(select(func.count(func.distinct(AttendanceEvent.student_id))).where(
        AttendanceEvent.academy_id==a.id,
        AttendanceEvent.occurred_at>=start,
        AttendanceEvent.event_type=="OUT"
    )) or 0

    current_in=0
    if active_ids:
        stmt=select(AttendanceEvent).where(
            AttendanceEvent.academy_id==a.id,
            AttendanceEvent.student_id.in_(active_ids)
        )
        if not getattr(a,"is_24_hours",True):
            stmt=stmt.where(AttendanceEvent.occurred_at>=start)
        events=db.scalars(stmt.order_by(AttendanceEvent.occurred_at.desc())).all()
        seen=set()
        for e in events:
            if e.student_id in seen: continue
            seen.add(e.student_id)
            if e.event_type=="IN": current_in+=1
            if len(seen)>=registered: break

    return {
        "academy_id":a.id,
        "is_24_hours":bool(getattr(a,"is_24_hours",True)),
        "open_time":getattr(a,"open_time","09:00"),
        "close_time":getattr(a,"close_time","20:00"),
        "registered_students":registered,
        "current_in":current_in,
        "today_in":int(today_in),
        "today_out":int(today_out)
    }

@app.get("/api/v3/admin/academy/settings")
def admin_academy_settings(auth=Depends(admin_auth),db:Session=Depends(get_db)):
    a=active_academy(db,auth["academy_id"])
    return academy_stats_payload(db,a)

@app.put("/api/v3/admin/academy/settings")
def update_admin_academy_settings(r:AcademyHoursReq,auth=Depends(admin_auth),db:Session=Depends(get_db)):
    a=active_academy(db,auth["academy_id"])
    hhmm(r.open_time,"영업 시작시간"); hhmm(r.close_time,"영업 종료시간")
    a.is_24_hours=r.is_24_hours
    a.open_time=r.open_time
    a.close_time=r.close_time
    db.commit()
    return academy_stats_payload(db,a)

@app.post("/api/v3/admin/students/new")
def new_student_without_nfc(r:NewStudentNoNfc,auth=Depends(admin_auth),db:Session=Depends(get_db)):
    phone=digits(r.phone_last4,11,"보호자 전화번호"); pin=digits(r.attendance_pin,4,"출석번호")
    if db.scalar(select(StudentAcademy).where(StudentAcademy.academy_id==auth["academy_id"],StudentAcademy.attendance_pin==pin,StudentAcademy.is_active.is_(True))):
        raise HTTPException(409,"이 학원에서 이미 사용 중인 출석번호입니다.")
    existing=db.scalar(select(Student).where(Student.name==r.name.strip(),Student.phone_last4==phone))
    if existing:
        raise HTTPException(409,"중복학생이니 NFC로만 등록 가능합니다.")
    while True:
        inactive_nfc="INACTIVE-"+secrets.token_urlsafe(32)
        if not db.scalar(select(Student.id).where(Student.nfc_token==inactive_nfc)): break
    s=Student(name=r.name.strip(),phone_last4=phone,nfc_token=inactive_nfc,nfc_active=False); db.add(s); db.flush()
    sa=StudentAcademy(student_id=s.id,academy_id=auth["academy_id"],attendance_pin=pin,memo=r.memo.strip()); db.add(sa); db.flush()
    refresh_duplicate_codes(db,auth["academy_id"],s.name,s.phone_last4); sync_parent_family_links(db,s.phone_last4); db.commit()
    return {"student_id":s.id,"student_academy_id":sa.id,"nfc_registered":False,"extra_code":sa.login_extra_code}


@app.post("/api/v3/admin/students/duplicate-check")
def duplicate_student_check(r:StudentIdentityReq,auth=Depends(admin_auth),db:Session=Depends(get_db)):
    phone=digits(r.phone_last4,11,"보호자 전화번호")
    s=db.scalar(select(Student).where(Student.name==r.name.strip(),Student.phone_last4==phone))
    if not s: return {"duplicate":False}
    own=db.scalar(select(StudentAcademy).where(StudentAcademy.student_id==s.id,StudentAcademy.academy_id==auth["academy_id"],StudentAcademy.is_active.is_(True)))
    return {"duplicate":True,"student_id":s.id,"nfc_registered":bool(s.nfc_active),"already_in_academy":bool(own),"message":"기존 학생 정보를 가져와 등록할 수 있습니다."}

@app.post("/api/v3/admin/students/lost-nfc/prepare")
def lost_nfc_prepare(r:LostNfcPrepareReq,auth=Depends(admin_auth),db:Session=Depends(get_db)):
    phone=digits(r.phone_last4,11,"보호자 전화번호"); pin=digits(r.attendance_pin,4,"출석번호")
    s=db.scalar(select(Student).where(Student.name==r.name.strip(),Student.phone_last4==phone))
    if not s: raise HTTPException(404,"동일한 이름과 보호자 전화번호로 등록된 기존 학생이 없습니다.")
    pin_owner=db.scalar(select(StudentAcademy).where(StudentAcademy.academy_id==auth["academy_id"],StudentAcademy.attendance_pin==pin,StudentAcademy.is_active.is_(True),StudentAcademy.student_id!=s.id))
    if pin_owner: raise HTTPException(409,"이 학원에서 이미 사용 중인 출석번호입니다.")
    s.nfc_token=None; s.nfc_active=False; s.updated_at=now_kst().astimezone(timezone.utc); db.flush()
    while True:
        value="CN-"+secrets.token_urlsafe(32)
        if not db.scalar(select(Student.id).where(Student.nfc_token==value)): break
    registration=token("lost_nfc_registration",student_id=s.id,academy_id=auth["academy_id"],nfc_token=value,attendance_pin=pin,memo=r.memo.strip())
    db.commit()
    return {"student_id":s.id,"nfc_token":value,"nfc_registration_token":registration}

@app.post("/api/v3/admin/students/lost-nfc/confirm")
def lost_nfc_confirm(r:NfcConfirm,auth=Depends(admin_auth),db:Session=Depends(get_db)):
    p=read_token(r.nfc_registration_token,"lost_nfc_registration",600)
    if int(p.get("academy_id",0))!=int(auth["academy_id"]): raise HTTPException(403,"다른 학원의 NFC 등록 요청입니다.")
    s=db.get(Student,int(p["student_id"]))
    if not s: raise HTTPException(404,"학생을 찾을 수 없습니다.")
    nt=str(p.get("nfc_token","")).strip()
    if db.scalar(select(Student).where(Student.nfc_token==nt,Student.id!=s.id)): raise HTTPException(409,"이미 다른 학생에게 사용 중인 NFC 토큰입니다.")
    sa=db.scalar(select(StudentAcademy).where(StudentAcademy.student_id==s.id,StudentAcademy.academy_id==auth["academy_id"],StudentAcademy.is_active.is_(True)))
    if not sa:
        sa=StudentAcademy(student_id=s.id,academy_id=auth["academy_id"],attendance_pin=str(p.get("attendance_pin","")),memo=str(p.get("memo",""))); db.add(sa)
    else:
        sa.attendance_pin=str(p.get("attendance_pin",sa.attendance_pin))
        if str(p.get("memo","")): sa.memo=str(p.get("memo",""))
    s.nfc_token=nt; s.nfc_active=True; s.updated_at=now_kst().astimezone(timezone.utc); db.commit()
    return {"ok":True,"student_id":s.id,"nfc_registered":True}

@app.post("/api/v3/admin/students/{student_id}/nfc/reset")
def reset_student_nfc(student_id:int,auth=Depends(admin_auth),db:Session=Depends(get_db)):
    sa=db.scalar(select(StudentAcademy).where(StudentAcademy.student_id==student_id,StudentAcademy.academy_id==auth["academy_id"],StudentAcademy.is_active.is_(True)))
    if not sa: raise HTTPException(404,"이 학원에 등록된 학생이 아닙니다.")
    s=db.get(Student,student_id)
    if not s: raise HTTPException(404,"학생을 찾을 수 없습니다.")
    s.nfc_token=None; s.nfc_active=False; s.updated_at=now_kst().astimezone(timezone.utc); db.commit()
    return {"ok":True,"student_id":student_id,"nfc_registered":False}


@app.post("/api/v3/admin/students/attach-existing-by-identity")
def attach_existing_by_identity(r:NewStudentNoNfc,auth=Depends(admin_auth),db:Session=Depends(get_db)):
    phone=digits(r.phone_last4,11,"보호자 전화번호")
    pin=digits(r.attendance_pin,4,"출석번호")
    s=db.scalar(select(Student).where(
        Student.name==r.name.strip(),
        Student.phone_last4==phone
    ))
    if not s:
        raise HTTPException(404,"동일한 이름과 보호자 전화번호로 등록된 기존 학생이 없습니다.")

    existing_link=db.scalar(select(StudentAcademy).where(
        StudentAcademy.student_id==s.id,
        StudentAcademy.academy_id==auth["academy_id"]
    ))

    if existing_link and existing_link.is_active:
        raise HTTPException(409,"이미 이 학원에 등록된 학생입니다.")

    pin_owner=db.scalar(select(StudentAcademy).where(
        StudentAcademy.academy_id==auth["academy_id"],
        StudentAcademy.attendance_pin==pin,
        StudentAcademy.student_id!=s.id
    ))
    if pin_owner:
        raise HTTPException(409,"이 학원에서 이미 사용 중인 출석번호입니다.")

    if existing_link:
        # 과거에 퇴원/비활성 처리된 같은 학생-학원 연결이 있으면 새 행을 만들지 않고 복구합니다.
        existing_link.attendance_pin=pin
        existing_link.memo=r.memo.strip()
        existing_link.is_active=True
        existing_link.login_extra_code=None
        sa=existing_link
    else:
        sa=StudentAcademy(
            student_id=s.id,
            academy_id=auth["academy_id"],
            attendance_pin=pin,
            memo=r.memo.strip()
        )
        db.add(sa)

    db.flush()

    refresh_duplicate_codes(db,auth["academy_id"],s.name,s.phone_last4)

    # 기존 학생의 NFC는 Student에 전역으로 보관되므로 새 학원에도 그대로 연결됩니다.
    device_ids=list(db.scalars(
        select(ParentLink.device_id)
        .where(ParentLink.student_id==s.id)
        .distinct()
    ).all())

    for did in device_ids:
        exists=db.scalar(select(ParentLink).where(
            ParentLink.device_id==did,
            ParentLink.student_id==s.id,
            ParentLink.academy_id==auth["academy_id"]
        ))
        if not exists:
            db.add(ParentLink(
                device_id=did,
                student_id=s.id,
                academy_id=auth["academy_id"]
            ))

    db.commit()
    return {
        "ok":True,
        "student_id":s.id,
        "name":s.name,
        "phone_last4":s.phone_last4,
        "nfc_registered":bool(s.nfc_active),
        "extra_code":sa.login_extra_code
    }

@app.post("/api/v3/admin/students/{student_id}/nfc/prepare")
def prepare_student_nfc(student_id:int,auth=Depends(admin_auth),db:Session=Depends(get_db)):
    sa=db.scalar(select(StudentAcademy).where(StudentAcademy.student_id==student_id,StudentAcademy.academy_id==auth["academy_id"],StudentAcademy.is_active.is_(True)))
    s=db.get(Student,student_id)
    if not sa or not s: raise HTTPException(404,"학생을 찾을 수 없습니다.")
    # 재발급을 시작하는 즉시 이전 카드는 무효화하되,
    # DB의 nfc_token NOT NULL/UNIQUE 조건을 깨지 않도록 비활성 전용 토큰을 넣습니다.
    while True:
        inactive_nfc="INACTIVE-"+secrets.token_urlsafe(32)
        if not db.scalar(select(Student.id).where(Student.nfc_token==inactive_nfc)): break
    s.nfc_token=inactive_nfc; s.nfc_active=False; s.updated_at=now_kst().astimezone(timezone.utc); db.flush()
    while True:
        value="CN-"+secrets.token_urlsafe(32)
        if not db.scalar(select(Student.id).where(Student.nfc_token==value)): break
    registration=token("nfc_registration",student_id=s.id,academy_id=auth["academy_id"],nfc_token=value)
    db.commit()
    return {"nfc_token":value,"nfc_registration_token":registration,"nfc_registered":False}

@app.post("/api/v3/admin/students/{student_id}/nfc/confirm")
def confirm_student_nfc(student_id:int,r:NfcConfirm,auth=Depends(admin_auth),db:Session=Depends(get_db)):
    p=read_token(r.nfc_registration_token,"nfc_registration",600)
    if int(p.get("student_id",0))!=student_id or int(p.get("academy_id",0))!=int(auth["academy_id"]): raise HTTPException(401,"NFC 등록정보가 올바르지 않습니다.")
    sa=db.scalar(select(StudentAcademy).where(StudentAcademy.student_id==student_id,StudentAcademy.academy_id==auth["academy_id"],StudentAcademy.is_active.is_(True)))
    s=db.get(Student,student_id)
    if not sa or not s: raise HTTPException(404,"학생을 찾을 수 없습니다.")
    nt=str(p.get("nfc_token","")).strip()
    if not nt: raise HTTPException(400,"NFC 토큰이 없습니다.")
    if db.scalar(select(Student).where(Student.nfc_token==nt,Student.id!=student_id)): raise HTTPException(409,"이미 다른 학생에게 사용 중인 NFC 토큰입니다.")
    s.nfc_token=nt; s.nfc_active=True; s.updated_at=now_kst().astimezone(timezone.utc); db.commit(); return {"ok":True,"nfc_registered":True}

@app.post("/api/v3/admin/nfc/issue-token")
def issue_nfc_token(auth=Depends(admin_auth),db:Session=Depends(get_db)):
    while True:
        value = "CN-" + secrets.token_urlsafe(32)
        if not db.scalar(select(Student.id).where(Student.nfc_token == value)):
            return {"nfc_token": value}

@app.post("/api/v3/admin/nfc/lookup")
def nfc_lookup(r:NfcLookup,auth=Depends(admin_auth),db:Session=Depends(get_db)):
    s=db.scalar(select(Student).where(Student.nfc_token==r.nfc_token.strip(),Student.nfc_active.is_(True)))
    if not s: return {"exists":False}
    own=db.scalar(select(StudentAcademy).where(StudentAcademy.student_id==s.id,StudentAcademy.academy_id==auth["academy_id"],StudentAcademy.is_active.is_(True)))
    return {"exists":True,"already_in_academy":bool(own),"student_id":s.id,"name":s.name,"phone_last4":s.phone_last4}
@app.post("/api/v3/admin/students/new-with-nfc")
def new_student(r:NewStudentNfc,auth=Depends(admin_auth),db:Session=Depends(get_db)):
    phone=digits(r.phone_last4,11,"보호자 전화번호"); pin=digits(r.attendance_pin,4,"출석번호"); nt=r.nfc_token.strip()
    if not nt: raise HTTPException(400,"NFC 카드 등록이 필요합니다.")
    if db.scalar(select(Student).where(Student.nfc_token==nt)): raise HTTPException(409,"이미 등록된 NFC 카드입니다. 기존 학생 불러오기를 사용하세요.")
    if db.scalar(select(StudentAcademy).where(StudentAcademy.academy_id==auth["academy_id"],StudentAcademy.attendance_pin==pin,StudentAcademy.is_active.is_(True))): raise HTTPException(409,"이 학원에서 이미 사용 중인 출석번호입니다.")
    s=Student(name=r.name.strip(),phone_last4=phone,nfc_token=nt,nfc_active=True); db.add(s); db.flush(); sa=StudentAcademy(student_id=s.id,academy_id=auth["academy_id"],attendance_pin=pin,memo=r.memo.strip()); db.add(sa); db.flush(); refresh_duplicate_codes(db,auth["academy_id"],s.name,s.phone_last4); sync_parent_family_links(db,s.phone_last4); db.commit(); return {"student_id":s.id,"student_academy_id":sa.id,"extra_code":sa.login_extra_code}
@app.post("/api/v3/admin/students/attach-existing")
def attach(r:AttachExisting,auth=Depends(admin_auth),db:Session=Depends(get_db)):
    pin=digits(r.attendance_pin,4,"출석번호"); s=db.scalar(select(Student).where(Student.nfc_token==r.nfc_token.strip(),Student.nfc_active.is_(True)))
    if not s: raise HTTPException(404,"등록된 NFC 학생이 아닙니다.")
    if db.scalar(select(StudentAcademy).where(StudentAcademy.student_id==s.id,StudentAcademy.academy_id==auth["academy_id"],StudentAcademy.is_active.is_(True))): raise HTTPException(409,"이미 이 학원에 등록된 학생입니다.")
    if db.scalar(select(StudentAcademy).where(StudentAcademy.academy_id==auth["academy_id"],StudentAcademy.attendance_pin==pin,StudentAcademy.is_active.is_(True))): raise HTTPException(409,"이 학원에서 이미 사용 중인 출석번호입니다.")
    sa=StudentAcademy(student_id=s.id,academy_id=auth["academy_id"],attendance_pin=pin,memo=r.memo.strip()); db.add(sa); db.flush(); refresh_duplicate_codes(db,auth["academy_id"],s.name,s.phone_last4)
    # 이미 이 학생으로 로그인한 학부모 기기에는 새 학원을 자동 연결합니다.
    device_ids=list(db.scalars(select(ParentLink.device_id).where(ParentLink.student_id==s.id).distinct()).all())
    for did in device_ids:
        if not db.scalar(select(ParentLink).where(ParentLink.device_id==did,ParentLink.student_id==s.id,ParentLink.academy_id==auth["academy_id"])):
            db.add(ParentLink(device_id=did,student_id=s.id,academy_id=auth["academy_id"]))
    sync_parent_family_links(db,s.phone_last4); db.commit(); return {"student_id":s.id,"name":s.name,"phone_last4":s.phone_last4,"extra_code":sa.login_extra_code}
@app.get("/api/v3/admin/students")
def students(q:str="",auth=Depends(admin_auth),db:Session=Depends(get_db)):
    stmt=select(StudentAcademy,Student).join(Student,Student.id==StudentAcademy.student_id).where(StudentAcademy.academy_id==auth["academy_id"],StudentAcademy.is_active.is_(True))
    if q.strip(): stmt=stmt.where(or_(Student.name.ilike(f"%{q.strip()}%"),Student.phone_last4.ilike(f"%{q.strip()}%"),StudentAcademy.attendance_pin.ilike(f"%{q.strip()}%"),StudentAcademy.memo.ilike(f"%{q.strip()}%")))
    rows=db.execute(stmt.order_by(Student.name).limit(1000)).all()
    return [{"student_id":s.id,"link_id":sa.id,"name":s.name,"phone_last4":s.phone_last4,"attendance_pin":sa.attendance_pin,"memo":sa.memo,"extra_code":sa.login_extra_code,"nfc_registered":s.nfc_active} for sa,s in rows]
@app.put("/api/v3/admin/students/{student_id}")
def edit_student(student_id:int,r:EditStudent,auth=Depends(admin_auth),db:Session=Depends(get_db)):
    sa=db.scalar(select(StudentAcademy).where(StudentAcademy.student_id==student_id,StudentAcademy.academy_id==auth["academy_id"],StudentAcademy.is_active.is_(True))); s=db.get(Student,student_id)
    if not sa or not s: raise HTTPException(404,"학생을 찾을 수 없습니다.")
    phone=digits(r.phone_last4,11,"보호자 전화번호"); pin=digits(r.attendance_pin,4,"출석번호")
    dup=db.scalar(select(StudentAcademy).where(StudentAcademy.academy_id==auth["academy_id"],StudentAcademy.attendance_pin==pin,StudentAcademy.id!=sa.id,StudentAcademy.is_active.is_(True)))
    if dup: raise HTTPException(409,"이 학원에서 이미 사용 중인 출석번호입니다.")
    links=db.scalar(select(func.count()).select_from(StudentAcademy).where(StudentAcademy.student_id==student_id,StudentAcademy.is_active.is_(True))) or 0
    global_changed=(s.name!=r.name.strip() or s.phone_last4!=phone)
    if global_changed and not r.confirm_global: raise HTTPException(409,"GLOBAL_CONFIRM_REQUIRED")
    old_name,old_phone=s.name,s.phone_last4
    phone_changed=(old_phone!=phone)
    family_students=list(db.scalars(select(Student).where(Student.phone_last4==old_phone)).all()) if phone_changed else [s]
    s.name=r.name.strip()
    now_value=now_kst().astimezone(timezone.utc)
    if phone_changed:
        for family_student in family_students:
            family_student.phone_last4=phone
            family_student.updated_at=now_value
    else:
        s.updated_at=now_value
    sa.attendance_pin=pin; sa.memo=r.memo.strip()
    affected_pairs=set()
    for family_student in family_students:
        for aid in db.scalars(select(StudentAcademy.academy_id).where(StudentAcademy.student_id==family_student.id,StudentAcademy.is_active.is_(True))).all():
            affected_pairs.add((aid,family_student.name))
    if old_name!=s.name:
        affected_pairs.add((auth["academy_id"],old_name))
        affected_pairs.add((auth["academy_id"],s.name))
    for aid,student_name in affected_pairs:
        refresh_duplicate_codes(db,aid,student_name,phone if phone_changed else old_phone)
    if phone_changed:
        sync_parent_family_links(db,phone)
    db.commit(); return {"ok":True,"global_updated":global_changed,"linked_academies":links,"family_phone_updated":len(family_students) if phone_changed else 0}
@app.post("/api/v3/admin/students/{student_id}/replace-nfc")
def replace_nfc(student_id:int,r:NfcReplace,auth=Depends(admin_auth),db:Session=Depends(get_db)):
    sa=db.scalar(select(StudentAcademy).where(StudentAcademy.student_id==student_id,StudentAcademy.academy_id==auth["academy_id"],StudentAcademy.is_active.is_(True))); s=db.get(Student,student_id)
    if not sa or not s: raise HTTPException(404,"학생을 찾을 수 없습니다.")
    nt=r.new_nfc_token.strip()
    if not nt: raise HTTPException(400,"새 NFC 토큰이 없습니다.")
    if db.scalar(select(Student).where(Student.nfc_token==nt,Student.id!=s.id)): raise HTTPException(409,"이미 다른 학생에게 등록된 NFC 카드입니다.")
    s.nfc_token=nt; s.nfc_active=True; s.updated_at=now_kst().astimezone(timezone.utc); db.commit(); return {"ok":True}
@app.delete("/api/v3/admin/students/{student_id}")
def remove_from_academy(student_id:int,delete_global:bool=False,auth=Depends(admin_auth),db:Session=Depends(get_db)):
    sa=db.scalar(select(StudentAcademy).where(StudentAcademy.student_id==student_id,StudentAcademy.academy_id==auth["academy_id"],StudentAcademy.is_active.is_(True))); s=db.get(Student,student_id)
    if not sa or not s: raise HTTPException(404,"학생을 찾을 수 없습니다.")
    other=db.scalar(select(func.count()).select_from(StudentAcademy).where(StudentAcademy.student_id==student_id,StudentAcademy.is_active.is_(True),StudentAcademy.academy_id!=auth["academy_id"])) or 0
    if delete_global and other>0: raise HTTPException(409,"다른 학원에 등록되어 있어 NFC/학생 전체삭제를 할 수 없습니다.")
    if delete_global:
        db.delete(s); db.commit(); return {"ok":True,"global_deleted":True,"nfc_reusable":True}
    sa.is_active=False; db.commit(); return {"ok":True,"global_deleted":False,"other_academies":other,"nfc_reusable":other==0}

@app.post("/api/v3/admin/attendance/manual")
def manual_attendance(r:ManualAttendanceReq,auth=Depends(admin_auth),db:Session=Depends(get_db)):
    if r.event_type not in {"IN","OUT"}:
        raise HTTPException(400,"입실 또는 퇴실을 선택해주세요.")

    sa=db.scalar(select(StudentAcademy).where(
        StudentAcademy.student_id==r.student_id,
        StudentAcademy.academy_id==auth["academy_id"],
        StudentAcademy.is_active.is_(True)
    ))
    student=db.get(Student,r.student_id)
    if not sa or not student:
        raise HTTPException(404,"이 학원에 등록된 학생이 아닙니다.")

    occurred=to_utc(r.occurred_at)

    now=now_kst().astimezone(timezone.utc)
    if occurred > now + timedelta(minutes=1):
        raise HTTPException(400,"미래 시간으로 출석을 등록할 수 없습니다.")

    local=to_kst(occurred)

    # 관리자 수동출석도 마지막 출석 처리 후 10분 동안 재처리를 막습니다.
    # 다음 처리 가능 유형(IN/OUT)을 함께 내려 앱에서 남은 시간 안내를 표시합니다.
    previous=db.scalar(
        select(AttendanceEvent).where(
            AttendanceEvent.academy_id==auth["academy_id"],
            AttendanceEvent.student_id==student.id,
            AttendanceEvent.occurred_at<=occurred
        ).order_by(AttendanceEvent.occurred_at.desc()).limit(1)
    )
    if previous:
        sec=(occurred-previous.occurred_at).total_seconds()
        if sec<LOCKOUT_SECONDS:
            remain=max(1,int((LOCKOUT_SECONDS-sec+59)//60))
            next_type="OUT" if previous.event_type=="IN" else "IN"
            raise HTTPException(409,f"DUPLICATE_WAIT:{remain}:{next_type}")

    event=AttendanceEvent(
        academy_id=auth["academy_id"],
        student_id=student.id,
        student_academy_id=sa.id,
        event_type=r.event_type,
        source="MANUAL",
        occurred_at=occurred
    )
    db.add(event)
    db.commit()
    db.refresh(event)

    academy=db.get(Academy,auth["academy_id"])
    action="입실" if r.event_type=="IN" else "퇴실"
    elapsed=max(0,int((now-occurred).total_seconds()//60))

    if elapsed < 1:
        relative=f"방금 {action}했습니다."
    elif elapsed < 60:
        relative=f"{elapsed}분 전에 {action}했습니다."
    else:
        hours=elapsed//60
        mins=elapsed%60
        relative=(f"{hours}시간 {mins}분 전에 {action}했습니다."
                  if mins else f"{hours}시간 전에 {action}했습니다.")

    when=local.strftime("%H:%M")
    tokens=list(db.scalars(
        select(ParentDevice.push_token)
        .join(ParentLink,ParentLink.device_id==ParentDevice.id)
        .where(
            ParentLink.student_id==student.id,
            ParentLink.academy_id==academy.id,
            ParentDevice.push_token.is_not(None)
        )
    ).all())

    send_push(
        tokens,
        academy.name,
        f"{student.name} 학생이 {relative} (등록시간 {when})",
        {
            "student_id":str(student.id),
            "academy_id":str(academy.id),
            "event_type":r.event_type,
            "source":"MANUAL"
        }
    )

    return {
        "ok":True,
        "message":f"{student.name} 학생 {when} {action} 등록 완료 · 학부모 알림 즉시 전송",
        "occurred_at":to_kst(event.occurred_at).isoformat()
    }

@app.post("/api/v3/attendance/check")
def attendance(r:AttendanceReq,auth=Depends(admin_auth),db:Session=Depends(get_db)):
    if bool(r.nfc_token) == bool(r.attendance_pin): raise HTTPException(400,"NFC 또는 4자리 출석번호 중 하나만 입력하세요.")
    if r.nfc_token:
        s=db.scalar(select(Student).where(Student.nfc_token==r.nfc_token.strip(),Student.nfc_active.is_(True)))
        if not s: raise HTTPException(404,"등록된 NFC 카드가 아닙니다.")
        sa=db.scalar(select(StudentAcademy).where(
            StudentAcademy.student_id==s.id,
            StudentAcademy.academy_id==auth["academy_id"],
            StudentAcademy.is_active.is_(True)
        ))
        if not sa:
            # 기존 NFC 학생이 새로운 학원에서 처음 태그하면 자동으로 현재 학원에 연결합니다.
            used=set(db.scalars(select(StudentAcademy.attendance_pin).where(
                StudentAcademy.academy_id==auth["academy_id"],
                StudentAcademy.is_active.is_(True)
            )).all())
            pin=str(random.randint(0,9999)).zfill(4)
            while pin in used:
                pin=str(random.randint(0,9999)).zfill(4)
            sa=StudentAcademy(
                student_id=s.id,
                academy_id=auth["academy_id"],
                attendance_pin=pin,
                memo=""
            )
            db.add(sa)
            db.flush()
            refresh_duplicate_codes(db,auth["academy_id"],s.name,s.phone_last4)

            # 이미 이 학생으로 로그인한 학부모 기기에는 현재 학원을 자동 추가합니다.
            device_ids=list(db.scalars(
                select(ParentLink.device_id).where(ParentLink.student_id==s.id).distinct()
            ).all())
            for did in device_ids:
                exists=db.scalar(select(ParentLink).where(
                    ParentLink.device_id==did,
                    ParentLink.student_id==s.id,
                    ParentLink.academy_id==auth["academy_id"]
                ))
                if not exists:
                    db.add(ParentLink(
                        device_id=did,
                        student_id=s.id,
                        academy_id=auth["academy_id"]
                    ))
            sync_parent_family_links(db,s.phone_last4)
            db.flush()
        source="NFC"
    else:
        pin=digits(r.attendance_pin or "",4,"출석번호"); row=db.execute(select(StudentAcademy,Student).join(Student,Student.id==StudentAcademy.student_id).where(StudentAcademy.academy_id==auth["academy_id"],StudentAcademy.attendance_pin==pin,StudentAcademy.is_active.is_(True))).first()
        if not row: raise HTTPException(404,"등록된 출석번호가 아닙니다.")
        sa,s=row; source="PIN"
    if not sa: raise HTTPException(403,"이 학원에 등록되지 않은 학생입니다.")
    now=now_kst().astimezone(timezone.utc)
    academy=db.get(Academy,auth["academy_id"])
    last=db.scalar(select(AttendanceEvent).where(
        AttendanceEvent.student_id==s.id,
        AttendanceEvent.academy_id==auth["academy_id"]
    ).order_by(AttendanceEvent.occurred_at.desc()).limit(1))
    if last:
        # 24시간 운영 학원은 기존 방식 그대로 마지막 상태를 이어갑니다.
        # 영업시간을 사용하는 학원은 새 영업일이 시작되면 전날 상태와 무관하게 입실부터 시작합니다.
        new_business_day=(not getattr(academy,"is_24_hours",True)) and last.occurred_at < academy_business_start_utc(academy,now)
        if new_business_day:
            typ="IN"
        else:
            sec=(now-last.occurred_at).total_seconds()
            if sec<LOCKOUT_SECONDS:
                remain=max(1,int((LOCKOUT_SECONDS-sec+59)//60))
                next_type="OUT" if last.event_type=="IN" else "IN"
                raise HTTPException(409,f"DUPLICATE_WAIT:{remain}:{next_type}")
            typ="OUT" if last.event_type=="IN" else "IN"
    else:
        typ="IN"
    e=AttendanceEvent(academy_id=auth["academy_id"],student_id=s.id,student_academy_id=sa.id,event_type=typ,source=source,occurred_at=now); db.add(e); db.commit(); db.refresh(e)
    a=db.get(Academy,auth["academy_id"]); action="입실" if typ=="IN" else "퇴실"; when=to_kst(e.occurred_at).strftime("%H:%M")
    tokens=list(db.scalars(select(ParentDevice.push_token).join(ParentLink,ParentLink.device_id==ParentDevice.id).where(ParentLink.student_id==s.id,ParentLink.academy_id==a.id,ParentDevice.push_token.is_not(None))).all())
    send_push(tokens,a.name,f"{s.name} 학생이 {when} {action}했습니다.",{"student_id":str(s.id),"academy_id":str(a.id),"event_type":typ})
    return {"ok":True,"student_id":s.id,"student_name":s.name,"event_type":typ,"source":source,"occurred_at":to_kst(e.occurred_at).isoformat(),"lockout_minutes":10}

@app.post("/api/v3/parent/login")
def parent_login(r:ParentLoginReq,db:Session=Depends(get_db)):
    a=active_academy(db,r.academy_id); phone=digits(r.phone_last4,11,"보호자 전화번호")
    rows=db.execute(select(StudentAcademy,Student).join(Student,Student.id==StudentAcademy.student_id).where(StudentAcademy.academy_id==a.id,StudentAcademy.is_active.is_(True),Student.name==r.name.strip(),Student.phone_last4==phone)).all()
    if not rows: raise HTTPException(401,"등록된 학생 정보를 확인해 주세요.")
    if len(rows)>1:
        if not r.extra_code: return {"needs_extra_code":True,"message":"동일한 이름과 전화번호 뒷자리를 가진 학생이 있습니다."}
        rows=[x for x in rows if x[0].login_extra_code==r.extra_code.strip()]
        if len(rows)!=1: raise HTTPException(401,"추가코드가 올바르지 않습니다.")
    sa,s=rows[0]
    d=db.scalar(select(ParentDevice).where(ParentDevice.installation_id==r.installation_id,ParentDevice.platform==r.platform))
    if not d: d=ParentDevice(installation_id=r.installation_id,platform=r.platform,push_token=r.push_token); db.add(d); db.flush()
    else: d.push_token=r.push_token or d.push_token; d.updated_at=now_kst().astimezone(timezone.utc)
    sync_parent_family_links(db,phone,d.id)
    db.commit(); return {"needs_extra_code":False,"student_id":s.id,"student_name":s.name,"academy_id":a.id,"academy_name":a.name,"access_token":token("parent",device_id=d.id)}
@app.post("/api/v3/parent/device/push-token")
def update_parent_push_token(r:PushTokenReq,auth=Depends(parent_auth),db:Session=Depends(get_db)):
    d=db.get(ParentDevice,auth["device_id"])
    if not d: raise HTTPException(404,"등록된 기기를 찾을 수 없습니다.")
    value=r.push_token.strip()
    if not value: raise HTTPException(400,"푸시 토큰이 없습니다.")
    d.push_token=value
    d.updated_at=now_kst().astimezone(timezone.utc)
    db.commit()
    return {"ok":True}

@app.get("/api/v3/parent/links")
def parent_links(auth=Depends(parent_auth),db:Session=Depends(get_db)):
    phones=list(db.scalars(
        select(Student.phone_last4)
        .join(ParentLink,ParentLink.student_id==Student.id)
        .where(ParentLink.device_id==auth["device_id"])
        .distinct()
    ).all())
    for phone in phones:
        sync_parent_family_links(db,phone,auth["device_id"])
    db.commit()
    rows=db.execute(select(ParentLink,Student,Academy).join(Student,Student.id==ParentLink.student_id).join(Academy,Academy.id==ParentLink.academy_id).join(StudentAcademy,and_(StudentAcademy.student_id==ParentLink.student_id,StudentAcademy.academy_id==ParentLink.academy_id)).where(ParentLink.device_id==auth["device_id"],Academy.is_active.is_(True),StudentAcademy.is_active.is_(True))).all()
    return [{"student_id":s.id,"student_name":s.name,"academy_id":a.id,"academy_name":a.name} for l,s,a in rows]
def month_bounds(y,m):
    if m<1 or m>12: raise HTTPException(400,"월이 올바르지 않습니다.")
    start=datetime(y,m,1,tzinfo=KST).astimezone(timezone.utc); end=(datetime(y,m,monthrange(y,m)[1],tzinfo=KST)+timedelta(days=1)).astimezone(timezone.utc); return start,end
@app.get("/api/v3/parent/attendance")
def parent_attendance(year:int,month:int,auth=Depends(parent_auth),db:Session=Depends(get_db)):
    start,end=month_bounds(year,month); permitted=select(ParentLink.student_id).where(ParentLink.device_id==auth["device_id"])
    rows=db.execute(select(AttendanceEvent,Student,Academy).join(Student,Student.id==AttendanceEvent.student_id).join(Academy,Academy.id==AttendanceEvent.academy_id).where(AttendanceEvent.student_id.in_(permitted),AttendanceEvent.occurred_at>=start,AttendanceEvent.occurred_at<end).order_by(AttendanceEvent.occurred_at)).all()
    return [{"student_id":s.id,"student_name":s.name,"academy_id":a.id,"academy_name":a.name,"event_type":e.event_type,"occurred_at":to_kst(e.occurred_at).isoformat()} for e,s,a in rows]
@app.delete("/api/v3/admin/attendance/{event_id}")
def delete_attendance(event_id:int,auth=Depends(admin_auth),db:Session=Depends(get_db)):
    event=db.get(AttendanceEvent,event_id)
    if not event or event.academy_id!=auth["academy_id"]:
        raise HTTPException(404,"출석기록을 찾을 수 없습니다.")
    db.delete(event)
    db.commit()
    return {"ok":True}

@app.get("/api/v3/admin/attendance")
def admin_attendance(year:int,month:int,q:str="",auth=Depends(admin_auth),db:Session=Depends(get_db)):
    start,end=month_bounds(year,month); stmt=select(AttendanceEvent,Student).join(Student,Student.id==AttendanceEvent.student_id).where(AttendanceEvent.academy_id==auth["academy_id"],AttendanceEvent.occurred_at>=start,AttendanceEvent.occurred_at<end)
    if q.strip(): stmt=stmt.where(or_(Student.name.ilike(f"%{q.strip()}%"),Student.phone_last4.ilike(f"%{q.strip()}%")))
    rows=db.execute(stmt.order_by(AttendanceEvent.occurred_at.desc()).limit(10000)).all(); return [{"id":e.id,"student_id":s.id,"student_name":s.name,"phone_last4":s.phone_last4,"event_type":e.event_type,"source":e.source,"occurred_at":to_kst(e.occurred_at).isoformat()} for e,s in rows]

@app.post("/api/v3/academy-management/verify")
async def manage_verify(r:KeyReq):
    if "kyh" not in r.license_key.lower(): raise HTTPException(401,"학원관리 권한이 없는 인증키입니다.")
    try: ok=await verify_license_key(r.license_key)
    except AuthUnavailable as e: raise HTTPException(503,str(e))
    if not ok: raise HTTPException(401,"유효하지 않은 인증키입니다.")
    return {"management_token":token("academy_management")}
@app.get("/api/v3/academy-management/list")
def manage_list(management_token:str,db:Session=Depends(get_db)):
    read_token(management_token,"academy_management",600); rows=db.scalars(select(Academy).order_by(Academy.region,Academy.district,Academy.name)).all(); return [{"id":a.id,"name":a.name,"region":a.region,"district":a.district,"is_active":a.is_active} for a in rows]
@app.post("/api/v3/academy-management/update")
def manage_update(r:AcademyUpdate,db:Session=Depends(get_db)):
    read_token(r.management_token,"academy_management",600); a=db.get(Academy,r.academy_id)
    if not a: raise HTTPException(404,"학원을 찾을 수 없습니다.")
    if r.name is not None:a.name=r.name.strip()
    if r.region is not None:a.region=r.region.strip()
    if r.district is not None:a.district=r.district.strip()
    if r.is_active is not None:a.is_active=r.is_active
    db.commit(); return {"ok":True}
@app.post("/api/v3/academy-management/delete")
def academy_management_delete(r:ManageReq,db:Session=Depends(get_db)):
    read_token(r.management_token,"academy_management",60*30)
    academy=db.get(Academy,r.academy_id)
    if not academy: raise HTTPException(404,"학원을 찾을 수 없습니다.")

    # 이 학원 출석/공지/학부모연결/학원별 학생연결 등 학원 종속 데이터 삭제
    db.query(AttendanceEvent).filter(AttendanceEvent.academy_id==academy.id).delete(synchronize_session=False)
    if "ParentLink" in globals():
        db.query(ParentLink).filter(ParentLink.academy_id==academy.id).delete(synchronize_session=False)

    links=list(db.scalars(select(StudentAcademy).where(StudentAcademy.academy_id==academy.id)).all())
    affected_student_ids=[x.student_id for x in links]
    for link in links:
        db.delete(link)
    db.flush()

    # 다른 학원과 중복 연결된 학생은 유지.
    # 이 학원이 마지막 연결이었던 학생만 전역 학생정보/NFC까지 삭제.
    for student_id in affected_student_ids:
        still_linked=db.scalar(select(StudentAcademy.id).where(
            StudentAcademy.student_id==student_id,
            StudentAcademy.is_active.is_(True)
        ))
        if not still_linked:
            student=db.get(Student,student_id)
            if student:
                if "ParentLink" in globals():
                    db.query(ParentLink).filter(ParentLink.student_id==student_id).delete(synchronize_session=False)
                db.delete(student)

    db.delete(academy)
    db.commit()
    return {"ok":True}


@app.post("/api/v3/academy-management/notice")
def manage_notice(r:NoticeWrite,db:Session=Depends(get_db)):
    read_token(r.management_token,"academy_management",600)
    if r.notice_type not in {"regular","emergency"}: raise HTTPException(400,"공지 종류가 올바르지 않습니다.")
    n=db.get(Notice,r.notice_type) or Notice(notice_type=r.notice_type); n.content=r.content.strip(); n.is_active=r.is_active; n.updated_at=now_kst().astimezone(timezone.utc); db.add(n); db.commit(); return {"ok":True}