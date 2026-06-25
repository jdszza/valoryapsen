"""
APSEN - Autenticação JWT (IHM de Manutenção)
Apenas técnicos de manutenção precisam de login (para registrar intervenções).
O dashboard é read-only e não requer autenticação.
"""
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt as _bcrypt
from jose import JWTError, jwt

from config import settings

ALGORITHM       = "HS256"
TOKEN_EXP_HOURS = 8


def hash_senha(senha: str) -> str:
    return _bcrypt.hashpw(senha.encode("utf-8"), _bcrypt.gensalt()).decode("utf-8")


def verificar_senha(senha: str, hash_: str) -> bool:
    return _bcrypt.checkpw(senha.encode("utf-8"), hash_.encode("utf-8"))


def criar_token(username: str, nome: str, role: str = "manutencao") -> str:
    expira  = datetime.now(timezone.utc) + timedelta(hours=TOKEN_EXP_HOURS)
    payload = {"sub": username, "nome": nome, "role": role, "exp": expira}
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=ALGORITHM)


def decodificar_token(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, settings.SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        return None
