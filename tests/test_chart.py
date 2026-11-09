from sql_analyst.chart import choose


def test_single_number():
    assert choose(["n"], [[42]]).type == "number"


def test_single_text_is_no_chart():
    assert choose(["name"], [["Ana"]]).type == "none"


def test_bar_for_category_and_measure():
    spec = choose(["country", "revenue"], [["Brazil", 10.0], ["Canada", 5.0], ["USA", 7.5]])
    assert spec.type == "bar" and spec.x == "country" and spec.y == ["revenue"]
    assert spec.sort == "value_desc"


def test_line_for_time():
    rows = [["2024-01", 3], ["2024-02", 5], ["2024-03", 4]]
    assert choose(["month", "orders"], rows).type == "line"
    rows = [["2024-01-01", 3], ["2024-01-02", 5], ["2024-01-03", 4]]
    assert choose(["day_bucket", "orders"], rows).type == "line"


def test_grouped_bar():
    rows = [["A", 1, 2], ["B", 3, 4]]
    assert choose(["k", "x", "y"], rows).type == "grouped_bar"


def test_scatter():
    rows = [[i, i * 2.0] for i in range(10)]
    assert choose(["a", "b"], rows).type == "scatter"


def test_too_many_categories():
    rows = [[f"c{i}", i] for i in range(40)]
    assert choose(["c", "v"], rows).type == "none"


def test_empty():
    assert choose(["a"], []).type == "none"
