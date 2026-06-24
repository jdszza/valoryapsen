"""
APSEN - Módulo de Autenticação
JWT + bcrypt | Hierarquia: admin > manutencao > operador
"""
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt as _bcrypt
from jose import JWTError, jwt

from config import settings

ALGORITHM       = "HS256"
TOKEN_EXP_HOURS = 8

ROLES = {
    "admin":      {"label": "Administrador", "nivel": 3},
    "manutencao": {"label": "Manutenção",    "nivel": 2},
    "operador":   {"label": "Operador",      "nivel": 1},
}


def hash_senha(senha: str) -> str:
    return _bcrypt.hashpw(senha.encode("utf-8"), _bcrypt.gensalt()).decode("utf-8")


def verificar_senha(senha: str, hash_: str) -> bool:
    return _bcrypt.checkpw(senha.encode("utf-8"), hash_.encode("utf-8"))


def criar_token(username: str, role: str, nome: str) -> str:
    expira  = datetime.now(timezone.utc) + timedelta(hours=TOKEN_EXP_HOURS)
    payload = {"sub": username, "role": role, "nome": nome, "exp": expira}
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=ALGORITHM)


def decodificar_token(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, settings.SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        return None


def nivel_role(role: str) -> int:
    return ROLES.get(role, {}).get("nivel", 0)


def tem_permissao(role_usuario: str, role_minimo: str) -> bool:
    return nivel_role(role_usuario) >= nivel_role(role_minimo)
