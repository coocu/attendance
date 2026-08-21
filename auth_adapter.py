import os
import httpx

# 기존 음악학원 예시 서버와 동일한 인증키 서버/프로토콜 사용
AUTH_URL = os.getenv(
    "POCKET_AUTH_URL",
    "https://poketserver.onrender.com/app/check"
).strip()


class AuthUnavailable(Exception):
    pass


async def verify_license_key(key: str) -> bool:
    """
    기존 인증키 서버와 동일:
    POST https://poketserver.onrender.com/app/check
    JSON: {"code": "<인증키>"}

    2xx 응답에서 token 값이 비어 있지 않을 때만 성공.
    """
    candidate = key.strip()
    if not candidate:
        return False

    if not AUTH_URL:
        raise AuthUnavailable("인증 서버 주소가 설정되지 않았습니다.")

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                AUTH_URL,
                json={"code": candidate},
                headers={"Content-Type": "application/json"},
            )
    except httpx.HTTPError as exc:
        raise AuthUnavailable("인증 서버에 연결할 수 없습니다.") from exc

    if not (200 <= response.status_code <= 299):
        return False

    try:
        data = response.json()
    except ValueError as exc:
        raise AuthUnavailable("인증 서버 응답 형식이 올바르지 않습니다.") from exc

    token = data.get("token")
    return token is not None and bool(str(token).strip())
