import os

def send_push(tokens,title,body,data=None):
    tokens=[t for t in tokens if t]
    if not tokens: return {"sent":0,"configured":False}
    try:
        import firebase_admin
        from firebase_admin import credentials,messaging
        if not firebase_admin._apps:
            path=os.getenv("FIREBASE_CREDENTIALS")
            if not path: return {"sent":0,"configured":False}
            firebase_admin.initialize_app(credentials.Certificate(path))
        sent=0
        for t in tokens:
            try:
                messaging.send(messaging.Message(token=t,notification=messaging.Notification(title=title,body=body),data=data or {})); sent+=1
            except Exception: pass
        return {"sent":sent,"configured":True}
    except Exception:
        return {"sent":0,"configured":False}
