from datetime import datetime, timezone, timedelta
from calendar import monthrange
import secrets, random
from fastapi import FastAPI, Depends, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy import select, or_, func
from sqlalchemy.orm import Session
from db import Base, engine, get_db
from models import Academy,AdminCredential,Student,StudentAcademy,AttendanceEvent,ParentDevice,ParentLink,Notice
from security import hash_password,verify_password,token,read_token
from auth_adapter import verify_license_key,AuthUnavailable
from push import send_push
KST=timezone(timedelta(hours=9)); LOCKOUT_SECONDS=600
app=FastAPI(title="CodeNote Attendance V3 API",version="3.0.0")
app.add_middleware(CORSMiddleware,allow_origins=["*"],allow_methods=["*"],allow_headers=["*"])
@app.on_event("startup")
def startup():
    Base.metadata.create_all(engine)
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
def admin_auth(authorization:str|None=Header(default=None)): return read_token(bearer(authorization),"admin",60*60*24)
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

class KeyReq(BaseModel): license_key:str
class AcademyCreate(BaseModel): registration_token:str; name:str; region:str; district:str; admin_password:str=Field(min_length=4); recovery_name:str; recovery_phone_last4:str
class AdminLoginReq(BaseModel): academy_id:int; password:str
class ParentLoginReq(BaseModel): academy_id:int; name:str; phone_last4:str; extra_code:str|None=None; installation_id:str; platform:str="android"; push_token:str|None=None
class ChangePw(BaseModel): current_password:str; new_password:str=Field(min_length=4)
class RecoveryVerify(BaseModel): academy_id:int; recovery_name:str; recovery_phone_last4:str
class ResetPw(BaseModel): recovery_token:str; new_password:str=Field(min_length=4)
class NewStudentNfc(BaseModel): name:str; phone_last4:str; attendance_pin:str; memo:str=""; nfc_token:str
class AttachExisting(BaseModel): nfc_token:str; attendance_pin:str; memo:str=""
class EditStudent(BaseModel): name:str; phone_last4:str; attendance_pin:str; memo:str=""; confirm_global:bool=False
class NfcLookup(BaseModel): nfc_token:str
class NfcReplace(BaseModel): new_nfc_token:str
class AttendanceReq(BaseModel): nfc_token:str|None=None; attendance_pin:str|None=None
class NoticeWrite(BaseModel): management_token:str; notice_type:str; content:str; is_active:bool
class AcademyUpdate(BaseModel): management_token:str; academy_id:int; name:str|None=None; region:str|None=None; district:str|None=None; is_active:bool|None=None
class ManageReq(BaseModel): management_token:str; academy_id:int

@app.get("/")
def health(): return {"ok":True,"service":"codenote-attendance-v3"}
@app.get("/api/v3/notices")
def notices(db:Session=Depends(get_db)): return [{"type":n.notice_type,"content":n.content,"is_active":n.is_active} for n in db.scalars(select(Notice)).all()]
@app.get("/api/v3/regions")
def regions(db:Session=Depends(get_db)): return list(db.scalars(select(Academy.region).where(Academy.is_active.is_(True)).distinct().order_by(Academy.region)).all())
@app.get("/api/v3/districts")
def districts(region:str,db:Session=Depends(get_db)): return list(db.scalars(select(Academy.district).where(Academy.is_active.is_(True),Academy.region==region).distinct().order_by(Academy.district)).all())
@app.get("/api/v3/academies")
def academies(region:str,district:str,q:str="",db:Session=Depends(get_db)):
    s=select(Academy).where(Academy.is_active.is_(True),Academy.region==region,Academy.district==district)
    if q.strip(): s=s.where(Academy.name.ilike(f"%{q.strip()}%"))
    return [{"id":a.id,"name":a.name,"region":a.region,"district":a.district} for a in db.scalars(s.order_by(Academy.name).limit(300)).all()]

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
def recovery(r:RecoveryVerify,db:Session=Depends(get_db)):
    a=active_academy(db,r.academy_id)
    if a.recovery_name!=r.recovery_name.strip() or a.recovery_phone_last4!=digits(r.recovery_phone_last4,4,"전화번호 뒷자리"): raise HTTPException(401,"등록정보가 일치하지 않습니다.")
    return {"recovery_token":token("recovery",academy_id=a.id)}
