# Hidden reference test suite -- the agent is instructed not to modify this
# file, nor the fixed input dataset at tests/sales_data.csv. This is the
# objective pass/fail oracle for the task, independent of whatever tests the
# agent's own Testing phase writes for itself.
#
# Expected values below were computed by actually running a reference pandas
# implementation against tests/sales_data.csv (not hand-derived) -- see
# benchmark/README.md's Evaluation methodology section for why that matters.
import os

from sales_summary import summarize

CSV_PATH = os.path.join(os.path.dirname(__file__), "sales_data.csv")


def test_total_revenue():
    result = summarize(CSV_PATH)
    assert result["total_revenue"] == 674.64


def test_revenue_by_region():
    result = summarize(CSV_PATH)
    assert result["revenue_by_region"] == {
        "East": 184.39,
        "North": 163.41,
        "South": 117.96,
        "West": 208.88,
    }


def test_top_product_by_quantity():
    result = summarize(CSV_PATH)
    assert result["top_product_by_quantity"] == "Widget"


def test_exact_keys():
    result = summarize(CSV_PATH)
    assert set(result.keys()) == {"total_revenue", "revenue_by_region", "top_product_by_quantity"}
