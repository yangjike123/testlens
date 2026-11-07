"""一条通过、一条故意失败：演示失败用例、只跑失败、复制错误信息。"""

from __future__ import annotations

from shop.discount import apply_discount


def test_bulk_order_gets_fifteen_percent_off() -> None:
    assert apply_discount(200.0, 15) == 170.0


def test_intentional_failure_for_demo() -> None:
    """这条是**故意失败**的，不是环境问题。

    面板里可以看：失败用例列表、只跑失败、复制错误信息。
    把下面的 165.00 改成 170.00 就会全绿。
    """
    assert apply_discount(200.0, 15) == 165.00, "故意失败：15% 折扣后应是 170.00"
