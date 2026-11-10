# SPDX-License-Identifier: Apache-2.0
# Copyright: a-stock-data contributors (Simon Lin).
# Copied from simonlin1212/a-stock-data, commit f21534946c42be3ad048ac938eecb1bad84cb8ab,
# SKILL.md section 6.5. Changes: import/module packaging only.
# Full upstream license: a-stock-data.LICENSE alongside this module.

from contextlib import contextmanager

import baostock as bs
import pandas as pd


@contextmanager
def bs_session():
    """baostock 登录会话 — 必须用上下文管理器，异常路径也保证 logout"""
    lg = bs.login()
    if lg.error_code != "0":
        raise RuntimeError(f"baostock 登录失败: {lg.error_code} {lg.error_msg}")
    try:
        yield
    finally:
        bs.logout()


def _rs_to_df(rs) -> pd.DataFrame:
    """baostock ResultData → DataFrame；错误码转异常，绝不静默返回空表"""
    if rs.error_code != "0":
        raise RuntimeError(f"baostock 查询失败: {rs.error_code} {rs.error_msg}")
    rows = []
    while rs.next():
        rows.append(rs.get_row_data())
    return pd.DataFrame(rows, columns=rs.fields)

