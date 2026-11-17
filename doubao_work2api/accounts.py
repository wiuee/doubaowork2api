from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import tempfile
import time
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any

from .errors import AccountBusyError, NoAvailableAccountError
from .vault import AccountVault, FORMAT as VAULT_FORMAT

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class Account:
    id: str
    cookies: dict[str, str]
    device_id: str
    web_id: str
    device_name: str = "DOUBAO-LINUX"
    enabled: bool = True
    cooldown_until: float = 0.0
    last_error: str = ""
    in_flight: int = 0
    source_path: Path | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    name: str = ""
    remark: str = ""
    group: str = ""
    error_code: str = ""
    last_error_at: float = 0.0
    identity_id: str = ''
    app_id: str = ''
    client_profile: str = 'desktop'
    client_version: str = ''
    fingerprint: str = ''
    user_agent: str = ''

    @property
    def available(self) -> bool:
        return self.enabled and not self.failure_type and self.cooldown_until <= time.time()

    @property
    def failure_type(self):
        if self.error_code in {'403','412','710022002','710022004','712010702'}:
            return 'needs_verification'
        if self.error_code in {'401','710012001'}:
            return 'needs_login'
        return ''

    def cooldown(self, seconds: int, reason: str, error_code: str = "") -> None:
        self.cooldown_until = max(self.cooldown_until, time.time() + seconds)
        self.last_error = reason
        self.error_code = error_code
        self.last_error_at = time.time()

    def clear_error(self) -> None:
        self.cooldown_until = 0.0
        self.last_error = ""
        self.error_code = ""
        self.last_error_at = 0.0

    def public_dict(self) -> dict[str, Any]:
        remaining = max(0, int(self.cooldown_until - time.time()))
        if self.failure_type:
            state = self.failure_type
        elif not self.enabled:
            state = "disabled"
        elif remaining:
            state = "cooling"
        elif self.in_flight:
            state = "busy"
        else:
            state = "ready"
        return {
            "id": self.id,
            "name": self.name,
            "remark": self.remark,
            "group": self.group,
            "enabled": self.enabled,
            "available": self.available,
            "state": state,
            "failure_type": self.failure_type,
            "cooldown_remaining_seconds": remaining,
            "last_error": self.last_error,
            "error_code": self.error_code,
            "last_error_at": self.last_error_at,
            "device_id": self.device_id[:6] + "..." if self.device_id else "",
            "web_id": self.web_id[:6] + "..." if self.web_id else "",
            "in_flight": self.in_flight,
        }

    def to_document(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "device_id": self.device_id,
            "web_id": self.web_id,
            "device_name": self.device_name,
            "enabled": self.enabled,
            "name": self.name,
            "remark": self.remark,
            "group": self.group,
            "cooldown_until": self.cooldown_until,
            "last_error": self.last_error,
            "error_code": self.error_code,
            "last_error_at": self.last_error_at,
            "cookies": dict(self.cookies),
            **{key: getattr(self, key) for key in ('identity_id', 'app_id', 'client_profile', 'client_version', 'fingerprint', 'user_agent')},
        }


def _cookie_dict(value: Any) -> dict[str, str]:
    if isinstance(value, dict):
        return {
            str(key).strip(): str(item).strip()
            for key, item in value.items()
            if str(key).strip()
        }
    if isinstance(value, str):
        result: dict[str, str] = {}
        for part in value.split(";"):
            key, separator, item = part.partition("=")
            if separator and key.strip():
                result[key.strip()] = item.strip()
        return result
    if isinstance(value, list):
        result = {}
        for item in value:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            cookie_value = item.get("value")
            if name and cookie_value is not None:
                result[name] = str(cookie_value)
        return result
    return {}


def _as_bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() not in {"0", "false", "no", "off", ""}


def _document_fields(document: Any) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise ValueError("account file must contain a JSON object")
    source = document.get("account")
    if isinstance(source, dict):
        document = {**document, **source}
    session = document.get('session')
    if isinstance(session, dict):
        document = {**document, **session}
    aliases = {'cookieHeader': 'cookie', 'deviceId': 'device_id', 'webId': 'web_id',
               'userAgent': 'user_agent', 'appId': 'app_id', 'clientProfile': 'client_profile',
               'clientVersion': 'client_version'}
    document = dict(document)
    for old, new in aliases.items():
        if old in document and new not in document:
            document[new] = document[old]
    return document


