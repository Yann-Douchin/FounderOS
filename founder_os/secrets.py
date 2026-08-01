"""Secret resolution with an optional native macOS Keychain backend."""

from __future__ import annotations

import ctypes
import os
import re
import sys
from ctypes.util import find_library
from typing import Any, Iterable, Mapping, Protocol


_ACCOUNT_PATTERN = re.compile(r"[A-Z][A-Z0-9_]{1,127}")
_ERR_SEC_ITEM_NOT_FOUND = -25300


class SecretError(RuntimeError):
    pass


class SecretNotFound(SecretError):
    pass


class SecretStoreUnavailable(SecretError):
    pass


class SecretStore(Protocol):
    persistent: bool

    def allows(self, account: str) -> bool:
        raise NotImplementedError

    def get(self, account: str) -> str:
        raise NotImplementedError

    def set(self, account: str, value: str) -> None:
        raise NotImplementedError

    def delete(self, account: str) -> bool:
        raise NotImplementedError


class SecretResolver:
    """Resolve environment-shaped secret names without globally exporting them."""

    def __init__(
        self,
        store: SecretStore | None = None,
        *,
        accounts: list[str] | tuple[str, ...] | set[str] = (),
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.store = store
        self.accounts = {_validate_account(name) for name in accounts}
        self.environ = os.environ if environ is None else environ

    @property
    def persistent(self) -> bool:
        return bool(self.store and self.store.persistent)

    def get(self, account: str) -> str:
        account = _validate_account(account)
        if self.store is not None and self.accounts and account not in self.accounts:
            return ""
        if self.store is not None and (not self.accounts or account in self.accounts):
            try:
                stored_value = self.store.get(account).strip()
            except SecretNotFound:
                return ""
            if stored_value:
                return stored_value
            return ""
        return str(self.environ.get(account, "")).strip()

    def require(self, account: str, *, context: str = "secret") -> str:
        value = self.get(account)
        if not value:
            raise SecretNotFound(f"missing {context}: {account}")
        return value

    def persist(self, account: str, value: str) -> None:
        account = _validate_account(account)
        if self.accounts and account not in self.accounts:
            raise SecretError(f"secret account is not allowlisted: {account}")
        if self.store is None or not self.store.persistent:
            raise SecretStoreUnavailable(f"no persistent secret store is configured for {account}")
        value = str(value)
        if not value:
            raise SecretError(f"refusing to persist an empty secret: {account}")
        self.store.set(account, value)


class MemorySecretStore:
    """Test backend with the same persistence contract as the Keychain store."""

    persistent = True

    def __init__(
        self,
        values: Mapping[str, str] | None = None,
        *,
        accounts: Iterable[str] | None = None,
    ) -> None:
        self.values = dict(values or {})
        self.accounts = None if accounts is None else {_validate_account(name) for name in accounts}

    def allows(self, account: str) -> bool:
        account = _validate_account(account)
        return self.accounts is None or account in self.accounts

    def get(self, account: str) -> str:
        account = _validate_account(account)
        self._require_allowed(account)
        if account not in self.values:
            raise SecretNotFound(account)
        return self.values[account]

    def set(self, account: str, value: str) -> None:
        account = _validate_account(account)
        self._require_allowed(account)
        self.values[account] = str(value)

    def delete(self, account: str) -> bool:
        account = _validate_account(account)
        self._require_allowed(account)
        return self.values.pop(account, None) is not None

    def _require_allowed(self, account: str) -> None:
        if not self.allows(account):
            raise SecretError(f"secret account is not allowlisted: {account}")


class MacOSKeychainStore:
    """Generic-password storage through Security.framework, never process argv."""

    persistent = True

    def __init__(
        self,
        service: str = "com.founderos.runtime",
        *,
        api: Any | None = None,
        accounts: Iterable[str] | None = None,
    ) -> None:
        self.service = str(service).strip()
        if not self.service or len(self.service.encode("utf-8")) > 255:
            raise SecretError("Keychain service must contain between 1 and 255 UTF-8 bytes")
        self._api = api if api is not None else _MacOSKeychainAPI()
        self.accounts = None if accounts is None else {_validate_account(name) for name in accounts}

    def allows(self, account: str) -> bool:
        account = _validate_account(account)
        return self.accounts is None or account in self.accounts

    def get(self, account: str) -> str:
        account = _validate_account(account)
        self._require_allowed(account)
        return self._api.get(self.service, account)

    def set(self, account: str, value: str) -> None:
        account = _validate_account(account)
        self._require_allowed(account)
        value = str(value)
        if not value:
            raise SecretError(f"refusing to persist an empty secret: {account}")
        self._api.set(self.service, account, value)

    def delete(self, account: str) -> bool:
        account = _validate_account(account)
        self._require_allowed(account)
        return bool(self._api.delete(self.service, account))

    def _require_allowed(self, account: str) -> None:
        if not self.allows(account):
            raise SecretError(f"secret account is not allowlisted: {account}")


class _MacOSKeychainAPI:
    def __init__(self) -> None:
        if sys.platform != "darwin":
            raise SecretStoreUnavailable("macOS Keychain is available only on macOS")
        security_path = find_library("Security") or "/System/Library/Frameworks/Security.framework/Security"
        core_foundation_path = (
            find_library("CoreFoundation")
            or "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
        )
        try:
            self.security = ctypes.CDLL(security_path)
            self.core_foundation = ctypes.CDLL(core_foundation_path)
        except OSError as exc:
            raise SecretStoreUnavailable(f"cannot load macOS Keychain frameworks: {exc}") from exc
        self._configure_signatures()

    def _configure_signatures(self) -> None:
        self.security.SecKeychainFindGenericPassword.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_char_p,
            ctypes.c_uint32,
            ctypes.c_char_p,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
        ]
        self.security.SecKeychainFindGenericPassword.restype = ctypes.c_int32
        self.security.SecKeychainAddGenericPassword.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_char_p,
            ctypes.c_uint32,
            ctypes.c_char_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        self.security.SecKeychainAddGenericPassword.restype = ctypes.c_int32
        self.security.SecKeychainItemModifyAttributesAndData.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
        ]
        self.security.SecKeychainItemModifyAttributesAndData.restype = ctypes.c_int32
        self.security.SecKeychainItemDelete.argtypes = [ctypes.c_void_p]
        self.security.SecKeychainItemDelete.restype = ctypes.c_int32
        self.security.SecKeychainItemFreeContent.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self.security.SecKeychainItemFreeContent.restype = ctypes.c_int32
        self.core_foundation.CFRelease.argtypes = [ctypes.c_void_p]
        self.core_foundation.CFRelease.restype = None

    def get(self, service: str, account: str) -> str:
        service_bytes, account_bytes = service.encode("utf-8"), account.encode("utf-8")
        length = ctypes.c_uint32()
        data = ctypes.c_void_p()
        status = self.security.SecKeychainFindGenericPassword(
            None,
            len(service_bytes),
            service_bytes,
            len(account_bytes),
            account_bytes,
            ctypes.byref(length),
            ctypes.byref(data),
            None,
        )
        if status == _ERR_SEC_ITEM_NOT_FOUND:
            raise SecretNotFound(account)
        _raise_keychain_status(status, "read", account)
        try:
            raw = ctypes.string_at(data, length.value)
            return raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SecretError(f"Keychain item is not valid UTF-8: {account}") from exc
        finally:
            if data:
                self.security.SecKeychainItemFreeContent(None, data)

    def set(self, service: str, account: str, value: str) -> None:
        service_bytes, account_bytes = service.encode("utf-8"), account.encode("utf-8")
        value_bytes = value.encode("utf-8")
        buffer = ctypes.create_string_buffer(value_bytes)
        item = ctypes.c_void_p()
        existing_length = ctypes.c_uint32()
        existing_data = ctypes.c_void_p()
        status = self.security.SecKeychainFindGenericPassword(
            None,
            len(service_bytes),
            service_bytes,
            len(account_bytes),
            account_bytes,
            ctypes.byref(existing_length),
            ctypes.byref(existing_data),
            ctypes.byref(item),
        )
        try:
            if status == _ERR_SEC_ITEM_NOT_FOUND:
                status = self.security.SecKeychainAddGenericPassword(
                    None,
                    len(service_bytes),
                    service_bytes,
                    len(account_bytes),
                    account_bytes,
                    len(value_bytes),
                    ctypes.cast(buffer, ctypes.c_void_p),
                    None,
                )
                _raise_keychain_status(status, "create", account)
                return
            _raise_keychain_status(status, "find", account)
            status = self.security.SecKeychainItemModifyAttributesAndData(
                item,
                None,
                len(value_bytes),
                ctypes.cast(buffer, ctypes.c_void_p),
            )
            _raise_keychain_status(status, "update", account)
        finally:
            if existing_data:
                self.security.SecKeychainItemFreeContent(None, existing_data)
            if item:
                self.core_foundation.CFRelease(item)
            ctypes.memset(buffer, 0, len(buffer))

    def delete(self, service: str, account: str) -> bool:
        service_bytes, account_bytes = service.encode("utf-8"), account.encode("utf-8")
        item = ctypes.c_void_p()
        existing_length = ctypes.c_uint32()
        existing_data = ctypes.c_void_p()
        status = self.security.SecKeychainFindGenericPassword(
            None,
            len(service_bytes),
            service_bytes,
            len(account_bytes),
            account_bytes,
            ctypes.byref(existing_length),
            ctypes.byref(existing_data),
            ctypes.byref(item),
        )
        if status == _ERR_SEC_ITEM_NOT_FOUND:
            return False
        _raise_keychain_status(status, "find", account)
        try:
            status = self.security.SecKeychainItemDelete(item)
            _raise_keychain_status(status, "delete", account)
            return True
        finally:
            if existing_data:
                self.security.SecKeychainItemFreeContent(None, existing_data)
            if item:
                self.core_foundation.CFRelease(item)