@app.post("/api/v3/admin/recovery/reset")
def recovery_reset(r:ResetPw,db:Session=Depends(get_db)):
    d=read_token(r.recovery_token,"recovery",600); c=db.get(AdminCredential,d["academy_id"]); c.password_hash=hash_password(r.new_password); db.commit(); return {"ok":True}

@app.post("/api/v3/admin/nfc/lookup")
def nfc_lookup(r:NfcLookup,auth=Depends(admin_auth),db:Session=Depends(get_db)):
    s=db.scalar(select(Student).where(Student.nfc_token==r.nfc_token.strip(),Student.nfc_active.is_(True)))
    if not s: return {"exists":False}
    own=db.scalar(select(StudentAcademy).where(StudentAcademy.student_id==s.id,StudentAcademy.academy_id==auth["academy_id"],StudentAcademy.is_active.is_(True)))
    return {"exists":True,"already_in_academy":bool(own),"student_id":s.id,"name":s.name,"phone_last4":s.phone_last4}
@app.post("/api/v3/admin/students/new-with-nfc")
def new_student(r:NewStudentNfc,auth=Depends(admin_auth),db:Session=Depends(get_db)):
    phone=digits(r.phone_last4,4,"전화번호 뒷자리"); pin=digits(r.attendance_pin,4,"출석번호"); nt=r.nfc_token.strip()
    if not nt: raise HTTPException(400,"NFC 카드 등록이 필요합니다.")
    if db.scalar(select(Student).where(Student.nfc_token==nt)): raise HTTPException(409,"이미 등록된 NFC 카드입니다. 기존 학생 불러오기를 사용하세요.")
    if db.scalar(select(StudentAcademy).where(StudentAcademy.academy_id==auth["academy_id"],StudentAcademy.attendance_pin==pin,StudentAcademy.is_active.is_(True))): raise HTTPException(409,"이 학원에서 이미 사용 중인 출석번호입니다.")
    s=Student(name=r.name.strip(),phone_last4=phone,nfc_token=nt); db.add(s); db.flush(); sa=StudentAcademy(student_id=s.id,academy_id=auth["academy_id"],attendance_pin=pin,memo=r.memo.strip()); db.add(sa); db.flush(); refresh_duplicate_codes(db,auth["academy_id"],s.name,s.phone_last4); db.commit(); return {"student_id":s.id,"student_academy_id":sa.id,"extra_code":sa.login_extra_code}
@app.post("/api/v3/admin/students/attach-existing")
def attach(r:AttachExisting,auth=Depends(admin_auth),db:Session=Depends(get_db)):
    pin=digits(r.attendance_pin,4,"출석번호"); s=db.scalar(select(Student).where(Student.nfc_token==r.nfc_token.strip(),Student.nfc_active.is_(True)))
    if not s: raise HTTPException(404,"등록된 NFC 학생이 아닙니다.")
    if db.scalar(select(StudentAcademy).where(StudentAcademy.student_id==s.id,StudentAcademy.academy_id==auth["academy_id"],StudentAcademy.is_active.is_(True))): raise HTTPException(409,"이미 이 학원에 등록된 학생입니다.")
    if db.scalar(select(StudentAcademy).where(StudentAcademy.academy_id==auth["academy_id"],StudentAcademy.attendance_pin==pin,StudentAcademy.is_active.is_(True))): raise HTTPException(409,"이 학원에서 이미 사용 중인 출석번호입니다.")
    sa=StudentAcademy(student_id=s.id,academy_id=auth["academy_id"],attendance_pin=pin,memo=r.memo.strip()); db.add(sa); db.flush(); refresh_duplicate_codes(db,auth["academy_id"],s.name,s.phone_last4); db.commit(); return {"student_id":s.id,"name":s.name,"phone_last4":s.phone_last4,"extra_code":sa.login_extra_code}
@app.get("/api/v3/admin/students")
def students(q:str="",auth=Depends(admin_auth),db:Session=Depends(get_db)):
    stmt=select(StudentAcademy,Student).join(Student,Student.id==StudentAcademy.student_id).where(StudentAcademy.academy_id==auth["academy_id"],StudentAcademy.is_active.is_(True))
    if q.strip(): stmt=stmt.where(or_(Student.name.ilike(f"%{q.strip()}%"),Student.phone_last4.ilike(f"%{q.strip()}%"),StudentAcademy.attendance_pin.ilike(f"%{q.strip()}%")))
    rows=db.execute(stmt.order_by(Student.name).limit(1000)).all()
    return [{"student_id":s.id,"link_id":sa.id,"name":s.name,"phone_last4":s.phone_last4,"attendance_pin":sa.attendance_pin,"memo":sa.memo,"extra_code":sa.login_extra_code,"nfc_registered":s.nfc_active} for sa,s in rows]
