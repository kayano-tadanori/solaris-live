"""NOAA GMGSI（全球の静止気象衛星の合成画像）の赤外（LW）を取る・読む共通の部品（Stage 6B・2026-10-04）。

配信物を正とする（公式資料どうしで記載が違うため。実ファイルで確かめた値をここに書く）
  取得の経路：NOAA Open Data Dissemination（NODD）の AWS S3 公開バケット noaa-gmgsi-pds（https・認証なし）
    https://noaa-gmgsi-pds.s3.amazonaws.com/GMGSI_LW/YYYY/MM/DD/HH/GLOBCOMPLIR_v3r0_blend_s…_e…_c….nc
  実ファイル（2026-10-01 00Z・2026-01-15 00Z で確認）：netCDF4（HDF5）・CF-1.8・Level 3・title GLOBCOMPLIR
    data（1×3000×4999・float32・整数の値 31〜255・_FillValue −9999・long_name「0-255 Brightness Temperature」・units「K」）
    dqf（int8・0＝良・20・−128＝埋め）・lat／lon（3000×4999 の 2 次元）・time（1970 年からの秒＝名目の時刻）
    列の数：2026-06 までは 5000（最後の列は経度 180° の重なり）・2026-09 からは 4999。最初の 4999 列を使う（同じ格子）
    格子：経度は 360°/4999 等間隔（列 0 が 180°＝−180°）。**緯度はメルカトル**（ln tan(π/4+φ/2) が行ごとに等間隔・
          赤道で 0.0722°・端で 0.0214°）。北 72.7154°〜南 −72.7368°（公式の有効範囲は 71°S〜71°N）
    1 時間に 1 個（time_coverage は名目の時刻から 10 分。source に各衛星の走査時刻＝名目の時刻と最大 15 分ほど違う）
    platform：G18・G19・Meteosat9・Meteosat10・H-9（ファイルごとに記録する）
  公式資料との違い（記録）：VLab「None (lon/lat grid)」→ 実ファイルはメルカトル／SCN 20-67「3 時間ごと」→ S3 は 1 時間ごと
  0〜255 の値 → 輝度温度（K）の換算式：公式資料で確認できない → **K として扱わない・表示しない**。生の値（以下「IR 値」）のまま使う。
    実データでは値が大きいほど冷たい（雲頂は 180〜255・晴れた熱帯の海は 60〜75）。この順序だけを使う
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import urllib.request
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
LIVE = ROOT / "data_live" / "clouds"
STORE = LIVE / "gmgsi_lw"          # 取った 1 時間分ごと：IR 値（uint8・zlib）＋メタデータ
BUCKET = "https://noaa-gmgsi-pds.s3.amazonaws.com"
UA = "uchuu-sim-cloud-fetcher/1.0 (personal educational app; about 24 requests per day)"
W, H = 4999, 3000
LAT_N, LAT_S = 72.7154, -72.7368
VALID_LAT = 71.0                   # 公式の有効範囲（これより極側は観測なし）


def utc(d: datetime) -> str:
    return d.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def list_keys(prefix: str, bucket: str = BUCKET) -> list[tuple[str, int]]:
    u = f"{bucket}/?list-type=2&prefix={prefix}&max-keys=1000"
    req = urllib.request.Request(u, headers={"User-Agent": UA})
    x = urllib.request.urlopen(req, timeout=60).read().decode()
    return list(zip(re.findall(r"<Key>(.*?)</Key>", x), map(int, re.findall(r"<Size>(.*?)</Size>", x))))


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    return urllib.request.urlopen(req, timeout=300).read()


def hour_path(t: datetime) -> Path:
    return STORE / f"{t:%Y/%m/%d}" / f"{t:%H}"


def read_lw(raw: bytes) -> tuple[np.ndarray, dict]:
    """netCDF の中身 → IR 値（uint8・0＝データなし）とメタデータ"""
    import h5py
    f = h5py.File(io.BytesIO(raw), "r")
    d = f["data"][0]
    cols = d.shape[1]
    # 2026 年 6〜9 月の間に 5000 列 → 4999 列（最後の列＝経度 180° の重なりを配信元が削った・ncks -d xc,0,4998）。最初の 4999 列は同じ格子
    assert d.shape[0] == H and cols in (W, W + 1), d.shape
    d = d[:, :W]
    ok = d > -9000
    v = np.where(ok, np.clip(d, 1, 255), 0).astype(np.uint8)
    lat = f["lat"][:, 0].astype(np.float64)
    lon0 = float(f["lon"][0, 0])
    A = {k: (v.decode() if isinstance(v, bytes) else v.tolist() if hasattr(v, "tolist") else v) for k, v in f.attrs.items()}
    meta = {
        "dims": [H, cols], "lat_first": float(lat[0]), "lat_last": float(lat[-1]), "lon_first": lon0,
        "lon_step": float(f["lon"][0, 2] - f["lon"][0, 1]),
        "merc_step": float(np.diff(np.log(np.tan(np.pi / 4 + np.radians(lat) / 2))).mean()),
        "time": utc(datetime.fromtimestamp(int(f["time"][0]), timezone.utc)),
        "time_coverage": [A.get("time_coverage_start"), A.get("time_coverage_end")], "date_created": A.get("date_created"),
        "platform": A.get("platform"), "instrument": A.get("instrument"), "title": A.get("title"),
        "data_long_name": f["data"].attrs["long_name"].decode(), "data_units": f["data"].attrs["units"].decode(),
        "value_range": [int(v[ok].min()), int(v[ok].max())] if ok.any() else None, "fill_pixels": int((~ok).sum()),
        "source_scans": sorted(set(re.findall(r"-(\d{12})-", str(A.get("source", ""))))),
    }
    return v, meta


def fetch_hour(t: datetime, force: bool = False) -> tuple[np.ndarray, dict] | None:
    """その時刻（UTC・正時）の LW を取って保存（あれば読むだけ）。無ければ None（欠けたフレーム）"""
    p = hour_path(t)
    if p.with_suffix(".json").exists() and not force:
        return load_hour(t)
    keys = [k for k in list_keys(f"GMGSI_LW/{t:%Y/%m/%d/%H}/") if k[0].endswith(".nc")]
    if not keys:
        return None
    key, size = keys[0]
    url = f"{BUCKET}/{key}"
    raw = fetch(url)
    v, meta = read_lw(raw)
    meta.update({"url": url, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(), "fetched": utc(datetime.now(timezone.utc)),
                 "s3_size": size, "nominal": utc(t)})
    p.parent.mkdir(parents=True, exist_ok=True)
    # 書きかけを残さない（一時ファイルに書いてから名前を変える。.json が後＝.json があれば中身はそろっている）
    tmp = p.with_suffix(".u8z.part")
    tmp.write_bytes(zlib.compress(v.tobytes(), 6)); tmp.replace(p.with_suffix(".u8z"))
    tmp = p.with_suffix(".json.part")
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8"); tmp.replace(p.with_suffix(".json"))
    return v, meta


def load_hour(t: datetime) -> tuple[np.ndarray, dict] | None:
    p = hour_path(t)
    if not p.with_suffix(".json").exists():
        return None
    v = np.frombuffer(zlib.decompress(p.with_suffix(".u8z").read_bytes()), np.uint8).reshape(H, W)
    return v, json.loads(p.with_suffix(".json").read_text(encoding="utf-8"))


def row_lat() -> np.ndarray:
    """各行の緯度（度・メルカトル）"""
    y0 = np.log(np.tan(np.pi / 4 + np.radians(LAT_N) / 2)); y1 = np.log(np.tan(np.pi / 4 + np.radians(LAT_S) / 2))
    y = y0 + (y1 - y0) * np.arange(H) / (H - 1)
    return np.degrees(2 * np.arctan(np.exp(y)) - np.pi / 2)


def col_lon() -> np.ndarray:
    """各列の経度（度・−180〜180）"""
    return -180.0 + np.arange(W) * (360.0 / W)


def sample(v: np.ndarray, lat: np.ndarray, lon: np.ndarray) -> np.ndarray:
    """緯度・経度（度）の所の値（いちばん近い画素・範囲外は 0）"""
    y0 = np.log(np.tan(np.pi / 4 + np.radians(LAT_N) / 2)); y1 = np.log(np.tan(np.pi / 4 + np.radians(LAT_S) / 2))
    la = np.clip(lat, -89.9, 89.9)
    y = np.log(np.tan(np.pi / 4 + np.radians(la) / 2))
    r = np.rint((y - y0) / (y1 - y0) * (H - 1)).astype(int)
    c = np.rint(((lon + 180.0) % 360.0) / (360.0 / W)).astype(int) % W
    inside = (r >= 0) & (r < H)
    out = np.zeros(np.broadcast(lat, lon).shape, np.uint8)
    out[inside] = v[r[inside], c[inside]]
    return out


def hours(t0: datetime, t1: datetime, step_h: int = 1):
    t = t0
    while t <= t1:
        yield t
        t += timedelta(hours=step_h)
