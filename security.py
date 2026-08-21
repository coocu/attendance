import os, time, jwt
from fastapi import HTTPException
from passlib.context import CryptContext
pwd=CryptContext(schemes=["bcrypt"],deprecated="auto")
SECRET=os.getenv("APP_SECRET","CHANGE-ME-IN-RENDER")
def hash_password(v): return pwd.hash(v)
def verify_password(v,h): return pwd.verify(v,h)
def token(kind,**data):
    p={"kind":kind,"iat":int(time.time()),**data}; return jwt.encode(p,SECRET,algorithm="HS256")
def read_token(t,kind,max_age=86400):
    try: p=jwt.decode(t,SECRET,algorithms=["HS256"])
    except Exception: raise HTTPException(401,"인증정보가 올바르지 않습니다.")
    if p.get("kind")!=kind or int(time.time())-int(p.get("iat",0))>max_age: raise HTTPException(401,"인증이 만료되었습니다.")
    return p
