"""Ed25519 signature verification (RFC 8032 section 6), standard library only.

Verify-only port of the reference implementation in the AETHER repo
(``python/utils/keygen.py``). The plugin cannot ship PyNaCl (no pip
dependencies in a QGIS plugin), and the only thing it verifies is one release
manifest per check, so the slow-but-simple big-integer arithmetic is fine
(a verification takes a few milliseconds). Nothing here signs or handles a
secret, so constant-time behaviour is not a concern.
"""

from __future__ import annotations

import hashlib

_P = 2 ** 255 - 19
_Q = 2 ** 252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, _P - 2, _P) % _P
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)


def _recover_x(y: int, sign: int):
    if y >= _P:
        return None
    x2 = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P)
    if x2 % _P == 0:
        return None if sign else 0
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P != 0:
        x = x * _SQRT_M1 % _P
    if (x * x - x2) % _P != 0:
        return None
    if (x & 1) != sign:
        x = _P - x
    return x


_GY = 4 * pow(5, _P - 2, _P) % _P
_GX = _recover_x(_GY, 0)
_G = (_GX, _GY, 1, _GX * _GY % _P)


def _pt_add(p, q):
    a = (p[1] - p[0]) * (q[1] - q[0]) % _P
    b = (p[1] + p[0]) * (q[1] + q[0]) % _P
    c = 2 * p[3] * q[3] * _D % _P
    d = 2 * p[2] * q[2] % _P
    e, f, g, h = b - a, d - c, d + c, b + a
    return (e * f, g * h, f * g, e * h)


def _pt_mul(s: int, p):
    q = (0, 1, 1, 0)
    while s > 0:
        if s & 1:
            q = _pt_add(q, p)
        p = _pt_add(p, p)
        s >>= 1
    return q


def _pt_eq(p, q) -> bool:
    return ((p[0] * q[2] - q[0] * p[2]) % _P == 0) and ((p[1] * q[2] - q[1] * p[2]) % _P == 0)


def _pt_decompress(s: bytes):
    y = int.from_bytes(s, "little")
    sign, y = y >> 255, y & ((1 << 255) - 1)
    x = _recover_x(y, sign)
    return None if x is None else (x, y, 1, x * y % _P)


def _h_modq(data: bytes) -> int:
    return int.from_bytes(hashlib.sha512(data).digest(), "little") % _Q


def verify(public_key: bytes, message: bytes, signature: bytes) -> bool:
    """True iff *signature* is a valid Ed25519 signature of *message* under *public_key*."""
    if len(public_key) != 32 or len(signature) != 64:
        return False
    a_pt, r_pt = _pt_decompress(public_key), _pt_decompress(signature[:32])
    if a_pt is None or r_pt is None:
        return False
    s = int.from_bytes(signature[32:], "little")
    if s >= _Q:
        return False
    h = _h_modq(signature[:32] + public_key + message)
    return _pt_eq(_pt_mul(s, _G), _pt_add(r_pt, _pt_mul(h, a_pt)))
