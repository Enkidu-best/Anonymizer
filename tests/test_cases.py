"""Declension of names inserted into new text (task 3, §2.3). Fictional names."""
import pytest

from core.cases import decline, same_person_form, target_case

M = 'Белозёров Аркадий Львович'
F = 'Скворцова Елена Дмитриевна'


@pytest.mark.parametrize('case,want', [
    ('nomn', M), ('gent', 'Белозёрова Аркадия Львовича'), ('datv', 'Белозёрову Аркадию Львовичу'),
    ('accs', 'Белозёрова Аркадия Львовича'), ('ablt', 'Белозёровым Аркадием Львовичем'),
    ('loct', 'Белозёрове Аркадии Львовиче'),
])
def test_male(case, want):
    assert decline(M, case) == want
    assert decline('Белозёрова Аркадия Львовича', case) == want     # from another case too


@pytest.mark.parametrize('case,want', [
    ('gent', 'Скворцовой Елены Дмитриевны'), ('datv', 'Скворцовой Елене Дмитриевне'),
    ('accs', 'Скворцову Елену Дмитриевну'), ('ablt', 'Скворцовой Еленой Дмитриевной'),
    ('nomn', F),
])
def test_female(case, want):
    assert decline(F, case) == want


def test_other_surname_kinds_and_initials():
    assert decline('Зелинский Олег Петрович', 'datv') == 'Зелинскому Олегу Петровичу'
    assert decline('Зелинская Ольга Петровна', 'gent') == 'Зелинской Ольги Петровны'
    assert decline('Кацнельсон Марк Ефимович', 'gent') == 'Кацнельсона Марка Ефимовича'
    assert decline('Кацнельсон Анна Ефимовна', 'gent') == 'Кацнельсон Анны Ефимовны'
    assert decline('Шевченко Пётр Ильич', 'datv') == 'Шевченко Петру Ильичу'
    assert decline('А.Л. Белозёров', 'datv') == 'А.Л. Белозёрову'
    assert decline('Тарханов Глеб Игоревич', 'datv') == 'Тарханову Глебу Игоревичу'   # not «Тархан»
    assert decline('Иванов Иван Иванович', 'ablt') == 'Ивановым Иваном Ивановичем'
    assert decline('БЕЛОЗЁРОВ АРКАДИЙ ЛЬВОВИЧ', 'ablt') == 'БЕЛОЗЁРОВЫМ АРКАДИЕМ ЛЬВОВИЧЕМ'


@pytest.mark.parametrize('left,right,case', [
    ('', ' подписал договор.', 'nomn'),
    ('Сторона: ', '', 'nomn'),
    ('Документы получены от ', '.', 'gent'),
    ('Подлинник хранится у ', '.', 'gent'),
    ('Письмо направить ', ' до пятницы.', 'datv'),
    ('Претензию передать ', '.', 'datv'),
    ('Требование к ', ' не предъявлялось.', 'datv'),
    ('Необходимо уведомить ', ' о сделке.', 'accs'),
    ('Договор заключён с ', '.', 'ablt'),
    ('Сведения о ', ' устарели.', 'loct'),
    ('Общество в лице ', ', действующего на основании', 'gent'),
    ('Кроме того, ', ' подписал акт.', 'nomn'),
    ('Риск связан с тем, что ', ' может', None),
])
def test_target_case(left, right, case):
    assert target_case(left, right) == case


def test_llm_answer_check():
    assert same_person_form(M, 'Белозёрову Аркадию Львовичу')
    assert not same_person_form(M, 'Скворцовой Елене Дмитриевне')
    assert not same_person_form(M, 'Белозёрову Аркадию Львовичу и другим')


@pytest.mark.parametrize('left,right,case', [
    ('Удостоверено нотариусом ', '.', 'ablt'),
    ('Договор с Индивидуальным предпринимателем ', ' (ОГРНИП', 'ablt'),
    ('обязательств Индивидуального предпринимателя ', ' перед Заказчиком', 'gent'),
    ('Компания контролируется семьей ', '. Единственный', 'gent'),
    ('По условиям договора ', ' обязуется оплатить', 'nomn'),
    ('Генеральный директор ', ' контролирует', 'nomn'),
    ('связей с фигурантом ', '?', 'ablt'),
])
def test_apposition_and_whose(left, right, case):
    assert target_case(left, right) == case


def test_hushing_surname_and_llm_case_only():
    from core.cases import case_of_form
    assert decline('Гуревич Павел Юрьевич', 'ablt') == 'Гуревичем Павлом Юрьевичем'
    assert decline('Гуревичем Павлом Юрьевичем', 'gent') == 'Гуревича Павла Юрьевича'
    # a half-declined LLM answer tells the case; the form is built by the rules
    assert case_of_form(M, 'Белозёров Аркадием Львовичем') == 'ablt'


def test_coordinated_names_share_the_case(tmp_db, session_id):
    from core.anonymizer import anonymize_text, make_rev_finder, apply_spans
    src = 'Стороны: Белозёров Аркадий Львович и Гуревич Павел Юрьевич.'
    out, occ = anonymize_text(src, tmp_db, session_id, use_spacy=False)
    a, b = sorted(t for t in occ if t.startswith('FIO'))
    text = f'Нужны поручительства от [{a}] и [{b}] до пятницы.'
    find = make_rev_finder(tmp_db, session_id)
    got = apply_spans(text, find(text))
    assert got == 'Нужны поручительства от Белозёрова Аркадия Львовича и Гуревича Павла Юрьевича до пятницы.'
