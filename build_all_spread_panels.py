# -*- coding: utf-8 -*-
"""构造含当月(c1)的跨期价差面板。

与 build_im_spread.py 不同：这里不剔除当月，而是把所有挂牌合约按到期月排序，
取下标 0/1/2/3（0=最近可用合约，通常是 c1；1=c2；2=c3；3=c4），
生成全部 6 组两两组合：
  i0_i1 : 近月-远月  (c1-c2)
  i0_i2 : 近月-下季  (c1-c3)
  i0_i3 : 近月-隔季  (c1-c4)
  i1_i2 : 远月-下季  (c2-c3)
  i1_i3 : 远月-隔季  (c2-c4)
  i2_i3 : 下季-隔季  (c3-c4)
"""
import os
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = HERE

PAIRS_ALL = {
    "i0_i1": ("近月-远月", 0, 1),
    "i0_i2": ("近月-下季", 0, 2),
    "i0_i3": ("近月-隔季", 0, 3),
    "i1_i2": ("远月-下季", 1, 2),
    "i1_i3": ("远月-隔季", 1, 3),
    "i2_i3": ("下季-隔季", 2, 3),
}


def exp_ym(code):
    return 200000 + int(code[2:])


def build_panels(data_path, prefix):
    df = pd.read_parquet(data_path)
    df["exp"] = df["code"].map(exp_ym)
    paths = {}
    for name, (label, near_idx, far_idx) in PAIRS_ALL.items():
        records = []
        for date, g in df.groupby("date"):
            g2 = g.sort_values("exp").reset_index(drop=True)
            if len(g2) <= far_idx:
                continue
            near = g2.iloc[near_idx]
            far = g2.iloc[far_idx]
            spread_abs = far["close"] - near["close"]
            spread_rel = spread_abs / near["close"]
            records.append({
                "date": date,
                "near_code": near["code"], "near_open": near["open"], "near_close": near["close"], "near_settle": near["settle"],
                "far_code": far["code"], "far_open": far["open"], "far_close": far["close"], "far_settle": far["settle"],
                "spread_abs": spread_abs, "spread_rel": spread_rel,
                "near_exp": near["exp"], "far_exp": far["exp"],
            })
        panel = pd.DataFrame(records).sort_values("date").reset_index(drop=True)
        path = os.path.join(OUT_DIR, f"{prefix}_spread_panel_all_{name}.parquet")
        panel.to_parquet(path, index=False)
        paths[name] = path
        print(f"[{prefix}] {name} {label} 行数={len(panel)} spread_rel均值={panel['spread_rel'].mean():.4%}")
    return paths


if __name__ == "__main__":
    build_panels(os.path.join(OUT_DIR, "im_future_data.parquet"), "im")
    build_panels(os.path.join(OUT_DIR, "ic_future_data.parquet"), "ic")
    print("\n全部面板构造完成。")