def _session_identity(cookies: dict[str, str]) -> tuple[str, str] | None:
    # These session aliases identify an account; device/web IDs can be shared.
    session = cookies.get("sessionid") or cookies.get("sessionid_ss")
    if session:
        return ("sessionid", session)
    if cookies.get("sid_tt"):
        return ("sid_tt", cookies["sid_tt"])
    return None


def parse_account_document(document: Any) -> Account:
    document = _document_fields(document)
    cookies = _cookie_dict(
        document.get("cookies")
        or document.get("cookie")
        or document.get("cookie_string")
    )
    device_id = str(
        document.get("device_id")
        or document.get("tea_uuid")
        or document.get("user_unique_id")
        or ""
    ).strip()
    web_id = str(document.get("web_id") or "").strip()
    identity = document.get("identity")
    if isinstance(identity, dict):
        device_id = device_id or str(
            identity.get("device_id")
            or identity.get("user_unique_id")
            or ""
        ).strip()
        web_id = web_id or str(identity.get("web_id") or "").strip()
    account_id = str(
        document.get("id")
        or document.get("account_id")
        or ""
    ).strip()
    if not cookies:
        raise ValueError("account has no cookies")
    if not account_id:
        digest = hashlib.sha256(
            json.dumps(_session_identity(cookies) or cookies, sort_keys=True).encode("utf-8")
        ).hexdigest()
        account_id = f"account-{digest[:24]}"
    return Account(
        id=account_id,
        cookies=cookies,
        device_id=device_id,
        web_id=web_id,
        device_name=str(
            document.get("device_name") or "DOUBAO-LINUX"
        ).strip(),
        enabled=_as_bool(document.get("enabled"), True),
        name=str(document.get("name") or ""),
        remark=str(document.get("remark") or ""),
        group=str(document.get("group") or ""),
        cooldown_until=float(document.get("cooldown_until") or 0),
        last_error=str(document.get("last_error") or ""),
        error_code=str(document.get("error_code") or ""),
        last_error_at=float(document.get("last_error_at") or 0),
        **{key: str(document.get(key) or ('desktop' if key == 'client_profile' else ''))[:512]
           for key in ('identity_id', 'app_id', 'client_profile', 'client_version', 'fingerprint', 'user_agent')},
    )


