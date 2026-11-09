from sql_analyst.schema import link, render, tokens


def test_introspection(shop_schema):
    t = shop_schema.tables["orders"]
    assert t.row_count == 4
    assert [c.name for c in t.columns] == ["id", "customer_id", "order_date", "total"]
    assert t.columns[0].primary_key
    assert t.foreign_keys[0].ref_table == "customers"
    country = next(c for c in shop_schema.tables["customers"].columns if c.name == "country")
    assert country.samples[0] == "Brazil"
    assert "canada" in country.values


def test_tokens_split_camel_and_snake():
    assert tokens("OrderDate customer_id Prices") == ["order", "date", "customer", "id", "price"]


def test_link_by_name(shop_schema):
    res = link(shop_schema, "What is the average order total?")
    assert res.tables[0] == "orders"


def test_link_by_value_and_bridge(shop_schema):
    # "Office" only appears as a value in products.category; customers come from "Brazil".
    res = link(shop_schema, "How many Office items did customers in Brazil buy?", max_tables=2)
    assert {"products", "customers"} <= set(res.tables)
    # orders and order_items sit on the FK path between them
    assert {"orders", "order_items"} <= set(res.tables)


def test_render_card(shop_schema):
    card = render(shop_schema, ["customers"])
    assert "TABLE customers" in card
    assert "id INTEGER PRIMARY KEY" in card
    assert "'Brazil'" in card
    assert "orders" not in card
    assert "'Brazil'" not in render(shop_schema, ["customers"], with_samples=False)
