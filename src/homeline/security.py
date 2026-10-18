# SPDX-License-Identifier: MPL-2.0
import hashlib
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from cryptography.fernet import Fernet

hasher = PasswordHasher(time_cost=3, memory_cost=32768, parallelism=2)
_dummy = hasher.hash(secrets.token_urlsafe(32))


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def verify_password(encoded, password):
    try:
        return hasher.verify(encoded or _dummy, password) and encoded is not None
    except (VerificationError, InvalidHashError):
        return False


class Vault:
    def __init__(self, path):
        if path.stat().st_mode & 0o077:
            raise PermissionError("Master key must not be group/world accessible")
        self.cipher = Fernet(path.read_bytes().strip())

    def encrypt(self, value):
        return self.cipher.encrypt(value.encode()).decode()

    def decrypt(self, value):
        return self.cipher.decrypt(value.encode()).decode()