def build_secret_resolver(config: Mapping[str, Any] | None) -> SecretResolver:
    secret_config = dict(config or {})
    provider = str(secret_config.get("provider", "environment")).strip().lower()
    accounts = secret_config.get("accounts") or []
    if not isinstance(accounts, list):
        raise SecretError("secrets.accounts must be a list")
    normalized_accounts = [_validate_account(str(name)) for name in accounts]
    if provider == "environment":
        return SecretResolver(accounts=normalized_accounts)
    if provider == "macos_keychain":
        service = str(secret_config.get("service", "com.founderos.runtime"))
        return SecretResolver(
            MacOSKeychainStore(service, accounts=normalized_accounts),
            accounts=normalized_accounts,
        )
    raise SecretError(f"unsupported secret provider: {provider}")


def keychain_store_from_config(config: Mapping[str, Any] | None) -> MacOSKeychainStore:
    secret_config = dict(config or {})
    provider = str(secret_config.get("provider", "environment")).strip().lower()
    if provider != "macos_keychain":
        raise SecretStoreUnavailable("configure secrets.provider as macos_keychain first")
    accounts = secret_config.get("accounts") or []
    if not isinstance(accounts, list):
        raise SecretError("secrets.accounts must be a list")
    return MacOSKeychainStore(
        str(secret_config.get("service", "com.founderos.runtime")),
        accounts=[_validate_account(str(name)) for name in accounts],
    )


def _validate_account(value: str) -> str:
    account = str(value).strip()
    if not _ACCOUNT_PATTERN.fullmatch(account):
        raise SecretError(f"invalid secret account name: {account or '<empty>'}")
    return account


def _raise_keychain_status(status: int, operation: str, account: str) -> None:
    if status:
        raise SecretError(f"Keychain {operation} failed for {account} with OSStatus {status}")
