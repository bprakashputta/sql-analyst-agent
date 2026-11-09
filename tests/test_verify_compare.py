from sql_analyst.evals.compare import ex_ordered, ex_set, gold_is_ordered
from sql_analyst.verify import unverified_numbers

ROWS = [["Rock", 1234567.0, 0.125], ["Jazz", 2, 3.14159]]


def test_numbers_in_result_pass():
    text = "Rock sold about 1.2 million, Jazz 2, a 12.5% share and 3.14."
    assert unverified_numbers(text, ROWS, row_count=2) == []


def test_invented_number_is_flagged():
    assert unverified_numbers("Revenue was 99.", ROWS, row_count=2) == [
        "99."
    ] or unverified_numbers("Revenue was 99.", ROWS, row_count=2) == ["99"]


def test_numbers_from_question_are_fine():
    assert unverified_numbers("The top 5 are listed.", ROWS, row_count=2, context="top 5") == []


def test_ex_set_ignores_order_and_duplicates():
    assert ex_set([[1], [2], [2]], [[2], [1]])
    assert not ex_set([[1, 2]], [[2, 1]])  # column order matters


def test_float_rounding():
    assert ex_set([[0.1 + 0.2]], [[0.3]])
    assert not ex_set([[0.1 + 0.2]], [[0.3]], ndigits=None)
    assert ex_set([[7.0]], [[7]])


def test_ordered():
    assert gold_is_ordered("SELECT a FROM t ORDER BY a")
    assert not gold_is_ordered("SELECT a FROM (SELECT a FROM t ORDER BY a)")
    assert ex_ordered([[1], [2]], [[1], [2]], ordered=True)
    assert not ex_ordered([[2], [1]], [[1], [2]], ordered=True)
    assert not ex_ordered([[1], [1]], [[1]], ordered=False)