class AccountPool:
    def __init__(
        self,
        accounts_dir: Path,
        *,
        cooldown_seconds: int = 60,
        account_max_concurrent: int = 0,
    ) -> None:
        self.accounts_dir = accounts_dir
        self.cooldown_seconds = cooldown_seconds
        self.account_max_concurrent = account_max_concurrent
        self._accounts: list[Account] = []
        self._index = 0
        self._lock = asyncio.Lock()

    @property
    def accounts(self) -> list[Account]:
        return list(self._accounts)

    def load(self) -> int:
        self.accounts_dir.mkdir(parents=True, exist_ok=True)
        loaded: list[Account] = []
        seen: set[str] = set()
        for path in sorted(self.accounts_dir.glob("*.json")):
            encrypted = False
            try:
                document = json.loads(path.read_text(encoding="utf-8"))
                encrypted = isinstance(document, dict) and document.get('format') == VAULT_FORMAT
                account = parse_account_document(AccountVault(self.accounts_dir).decode(document))
                account.source_path = path
            except Exception as exc:
                if encrypted:
                    raise ValueError(f'加密账号文件无法读取：{path.name}；原文件保留。') from exc
                logger.warning("Skipping account file %s: %s", path.name, exc)
                continue
            if account.id in seen:
                logger.warning("Skipping duplicate account id %s", account.id)
                continue
            seen.add(account.id)
            loaded.append(account)
        # Validate every encrypted document before generating any new key or
        # replacing legacy files. In particular, never mask a missing key.
        for account in loaded:
            raw = json.loads(account.source_path.read_text(encoding='utf-8'))
            if not isinstance(raw,dict) or raw.get('format') != VAULT_FORMAT:
                self._write(account)
        self._accounts = loaded
        self._index = 0
        logger.info("Loaded %d Doubao account(s)", len(loaded))
        return len(loaded)

    def find(self, account_id: str) -> Account | None:
        return next(
            (account for account in self._accounts if account.id == account_id),
            None,
        )

    async def acquire(self, *, allowed_ids: set[str] | None = None, preferred_id: str | None = None, verification_id: str | None = None) -> Account:
        async with self._lock:
            if not self._accounts:
                raise NoAvailableAccountError(
                    "No Doubao accounts are configured. Put account JSON files "
                    "under the accounts directory."
                )
            eligible = [
                account for account in self._accounts
                if allowed_ids is None or account.id in allowed_ids
            ]
            if not eligible:
                raise NoAvailableAccountError("No Doubao accounts match the allowed IDs.")
            candidates = [
                account
                for account in eligible
                if (account.available or (account.id == verification_id and account.enabled and account.failure_type == 'needs_verification')) and (self.account_max_concurrent == 0 or account.in_flight < self.account_max_concurrent)
            ]
            if not candidates:
                enabled = [account for account in eligible if account.enabled]
                if not enabled:
                    raise NoAvailableAccountError(
                        "All configured Doubao accounts are disabled."
                    )
                if any(account.failure_type for account in enabled):
                    raise NoAvailableAccountError('账号需要重新登录或官网人工验证；处理后请复测指定账号。',code='account_requires_action')
                if any(account.available for account in enabled):
                    raise AccountBusyError(
                        f"All usable Doubao accounts are busy (per-account concurrency limit: {self.account_max_concurrent}); wait for an active request to finish or raise the cap."
                    )
                wait = min(
                    max(1, math.ceil(account.cooldown_until - time.time()))
                    for account in enabled
                )
                raise NoAvailableAccountError(
                    f"All enabled Doubao accounts are cooling down; retry in about {wait}s.",
                    code="account_cooling_down",
                )
            preferred = next((account for account in candidates if account.id == preferred_id), None)
            if preferred is not None:
                preferred.in_flight += 1
                return preferred
            for offset in range(len(self._accounts)):
                index = (self._index + offset) % len(self._accounts)
                account = self._accounts[index]
                if account in candidates:
                    self._index = (index + 1) % len(self._accounts)
                    account.in_flight += 1
                    return account
        raise NoAvailableAccountError("Could not acquire a Doubao account.")

    async def release(self, account: Account) -> None:
        async with self._lock:
            account.in_flight = max(0, account.in_flight - 1)

    def mark_cooldown(
        self, account: Account, reason: str, error_code: str = ""
    ) -> None:
        candidate = replace(account)
        candidate.cooldown(self.cooldown_seconds, reason, error_code)
        self._persist_update(account, candidate)

    def mark_success(self, account: Account, *, started_at: float | None = None) -> None:
        # A concurrent request's later failure must survive this request's
        # success; otherwise a cooldown/verification warning could disappear.
        # Equal timestamps can still be a later error on coarse-resolution
        # Windows clocks, so preserve the error conservatively in that case.
        if started_at is not None and (
            not account.enabled or account.last_error_at >= started_at
        ):
            return
        if account.cooldown_until or account.last_error or account.error_code or account.last_error_at:
            candidate = replace(account)
            candidate.clear_error()
            self._persist_update(account, candidate)

    async def upsert(self, document: Any) -> Account:
        document = _document_fields(document)
        incoming = parse_account_document(document)
        async with self._lock:
            supplied_id = str(document.get("id") or document.get("account_id") or "").strip()
            existing = self.find(supplied_id) if supplied_id else None
            if existing is None and incoming.identity_id:
                existing = next((a for a in self._accounts if a.identity_id == incoming.identity_id), None)
            identity = _session_identity(incoming.cookies)
            if existing is None and identity is not None:
                existing = next(
                    (account for account in self._accounts
                     if _session_identity(account.cookies) == identity), None
                )
            if existing is None:
                existing = self.find(incoming.id)
            if existing is None:
                incoming.source_path = self._account_path(incoming.id)
                self._write(incoming)
                self._accounts.append(incoming)
                return incoming
            if existing.in_flight or existing.lock.locked():
                raise ValueError("账号正在处理请求，请在请求结束后更新凭证。")
            candidate = replace(
                existing, cookies=incoming.cookies,
                device_id=incoming.device_id or existing.device_id,
                web_id=incoming.web_id or existing.web_id,
            )
            for name in ("name", "remark", "group", "enabled", "device_name", 'identity_id', 'app_id', 'client_profile', 'client_version', 'fingerprint', 'user_agent'):
                if name in document:
                    setattr(candidate, name, getattr(incoming, name))
            candidate.clear_error()
            self._persist_update(existing, candidate)
            return existing

    async def set_enabled(self, account_id: str, enabled: bool) -> Account:
        async with self._lock:
            account = self.find(account_id)
            if account is None:
                raise KeyError(account_id)
            candidate = replace(account, enabled=enabled)
            if not enabled:
                candidate.cooldown_until = 0.0
            self._persist_update(account, candidate)
            return account

    async def update_metadata(self, account_id: str, patch: dict[str, Any]) -> Account:
        if not isinstance(patch, dict) or patch.keys() - {"name", "remark", "group", "enabled"}:
            raise ValueError("metadata patch only accepts name, remark, group, enabled")
        if any(not isinstance(value, str) for key, value in patch.items() if key != "enabled"):
            raise ValueError("metadata values must be strings")
        if "enabled" in patch and not isinstance(patch["enabled"], bool):
            raise ValueError("enabled must be a boolean")
        async with self._lock:
            account = self.find(account_id)
            if account is None:
                raise KeyError(account_id)
            candidate = replace(account, **patch)
            if patch.get("enabled") is False:
                candidate.cooldown_until = 0.0
            self._persist_update(account, candidate)
            return account

    def _select(self, ids: list[str]) -> list[Account]:
        selected = []
        for account_id in dict.fromkeys(ids):
            account = self.find(account_id)
            if account is None:
                raise KeyError(account_id)
            selected.append(account)
        return selected

    def export_accounts(self, ids: list[str]) -> list[dict[str, Any]]:
        return [account.to_document() for account in self._select(ids)]

    async def delete_many(self, ids: list[str]) -> list[str]:
        async with self._lock:
            selected = self._select(ids)
            if any(account.in_flight or account.lock.locked() for account in selected):
                raise ValueError("Cannot delete busy accounts")
            # Stage removals with same-directory renames. Roll back all staged
            # files if any rename fails, before touching the in-memory pool.
            staged: list[tuple[Path, Path]] = []
            try:
                for account in selected:
                    path = account.source_path
                    if path is None or not path.exists():
                        continue
                    descriptor, temporary = tempfile.mkstemp(
                        prefix=f".{path.name}.", suffix=".deleted", dir=path.parent
                    )
                    os.close(descriptor)
                    backup = Path(temporary)
                    try:
                        os.replace(path, backup)
                    except BaseException:
                        backup.unlink(missing_ok=True)
                        raise
                    staged.append((path, backup))
            except BaseException:
                for path, backup in reversed(staged):
                    os.replace(backup, path)
                raise
            deleted = [account.id for account in selected]
            deleted_set = set(deleted)
            next_id = self._accounts[self._index % len(self._accounts)].id if self._accounts else None
            self._accounts = [account for account in self._accounts if account.id not in deleted_set]
            self._index = next(
                (index for index, account in enumerate(self._accounts) if account.id == next_id),
                0,
            )
            for _, backup in staged:
                try:
                    backup.unlink()
                except OSError:
                    logger.warning("Could not remove a staged account deletion file")
            return deleted

    async def clear_cooldown(self, account_id: str) -> Account:
        async with self._lock:
            account = self.find(account_id)
            if account is None:
                raise KeyError(account_id)
            candidate = replace(account)
            candidate.clear_error()
            self._persist_update(account, candidate)
            return account

    def snapshot(self) -> list[dict[str, Any]]:
        return [account.public_dict() for account in self._accounts]

    def _account_path(self, account_id: str) -> Path:
        safe = "".join(
            character
            for character in account_id
            if character.isalnum() or character in {"-", "_"}
        )[:60]
        digest = hashlib.sha256(account_id.encode("utf-8")).hexdigest()
        return self.accounts_dir / f"{safe or 'account'}-{digest}.json"

    def _persist_update(self, account: Account, candidate: Account) -> None:
        candidate.source_path = account.source_path or self._account_path(account.id)
        self._write(candidate)
        # Keep the object held by in-flight requests, and its lock, intact.
        for item in fields(Account):
            if item.name not in {"lock", "in_flight"}:
                setattr(account, item.name, getattr(candidate, item.name))

    def _write(self, account: Account) -> None:
        assert account.source_path is not None
        path = account.source_path
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            AccountVault(self.accounts_dir).encode(account.to_document()),
            ensure_ascii=False,
            indent=2,
        )
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
