"""GMGSI の IR 値（0〜255・大きいほど冷たい）→ 雲の濃さ（0〜1）の決め方（Stage 6B・2026-10-04）。

単純な「冷たい＝雲」（固定のしきい値）は使わない（比べるためだけに残す：method 'fixed'）。
晴れの基準（clear-sky baseline）：同じ場所・同じ時刻（正時）の過去 N 日の IR 値の中で「暖かい方から p%」の値＝その場所・その時刻の晴れの値。
  雲は地面より冷たいので、今の値が基準より冷たい分（差）で雲を決める。
  ・同じ時刻にそろえるのは、陸（砂漠）は昼夜で地面の温度が大きく変わるため（夜の砂漠を雲と見ない・昼の高い山を晴れと見る）
  ・暖かい方から p% にするのは、たまたま暖かすぎる値（雑音）を基準にしないため
  ・雲がずっと続く所（層積雲の海・熱帯の雲）は基準が雲の値に寄る → 「とても冷たい」所は差によらず雲とする項を足す（cold）
差 → 濃さ：smoothstep(T − R/2, T + R/2, 差)。T＝しきい値（2 値の評価で 0.5 になる所）・R＝なめらかさの幅
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class Method:
    name: str
    days: int = 0            # 晴れの基準に使う過去の日数（0＝基準を使わない＝固定のしきい値）
    pct: float = 0.0         # 暖かい方から何 %（0＝いちばん暖かい）
    window_h: int = 0        # 前後何時間も基準に入れるか（0＝同じ正時だけ）
    T: float = 12.0          # 差のしきい値（IR 値の単位）
    R: float = 16.0          # なめらかさの幅
    cold: float | None = None  # これより冷たい（IR 値が大きい）所は差によらず雲（None＝使わない）
    coldR: float = 16.0
    slope: float = 0.0       # 晴れの基準が冷たい所（寒い地面）ほどしきい値を上げる：T＋slope×max(0, 基準 − r0)
    r0: float = 120.0
    S: float = 50.0          # 表示の濃さ：差がしきい値から S 増えると不透明（display_opacity）

    def label(self) -> str:
        if self.days == 0:
            return f"固定 IR≥{self.T:.0f}"
        s = f"基準 {self.days}日・{'最も暖かい' if self.pct == 0 else f'暖かい方{self.pct:.0f}%'}{'・±%dh' % self.window_h if self.window_h else ''}・差≥{self.T:.0f}"
        s += f"・基準が{self.r0:.0f}より冷たい所は差の条件を×{self.slope:g}/単位 上げる" if self.slope else ""
        return s + (f"・IR≥{self.cold:.0f}は雲" if self.cold is not None else "")


def smooth(x0: float, x1: float, x: np.ndarray) -> np.ndarray:
    t = np.clip((x - x0) / np.maximum(np.asarray(x1, np.float32) - x0, 1e-6), 0.0, 1.0)
    return t * t * (3 - 2 * t)


def clear_ref(hist: np.ndarray, pct: float) -> np.ndarray:
    """hist：(サンプル, …) の IR 値（0＝データなし）→ 晴れの基準（暖かい方から pct%）。データなしは 255（＝基準にならない）として扱う"""
    h = np.where(hist == 0, 255, hist).astype(np.uint8)
    n = h.shape[0]
    k = int(round(pct / 100 * (n - 1)))
    part = np.partition(h, k, axis=0)
    return part[k]


def alpha(v: np.ndarray, ref: np.ndarray | None, m: Method) -> np.ndarray:
    """IR 値 v（0＝データなし）と基準 → 雲の濃さ（0〜1・float32）。データなしは 0"""
    vf = v.astype(np.float32)
    if m.days == 0 or ref is None:
        a = smooth(m.T - m.R / 2, m.T + m.R / 2, vf)
    else:
        rf = ref.astype(np.float32)
        d = vf - rf
        T = m.T + m.slope * np.maximum(0.0, rf - m.r0)
        a = smooth(T - m.R / 2, T + m.R / 2, d)
        if m.cold is not None:
            a = np.maximum(a, smooth(m.cold - m.coldR / 2, m.cold + m.coldR / 2, vf))
    return np.where(v == 0, 0.0, a).astype(np.float32)


# 使う決め方（2026-10-04 study_cloud_alpha.py の比較：地域ごとの HSS の平均が最大。data_live/clouds/study/report.md・README）
#   過去 10 日・同じ正時・いちばん暖かい値を晴れの基準・差 ≥ 6 で雲（濃さ 0.5）・基準が 140 より冷たい所は 1 あたり 0.2 ずつ条件を上げる
#   表示の濃さ（display_opacity）：しきい値から差が S＝50 増えると不透明（比べた 30・50・80 のうち、雲の形が見え、全体の平均の濃さ 0.33 が
#   代表の静止画像の雲に近いもの。0/1 の雲マスクのままだと地球の 6 割が真っ白になる）
CHOSEN = Method("chosen", days=10, pct=0.0, T=6.0, R=10.0, r0=140.0, slope=0.2, S=50.0)


def display_opacity(v: np.ndarray, ref: np.ndarray, m: Method) -> np.ndarray:
    """表示の濃さ（0〜1）。雲の有無はしきい値（T・寒い地面の補正こみ）で決め、濃さは「基準より冷たい分」に比例させる
    （冷たい＝背の高い・厚い雲ほど不透明。薄い巻雲・低い雲は半透明）。光学的な厚さの観測ではない近似（Stage 6B の表示用）"""
    rf = ref.astype(np.float32)
    T = m.T + m.slope * np.maximum(0.0, rf - m.r0)
    o = np.clip((v.astype(np.float32) - rf - T) / m.S, 0.0, 1.0)
    return np.where(v == 0, 0.0, o).astype(np.float32)
