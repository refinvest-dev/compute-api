"""Analytics AES-GCM storage format; the repository owns configuration validation."""

from os import urandom

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def create_payload_cipher(key: bytes) -> AESGCM:
    return AESGCM(key)


def encrypt_payload(cipher: AESGCM, identity: str, data: bytes) -> bytes:
    nonce = urandom(12)
    return nonce + cipher.encrypt(nonce, data, identity.encode())


def decrypt_payload(cipher: AESGCM, identity: str, data: bytes) -> bytes:
    return cipher.decrypt(data[:12], data[12:], identity.encode())
