"""Generators of checksum-valid fake Russian identifiers (for test corpora)."""
import random

R = random.Random(42)


def _d(n):
    return [R.randint(0, 9) for _ in range(n)]


def inn10():
    d = _d(9)
    d[0] = R.randint(1, 9)
    w = [2, 4, 10, 3, 5, 9, 4, 6, 8]
    d.append(sum(a * b for a, b in zip(d, w)) % 11 % 10)
    return ''.join(map(str, d))


def inn12():
    d = _d(10)
    d[0] = R.randint(1, 9)
    w1 = [7, 2, 4, 10, 3, 5, 9, 4, 6, 8]
    d.append(sum(a * b for a, b in zip(d, w1)) % 11 % 10)
    w2 = [3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8]
    d.append(sum(a * b for a, b in zip(d, w2)) % 11 % 10)
    return ''.join(map(str, d))


def ogrn():
    body = '1' + ''.join(map(str, _d(11)))
    return body + str(int(body) % 11 % 10)


def ogrnip():
    body = '3' + ''.join(map(str, _d(13)))
    return body + str(int(body) % 13 % 10)


def snils():
    while True:
        d = _d(9)
        if int(''.join(map(str, d))) > 1001998:
            break
    s = sum(x * (9 - i) for i, x in enumerate(d))
    c = s % 101
    if c == 100:
        c = 0
    n = ''.join(map(str, d))
    return f'{n[:3]}-{n[3:6]}-{n[6:9]} {c:02d}'


def bik():
    return '04' + ''.join(map(str, _d(7)))


def _acc_key(prefix3, acc20):
    digits = [int(c) for c in prefix3 + acc20]
    digits[3 + 8] = 0
    w = [7, 1, 3] * 8
    s = sum((a * b) % 10 for a, b in zip(digits, w))
    return (s % 10) * 3 % 10


def rs(bik_):
    a = list('40702810' + '0' + ''.join(map(str, _d(11))))
    a[8] = str(_acc_key(bik_[-3:], ''.join(a)))
    return ''.join(a)


def ks(bik_):
    a = list('30101810' + '0' + ''.join(map(str, _d(8))) + bik_[-3:])
    a[8] = str(_acc_key('0' + bik_[4:6], ''.join(a)))
    return ''.join(a)


def kpp():
    return ''.join(map(str, _d(4))) + '01' + ''.join(map(str, _d(3)))