@app.put("/api/v3/admin/students/{student_id}")
def edit_student(student_id:int,r:EditStudent,auth=Depends(admin_auth),db:Session=Depends(get_db)):
    sa=db.scalar(select(StudentAcademy).where(StudentAcademy.student_id==student_id,StudentAcademy.academy_id==auth["academy_id"],StudentAcademy.is_active.is_(True))); s=db.get(Student,student_id)
    if not sa or not s: raise HTTPException(404,"학생을 찾을 수 없습니다.")
    phone=digits(r.phone_last4,4,"전화번호 뒷자리"); pin=digits(r.attendance_pin,4,"출석번호")
    dup=db.scalar(select(StudentAcademy).where(StudentAcademy.academy_id==auth["academy_id"],StudentAcademy.attendance_pin==pin,StudentAcademy.id!=sa.id,StudentAcademy.is_active.is_(True)))
    if dup: raise HTTPException(409,"이 학원에서 이미 사용 중인 출석번호입니다.")
    links=db.scalar(select(func.count()).select_from(StudentAcademy).where(StudentAcademy.student_id==student_id,StudentAcademy.is_active.is_(True))) or 0
    global_changed=(s.name!=r.name.strip() or s.phone_last4!=phone)
    if global_changed and links>1 and not r.confirm_global: raise HTTPException(409,"GLOBAL_CONFIRM_REQUIRED")
    old_name,old_phone=s.name,s.phone_last4; s.name=r.name.strip(); s.phone_last4=phone; s.updated_at=datetime.now(timezone.utc); sa.attendance_pin=pin; sa.memo=r.memo.strip(); refresh_duplicate_codes(db,auth["academy_id"],old_name,old_phone); refresh_duplicate_codes(db,auth["academy_id"],s.name,s.phone_last4); db.commit(); return {"ok":True,"global_updated":global_changed,"linked_academies":links}
@app.post("/api/v3/admin/students/{student_id}/replace-nfc")
def replace_nfc(student_id:int,r:NfcReplace,auth=Depends(admin_auth),db:Session=Depends(get_db)):
    sa=db.scalar(select(StudentAcademy).where(StudentAcademy.student_id==student_id,StudentAcademy.academy_id==auth["academy_id"],StudentAcademy.is_active.is_(True))); s=db.get(Student,student_id)
    if not sa or not s: raise HTTPException(404,"학생을 찾을 수 없습니다.")
    nt=r.new_nfc_token.strip()
    if not nt: raise HTTPException(400,"새 NFC 토큰이 없습니다.")
    if db.scalar(select(Student).where(Student.nfc_token==nt,Student.id!=s.id)): raise HTTPException(409,"이미 다른 학생에게 등록된 NFC 카드입니다.")
    s.nfc_token=nt; s.nfc_active=True; s.updated_at=datetime.now(timezone.utc); db.commit(); return {"ok":True}
@app.delete("/api/v3/admin/students/{student_id}")
def remove_from_academy(student_id:int,delete_global:bool=False,auth=Depends(admin_auth),db:Session=Depends(get_db)):
    sa=db.scalar(select(StudentAcademy).where(StudentAcademy.student_id==student_id,StudentAcademy.academy_id==auth["academy_id"],StudentAcademy.is_active.is_(True))); s=db.get(Student,student_id)
    if not sa or not s: raise HTTPException(404,"학생을 찾을 수 없습니다.")
    other=db.scalar(select(func.count()).select_from(StudentAcademy).where(StudentAcademy.student_id==student_id,StudentAcademy.is_active.is_(True),StudentAcademy.academy_id!=auth["academy_id"])) or 0
    if delete_global and other>0: raise HTTPException(409,"다른 학원에 등록되어 있어 NFC/학생 전체삭제를 할 수 없습니다.")
    if delete_global:
        db.delete(s); db.commit(); return {"ok":True,"global_deleted":True,"nfc_reusable":True}
    sa.is_active=False; db.commit(); return {"ok":True,"global_deleted":False,"other_academies":other,"nfc_reusable":other==0}

