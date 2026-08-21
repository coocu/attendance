import os, re, time, json
import jwt
import httpx

_HEX_APNS = re.compile(r"^[0-9a-fA-F]{64,}$")
_apns_cached_token = None
_apns_cached_at = 0

def _apns_jwt():
    global _apns_cached_token, _apns_cached_at
    now=int(time.time())
    if _apns_cached_token and now-_apns_cached_at < 50*60:
        return _apns_cached_token

    key_id=os.getenv("APNS_KEY_ID","").strip()
    team_id=os.getenv("APNS_TEAM_ID","").strip()
    private_key=os.getenv("APNS_PRIVATE_KEY","").replace("\\n","\n").strip()
    key_path=os.getenv("APNS_KEY_PATH","").strip()

    if not private_key and key_path:
        try:
            with open(key_path,"r",encoding="utf-8") as f:
                private_key=f.read().strip()
        except Exception:
            private_key=""

    if not key_id or not team_id or not private_key:
        return None

    _apns_cached_token=jwt.encode(
        {"iss":team_id,"iat":now},
        private_key,
        algorithm="ES256",
        headers={"kid":key_id}
    )
    _apns_cached_at=now
    return _apns_cached_token

def _send_apns(device_token,title,body,data=None):
    auth=_apns_jwt()
    bundle=os.getenv("APNS_BUNDLE_ID","com.codenote.attendance").strip()
    if not auth or not bundle:
        return False

    sandbox=os.getenv("APNS_SANDBOX","true").lower() in ("1","true","yes","on")
    host="https://api.sandbox.push.apple.com" if sandbox else "https://api.push.apple.com"
    payload={
        "aps":{
            "alert":{"title":title,"body":body},
            "sound":"default"
        }
    }
    if data:
        payload.update({str(k):str(v) for k,v in data.items()})

    try:
        with httpx.Client(http2=True,timeout=15.0) as client:
            r=client.post(
                f"{host}/3/device/{device_token}",
                headers={
                    "authorization":f"bearer {auth}",
                    "apns-topic":bundle,
                    "apns-push-type":"alert",
                    "apns-priority":"10"
                },
                json=payload
            )
        return r.status_code == 200
    except Exception:
        return False

def _send_fcm(token,title,body,data=None):
    try:
        import firebase_admin
        from firebase_admin import credentials,messaging
        if not firebase_admin._apps:
            path=os.getenv("FIREBASE_CREDENTIALS","").strip()

            # Render Secret File을 firebase-service-account.json 이름으로 만든 경우도 자동 인식.
            if not path:
                default_path="/etc/secrets/firebase-service-account.json"
                if os.path.exists(default_path):
                    path=default_path

            if not path or not os.path.exists(path):
                print("[FCM] Firebase credentials file not found:", path or "(empty)", flush=True)
                return False

            firebase_admin.initialize_app(credentials.Certificate(path))

        message_id=messaging.send(
            messaging.Message(
                token=token,
                notification=messaging.Notification(title=title,body=body),
                data={str(k):str(v) for k,v in (data or {}).items()}
            )
        )
        print("[FCM] sent:", message_id, flush=True)
        return True
    except Exception as exc:
        print("[FCM] send failed:", repr(exc), flush=True)
        return False

def send_push(tokens,title,body,data=None):
    tokens=[str(t).strip() for t in tokens if t and str(t).strip()]
    sent=0
    for t in tokens:
        # iOS 앱은 APNs device token(hex)을 저장하고 Android/기존 구성은 FCM을 유지합니다.
        if _HEX_APNS.fullmatch(t):
            ok=_send_apns(t,title,body,data)
        else:
            ok=_send_fcm(t,title,body,data)
        if ok:
            sent+=1
    return {"sent":sent,"configured":bool(tokens)}
