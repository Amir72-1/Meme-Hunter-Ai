"""Solana PDA and Base58 helpers. No extra packages."""

from __future__ import annotations

from hashlib import sha256

_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_INDEX = {char: index for index, char in enumerate(_ALPHABET)}
_P = 2**255 - 19
_D = (-121665 * pow(121666, _P - 2, _P)) % _P


class PdaError(ValueError):
    """Raised when a public key or PDA cannot be derived."""


def b58decode(value: str) -> bytes:
    raw = value.strip()
    if not raw:
        raise PdaError("Empty public key")
    number = 0
    for char in raw:
        if char not in _INDEX:
            raise PdaError("Invalid Base58 character")
        number = number * 58 + _INDEX[char]
    payload = number.to_bytes((number.bit_length() + 7) // 8 or 1, "big")
    pad = len(raw) - len(raw.lstrip("1"))
    decoded = (b"\x00" * pad) + payload.lstrip(b"\x00")
    if len(decoded) != 32:
        raise PdaError("Public key must decode to exactly 32 bytes")
    return decoded


def is_valid_pubkey(value: object) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        b58decode(value.strip())
        return True
    except PdaError:
        return False


def b58encode(data: bytes) -> str:
    number = int.from_bytes(data, "big")
    chars: list[str] = []
    while number > 0:
        number, rem = divmod(number, 58)
        chars.append(_ALPHABET[rem])
    pad = len(data) - len(data.lstrip(b"\x00"))
    return ("1" * pad) + "".join(reversed(chars) or "1")


def is_on_curve(point: bytes) -> bool:
    if len(point) != 32:
        return False
    y = int.from_bytes(point, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    y2 = (y * y) % _P
    x2 = ((y2 - 1) * pow((_D * y2 + 1) % _P, _P - 2, _P)) % _P
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P != 0:
        x = (x * pow(2, (_P - 1) // 4, _P)) % _P
    if (x * x - x2) % _P != 0:
        return False
    if (x & 1) != sign:
        x = _P - x
    return True


def find_program_address(seeds: list[bytes], program_id: bytes) -> tuple[bytes, int]:
    if len(program_id) != 32:
        raise PdaError("Program id must be 32 bytes")
    for nonce in range(255, -1, -1):
        digest = sha256(b"".join(seeds) + bytes([nonce]) + program_id + b"ProgramDerivedAddress").digest()
        if not is_on_curve(digest):
            return digest, nonce
    raise PdaError("Unable to find a valid program address")