@app.post("/api/v3/attendance/check")
def attendance(r:AttendanceReq,auth=Depends(admin_auth),db:Session=Depends(get_db)):
    if bool(r.nfc_token) == bool(r.attendance_pin): raise HTTPException(400,"NFC 또는 4자리 출석번호 중 하나만 입력하세요.")
    if r.nfc_token:
        s=db.scalar(select(Student).where(Student.nfc_token==r.nfc_token.strip(),Student.nfc_active.is_(True)))
        if not s: raise HTTPException(404,"등록된 NFC 카드가 아닙니다.")
        sa=db.scalar(select(StudentAcademy).where(StudentAcademy.student_id==s.id,StudentAcademy.academy_id==auth["academy_id"],StudentAcademy.is_active.is_(True))); source="NFC"
    else:
        pin=digits(r.attendance_pin or "",4,"출석번호"); row=db.execute(select(StudentAcademy,Student).join(Student,Student.id==StudentAcademy.student_id).where(StudentAcademy.academy_id==auth["academy_id"],StudentAcademy.attendance_pin==pin,StudentAcademy.is_active.is_(True))).first()
        if not row: raise HTTPException(404,"등록된 출석번호가 아닙니다.")
        sa,s=row; source="PIN"
    if not sa: raise HTTPException(403,"이 학원에 등록되지 않은 학생입니다.")
    now=datetime.now(timezone.utc); last=db.scalar(select(AttendanceEvent).where(AttendanceEvent.student_id==s.id,AttendanceEvent.academy_id==auth["academy_id"]).order_by(AttendanceEvent.occurred_at.desc()).limit(1))
    if last:
        sec=(now-last.occurred_at).total_seconds()
        if sec<LOCKOUT_SECONDS: raise HTTPException(409,f"DUPLICATE_WAIT:{max(1,int((LOCKOUT_SECONDS-sec+59)//60))}")
        typ="OUT" if last.event_type=="IN" else "IN"
    else: typ="IN"
    e=AttendanceEvent(academy_id=auth["academy_id"],student_id=s.id,student_academy_id=sa.id,event_type=typ,source=source,occurred_at=now); db.add(e); db.commit(); db.refresh(e)
    a=db.get(Academy,auth["academy_id"]); action="입실" if typ=="IN" else "퇴실"; when=e.occurred_at.astimezone(KST).strftime("%H:%M")
    tokens=list(db.scalars(select(ParentDevice.push_token).join(ParentLink,ParentLink.device_id==ParentDevice.id).where(ParentLink.student_id==s.id,ParentLink.academy_id==a.id,ParentDevice.push_token.is_not(None))).all())
    send_push(tokens,a.name,f"{s.name} 학생이 {when} {action}했습니다.",{"student_id":str(s.id),"academy_id":str(a.id),"event_type":typ})
    return {"ok":True,"student_id":s.id,"student_name":s.name,"event_type":typ,"source":source,"occurred_at":e.occurred_at.isoformat(),"lockout_minutes":10}

@app.post("/api/v3/parent/login")
def parent_login(r:ParentLoginReq,db:Session=Depends(get_db)):
    a=active_academy(db,r.academy_id); phone=digits(r.phone_last4,4,"전화번호 뒷자리")
    rows=db.execute(select(StudentAcademy,Student).join(Student,Student.id==StudentAcademy.student_id).where(StudentAcademy.academy_id==a.id,StudentAcademy.is_active.is_(True),Student.name==r.name.strip(),Student.phone_last4==phone)).all()
    if not rows: raise HTTPException(401,"등록된 학생 정보를 확인해 주세요.")
    if len(rows)>1:
        if not r.extra_code: return {"needs_extra_code":True,"message":"동일한 이름과 전화번호 뒷자리를 가진 학생이 있습니다."}
        rows=[x for x in rows if x[0].login_extra_code==r.extra_code.strip()]
        if len(rows)!=1: raise HTTPException(401,"추가코드가 올바르지 않습니다.")
    sa,s=rows[0]
    d=db.scalar(select(ParentDevice).where(ParentDevice.installation_id==r.installation_id,ParentDevice.platform==r.platform))
    if not d: d=ParentDevice(installation_id=r.installation_id,platform=r.platform,push_token=r.push_token); db.add(d); db.flush()
    else: d.push_token=r.push_token or d.push_token; d.updated_at=datetime.now(timezone.utc)
    if not db.scalar(select(ParentLink).where(ParentLink.device_id==d.id,ParentLink.student_id==s.id,ParentLink.academy_id==a.id)): db.add(ParentLink(device_id=d.id,student_id=s.id,academy_id=a.id))
    db.commit(); return {"needs_extra_code":False,"student_id":s.id,"student_name":s.name,"academy_id":a.id,"academy_name":a.name,"access_token":token("parent",device_id=d.id)}
