"""
APSEN - Autenticação JWT (IHM de Manutenção)
Apenas técnicos de manutenção precisam de login (para registrar intervenções).
O dashboard é read-only e não requer autenticação.
"""
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt as _bcrypt
from jose import JWTError, jwt

from config import settings

logger = logging.getLogger(__name__)

ALGORITHM       = "HS256"
TOKEN_EXP_HOURS = 8


def hash_senha(senha: str) -> str:
    return _bcrypt.hashpw(senha.encode("utf-8"), _bcrypt.gensalt()).decode("utf-8")


# Hash bcrypt bem-formado: `$2<letra>$<custo>$` + 53 caracteres do alfabeto
# do bcrypt, 60 no total. Ver `verificar_senha` para o porquê da checagem.
_RE_HASH_BCRYPT = re.compile(r"^\$2[abxy]?\$\d{2}\$[./A-Za-z0-9]{53}$")


def verificar_senha(senha: str, hash_: str) -> bool:
    """Confere a senha contra o hash. Entrada inválida é senha errada, não erro.

    Hash malformado no banco (coluna truncada, linha gravada por outra
    ferramenta) fazia `login()` devolver 500 em vez de 401. E a forma como ele
    quebra depende do formato:

      * lixo em geral (`""`, `"nao-e-um-hash"`, alfabeto inválido, custo fora
        da faixa) levanta `ValueError: Invalid salt` — capturável;
      * **hash truncado** (`"$2b$12$truncado"`) faz o backend Rust do bcrypt 4
        entrar em pânico: `pyo3_runtime.PanicException`, que herda de
        `BaseException` e NÃO de `Exception`. Nenhum `try/except` razoável o
        pega — por isso a defesa é a checagem de formato ANTES da chamada, e
        não só o try/except.

    Sobre senha longa: no bcrypt 4.1.3 `checkpw` com mais de 72 bytes devolve
    False (é `hashpw` que levanta), então esse caso já não gerava 500. O
    try/except cobre o comportamento de outras versões sem custo nenhum.
    """
    if not isinstance(hash_, str) or not _RE_HASH_BCRYPT.match(hash_):
        logger.warning("[AUTH] Hash malformado no banco — verificação recusada.")
        return False
    try:
        return _bcrypt.checkpw(senha.encode("utf-8"), hash_.encode("utf-8"))
    except (ValueError, TypeError) as exc:
        logger.warning("[AUTH] Verificação de senha rejeitada: %s", exc)
        return False


def criar_token(username: str, nome: str, role: str = "manutencao") -> str:
    expira  = datetime.now(timezone.utc) + timedelta(hours=TOKEN_EXP_HOURS)
    payload = {"sub": username, "nome": nome, "role": role, "exp": expira}
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=ALGORITHM)


def decodificar_token(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, settings.SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        return None
