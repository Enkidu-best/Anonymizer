"""Checksum validators for Russian and international identifiers.

All functions take a string, ignore non-digit separators where it makes
sense, and return bool. Algorithms: docs/PII_VARIANTS_CATALOG.md.
"""
import re

_NON_DIGIT = re.compile(r'\D')


def digits(s: str) -> str:
    return _NON_DIGIT.sub('', s)


def inn10(s: str) -> bool:
    d = digits(s)
    if len(d) != 10 or d[0] == '0':
        return False
    w = (2, 4, 10, 3, 5, 9, 4, 6, 8)
    return sum(int(a) * b for a, b in zip(d, w)) % 11 % 10 == int(d[9])


def inn12(s: str) -> bool:
    d = digits(s)
    if len(d) != 12 or d[0] == '0':
        return False
    w1 = (7, 2, 4, 10, 3, 5, 9, 4, 6, 8)
    w2 = (3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8)
    c1 = sum(int(a) * b for a, b in zip(d, w1)) % 11 % 10
    c2 = sum(int(a) * b for a, b in zip(d, w2)) % 11 % 10
    return c1 == int(d[10]) and c2 == int(d[11])


def inn(s: str) -> bool:
    return inn10(s) or inn12(s)


def ogrn(s: str) -> bool:
    """ОГРН (13 digits, starts 1/5) or ГРН record number (starts 2/6)."""
    d = digits(s)
    if len(d) != 13 or d[0] not in '1256':
        return False
    return int(d[:12]) % 11 % 10 == int(d[12])


def ogrnip(s: str) -> bool:
    d = digits(s)
    if len(d) != 15 or d[0] not in '34':
        return False
    return int(d[:14]) % 13 % 10 == int(d[14])


def snils(s: str) -> bool:
    d = digits(s)
    if len(d) != 11:
        return False
    body, check = d[:9], int(d[9:])
    if int(body) <= 1001998:
        return False
    c = sum(int(x) * (9 - i) for i, x in enumerate(body)) % 101
    return (0 if c == 100 else c) == check


def luhn(s: str) -> bool:
    d = digits(s)
    if not 13 <= len(d) <= 19:
        return False
    total = 0
    for i, ch in enumerate(reversed(d)):
        n = int(ch)
        if i % 2:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def iban(s: str) -> bool:
    v = re.sub(r'\s', '', s).upper()
    if not re.fullmatch(r'[A-Z]{2}\d{2}[A-Z0-9]{11,30}', v):
        return False
    rearranged = v[4:] + v[:4]
    num = ''.join(str(int(c, 36)) for c in rearranged)
    return int(num) % 97 == 1


def account_with_bik(acc: str, bik: str) -> bool:
    """Control key of a 20-digit account against a BIK (settlement or correspondent)."""
    a, b = digits(acc), digits(bik)
    if len(a) != 20 or len(b) != 9:
        return False
    prefix = '0' + b[4:6] if a.startswith('301') else b[-3:]
    w = (7, 1, 3) * 8
    return sum((int(x) * y) % 10 for x, y in zip(prefix + a, w)) % 10 == 0
