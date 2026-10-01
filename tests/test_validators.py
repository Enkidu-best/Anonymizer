"""Checksum validators: generated valid IDs pass, one-digit corruption fails."""
import os
import random
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'bench'))
import ids  # noqa: E402

from core import validators as v  # noqa: E402


def _corrupt(s):
    i = max(i for i, c in enumerate(s) if c.isdigit())
    return s[:i] + str((int(s[i]) + 1) % 10) + s[i + 1:]


@pytest.mark.parametrize('gen,check', [
    (ids.inn10, v.inn10), (ids.inn12, v.inn12), (ids.ogrn, v.ogrn),
    (ids.ogrnip, v.ogrnip), (ids.snils, v.snils),
])
def test_valid_and_corrupted(gen, check):
    for _ in range(50):
        s = gen()
        assert check(s), s
        assert not check(_corrupt(s)), s


def test_accounts_against_bik():
    for _ in range(20):
        b = ids.bik()
        assert v.account_with_bik(ids.rs(b), b)
        assert v.account_with_bik(ids.ks(b), b)
        assert not v.account_with_bik(_corrupt(ids.rs(b)), b)


def test_luhn_and_iban():
    assert v.luhn('4276 3800 1234 5679')
    assert not v.luhn('4276 3800 1234 5674')
    assert v.iban('DE89 3704 0044 0532 0130 00')
    assert v.iban('GB82WEST12345698765432')
    assert not v.iban('DE89 3704 0044 0532 0130 01')


def test_random_numbers_rarely_pass_inn12():
    r = random.Random(1)
    hits = sum(v.inn12(str(r.randint(10**11, 10**12 - 1))) for _ in range(5000))
    assert hits < 150  # ~1/100 by construction