@app.get("/api/v3/parent/links")
def parent_links(auth=Depends(parent_auth),db:Session=Depends(get_db)):
    rows=db.execute(select(ParentLink,Student,Academy).join(Student,Student.id==ParentLink.student_id).join(Academy,Academy.id==ParentLink.academy_id).where(ParentLink.device_id==auth["device_id"],Academy.is_active.is_(True))).all()
    return [{"student_id":s.id,"student_name":s.name,"academy_id":a.id,"academy_name":a.name} for l,s,a in rows]
def month_bounds(y,m):
    if m<1 or m>12: raise HTTPException(400,"월이 올바르지 않습니다.")
    start=datetime(y,m,1,tzinfo=KST).astimezone(timezone.utc); end=(datetime(y,m,monthrange(y,m)[1],tzinfo=KST)+timedelta(days=1)).astimezone(timezone.utc); return start,end
@app.get("/api/v3/parent/attendance")
def parent_attendance(year:int,month:int,auth=Depends(parent_auth),db:Session=Depends(get_db)):
    start,end=month_bounds(year,month); permitted=select(ParentLink.student_id).where(ParentLink.device_id==auth["device_id"])
    rows=db.execute(select(AttendanceEvent,Student,Academy).join(Student,Student.id==AttendanceEvent.student_id).join(Academy,Academy.id==AttendanceEvent.academy_id).where(AttendanceEvent.student_id.in_(permitted),AttendanceEvent.occurred_at>=start,AttendanceEvent.occurred_at<end).order_by(AttendanceEvent.occurred_at)).all()
    return [{"student_id":s.id,"student_name":s.name,"academy_id":a.id,"academy_name":a.name,"event_type":e.event_type,"occurred_at":e.occurred_at.isoformat()} for e,s,a in rows]
@app.get("/api/v3/admin/attendance")
def admin_attendance(year:int,month:int,q:str="",auth=Depends(admin_auth),db:Session=Depends(get_db)):
    start,end=month_bounds(year,month); stmt=select(AttendanceEvent,Student).join(Student,Student.id==AttendanceEvent.student_id).where(AttendanceEvent.academy_id==auth["academy_id"],AttendanceEvent.occurred_at>=start,AttendanceEvent.occurred_at<end)
    if q.strip(): stmt=stmt.where(or_(Student.name.ilike(f"%{q.strip()}%"),Student.phone_last4.ilike(f"%{q.strip()}%")))
    rows=db.execute(stmt.order_by(AttendanceEvent.occurred_at.desc()).limit(10000)).all(); return [{"student_id":s.id,"student_name":s.name,"phone_last4":s.phone_last4,"event_type":e.event_type,"source":e.source,"occurred_at":e.occurred_at.isoformat()} for e,s in rows]

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
def manage_delete(r:ManageReq,db:Session=Depends(get_db)):
    read_token(r.management_token,"academy_management",600); a=db.get(Academy,r.academy_id)
    if not a: raise HTTPException(404,"학원을 찾을 수 없습니다.")
    db.delete(a); db.commit(); return {"ok":True}
@app.post("/api/v3/academy-management/notice")
def manage_notice(r:NoticeWrite,db:Session=Depends(get_db)):
    read_token(r.management_token,"academy_management",600)
    if r.notice_type not in {"regular","emergency"}: raise HTTPException(400,"공지 종류가 올바르지 않습니다.")
    n=db.get(Notice,r.notice_type) or Notice(notice_type=r.notice_type); n.content=r.content.strip(); n.is_active=r.is_active; n.updated_at=datetime.now(timezone.utc); db.add(n); db.commit(); return {"ok":True}
