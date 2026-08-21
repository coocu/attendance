import os, httpx
AUTH_SERVER=os.getenv("AUTH_SERVER_URL","").rstrip("/")
class AuthUnavailable(Exception): pass
async def verify_license_key(key:str)->bool:
    if not AUTH_SERVER: raise AuthUnavailable("AUTH_SERVER_URL이 설정되지 않았습니다.")
    try:
        async with httpx.AsyncClient(timeout=12) as c:
            r=await c.post(AUTH_SERVER+"/app/check",json={"key":key})
            if r.status_code>=500: raise AuthUnavailable("인증 서버에 연결할 수 없습니다.")
            if r.status_code>=400: return False
            j=r.json()
            return bool(j.get("ok",j.get("valid",j.get("success",False))))
    except AuthUnavailable: raise
    except Exception as e: raise AuthUnavailable(f"인증 서버 오류: {e}")
