"""Login, sessão e CSRF do painel admin.

- Um usuário (`ADMIN_USERNAME`) com senha em hash argon2 (`ADMIN_PASSWORD_HASH`). Sem os dois e o
  segredo da sessão (`ADMIN_SESSION_SECRET`), todo login é recusado.
- A sessão é um cookie assinado (itsdangerous), HttpOnly, SameSite=Lax e Secure sob https. Cai
  após 8 h sem uso, 12 h após o login ou quando a senha muda. Sair apaga o cookie; para derrubar
  todas as sessões de uma vez, troque `ADMIN_SESSION_SECRET`.
- O token CSRF nasce com a sessão e acompanha todo POST (campo oculto ou cabeçalho do HTMX).
- Bloqueio progressivo por IP: a 5ª falha seguida bloqueia 1 min, e cada nova falha dobra a
  espera até 15 min. Fica em memória do processo (some ao reiniciar; um contador por processo).
"""

from __future__ import annotations

import hashlib
import hmac
import math
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from itsdangerous import BadSignature, URLSafeSerializer
from starlette.responses import Response

from app.settings import Settings

Clock = Callable[[], float]


def instant(clock: Clock) -> datetime:
    """O instante do relógio `clock` (epoch em segundos) como `datetime` com fuso UTC: o `now` que
    os serviços recebem. É a única conversão do relógio do app para um instante, para a sessão, o
    admin e a API de admin lerem o mesmo tempo (os testes trocam o relógio)."""
    return datetime.fromtimestamp(clock(), UTC)


COOKIE_NAME = "mcc_admin"
COOKIE_PATH = "/admin"
IDLE_TIMEOUT = 8 * 3600  # segundos sem uso até a sessão cair
ABSOLUTE_TIMEOUT = 12 * 3600  # segundos desde o login
_hasher = PasswordHasher()


@dataclass
class _Failures:
    count: int
    last: float
    locked_until: float = 0.0


class LoginThrottle:
    MAX_FAILURES = 5
    WINDOW = 15 * 60  # falhas mais antigas que isso (sem bloqueio ativo) são esquecidas
    FIRST_LOCK = 60
    MAX_LOCK = 15 * 60
    MAX_KEYS = 10_000  # teto de memória sob ataque distribuído

    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._failures: dict[str, _Failures] = {}
        self._lock = threading.Lock()

    def _current(self, key: str, now: float) -> _Failures | None:
        entry = self._failures.get(key)
        if entry and now - entry.last > self.WINDOW and now >= entry.locked_until:
            del self._failures[key]
            return None
        return entry

    def retry_after(self, key: str) -> int:
        """Segundos até poder tentar de novo (0 = pode tentar)."""
        with self._lock:
            now = self._clock()
            entry = self._current(key, now)
            return math.ceil(entry.locked_until - now) if entry and entry.locked_until > now else 0

    def failure(self, key: str) -> int:
        """Registra uma falha e devolve a espera que ela impôs (0 = ainda sem bloqueio)."""
        with self._lock:
            now = self._clock()
            entry = self._current(key, now) or _Failures(count=0, last=now)
            entry.count += 1
            entry.last = now
            if entry.count >= self.MAX_FAILURES:
                wait = self.FIRST_LOCK * 2 ** (entry.count - self.MAX_FAILURES)
                entry.locked_until = now + min(wait, self.MAX_LOCK)
            self._failures.pop(key, None)
            self._failures[key] = entry  # reinserir mantém a ordem da falha mais recente
            while len(self._failures) > self.MAX_KEYS:
                del self._failures[next(iter(self._failures))]
            return math.ceil(entry.locked_until - now) if entry.locked_until > now else 0

    def success(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)


@dataclass
class AdminSession:
    username: str
    csrf_token: str
    issued_at: float
    flash: tuple[str, str] | None = None  # (tipo, mensagem) mostrado uma vez na próxima página


class AdminAuth:
    def __init__(
        self,
        *,
        username: str | None,
        password_hash: str | None,
        secret: str | None,
        secure_cookies: bool,
        clock: Clock = time.time,
    ) -> None:
        self.username = username
        self._password_hash = password_hash
        self.secure_cookies = secure_cookies
        self.clock = clock
        self.throttle = LoginThrottle(clock)
        self._serializer = (
            URLSafeSerializer(
                secret,
                salt="mcc-admin-session",
                signer_kwargs={"digest_method": hashlib.sha256},
            )
            if secret
            else None
        )
        # O cookie é assinado, não cifrado: guarda só um HMAC do hash, para a sessão cair quando
        # a senha muda sem revelar nada sobre ela.
        self._password_mark = (
            hmac.new(secret.encode(), password_hash.encode(), hashlib.sha256).hexdigest()[:16]
            if secret and password_hash
            else None
        )

    @classmethod
    def from_settings(cls, settings: Settings, *, clock: Clock = time.time) -> AdminAuth:
        password_hash = settings.admin_password_hash
        secret = settings.admin_session_secret
        return cls(
            username=settings.admin_username,
            password_hash=password_hash.get_secret_value() if password_hash else None,
            secret=secret.get_secret_value() if secret else None,
            secure_cookies=settings.public_base_url.startswith("https://"),
            clock=clock,
        )

    @property
    def enabled(self) -> bool:
        return bool(self.username and self._password_hash and self._serializer)

    def check_credentials(self, username: str, password: str) -> bool:
        """Compara em tempo constante e roda o argon2 mesmo com usuário errado."""
        if not self.enabled:
            return False
        assert self.username and self._password_hash
        user_ok = hmac.compare_digest(username.encode(), self.username.encode())
        try:
            password_ok = _hasher.verify(self._password_hash, password)
        except (VerificationError, InvalidHashError):
            password_ok = False
        return user_ok and password_ok

    def new_session(self, username: str) -> AdminSession:
        return AdminSession(
            username=username, csrf_token=secrets.token_urlsafe(32), issued_at=self.clock()
        )

    def set_cookie(self, response: Response, session: AdminSession) -> None:
        response.set_cookie(
            COOKIE_NAME,
            self.dump(session),
            max_age=IDLE_TIMEOUT,
            path=COOKIE_PATH,
            secure=self.secure_cookies,
            httponly=True,
            samesite="lax",
        )

    def dump(self, session: AdminSession) -> str:
        """Valor do cookie; renovado a cada requisição."""
        assert self._serializer is not None
        payload: dict[str, Any] = {
            "u": session.username,
            "csrf": session.csrf_token,
            "iat": session.issued_at,
            "seen": self.clock(),
            "pw": self._password_mark,
        }
        if session.flash:
            payload["flash"] = list(session.flash)
        return self._serializer.dumps(payload)

    def read(self, cookie: str | None) -> AdminSession | None:
        """A sessão, ou None se o cookie falta, foi adulterado, venceu, é de outra senha ou de um
        formato antigo deste código."""
        if not cookie or not self.enabled:
            return None
        assert self._serializer is not None
        try:
            data = self._serializer.loads(cookie)
            flash = data.get("flash")
            session = AdminSession(
                username=data["u"],
                csrf_token=data["csrf"],
                issued_at=float(data["iat"]),
                flash=(flash[0], flash[1]) if flash else None,
            )
            idle = self.clock() - float(data["seen"])
        except (BadSignature, AttributeError, KeyError, TypeError, ValueError, IndexError):
            return None
        if (
            session.username != self.username
            or data.get("pw") != self._password_mark
            or idle > IDLE_TIMEOUT
            or self.clock() - session.issued_at > ABSOLUTE_TIMEOUT
        ):
            return None
        return session
