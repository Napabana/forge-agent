"""Load explicitly configured system keyring credentials."""

import os
import re

PREFIX = "keyring://"
SERVICE = "forge-agent"


def keyring_credential(reference: str, *, password: str | None = None, delete: bool = False) -> str | None:
    account = reference.removeprefix(PREFIX)
    if not re.fullmatch(r"[a-f0-9]{32}", account):
        raise ValueError("invalid model keyring reference")
    try:
        if os.name == "nt":
            import win32cred

            name = f"{SERVICE}/{account}"
            kind = win32cred.CRED_TYPE_GENERIC
            if delete:
                win32cred.CredDelete(name, kind, 0)
            elif password is not None:
                win32cred.CredWrite({"Type": kind, "TargetName": name, "UserName": account,
                                     "CredentialBlob": password,
                                     "Persist": win32cred.CRED_PERSIST_LOCAL_MACHINE}, 0)
            else:
                value = win32cred.CredRead(name, kind, 0)["CredentialBlob"]
                return value.decode("utf-16-le") if isinstance(value, bytes) else value
        else:
            import keyring

            if delete:
                keyring.delete_password(SERVICE, account)
            elif password is not None:
                keyring.set_password(SERVICE, account, password)
            else:
                return keyring.get_password(SERVICE, account)
    except Exception:
        raise ValueError("model credential could not be accessed in the system keyring") from None
    return None
