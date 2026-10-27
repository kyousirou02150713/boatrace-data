#!/usr/bin/env python3
"""
ボートレース公式データ 1日分の収集プログラム(GitHub Actions 用)

使い方:
    python collect.py 20261026   ← 日付を指定
    python collect.py            ← 指定なしなら「昨日」(日本時間)

保存先:
    data/日付/race_weather_日付.csv   番組表+競走成績+気象
    data/日付/payout_日付.csv         払戻金
    data/日付/odds_日付.csv           単勝オッズ
    data/日付/odds2t_日付.csv         2連単オッズ(30通り)
    data/日付/odds3t_日付.csv         3連単オッズ(120通り)

秘密の情報(パスワード・鍵など)は一切使っていません。
"""

import os
import re
import sys
import time
import glob
import shutil
import tempfile
import subprocess
import unicodedata
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
import pandas as pd
from bs4 import BeautifulSoup

WAIT_SECONDS = 3            # アクセスの間隔(秒)。サイトに負担をかけないため 3 秒以上あける
RETRY = 3                   # 通信エラーのときに試す回数
USER_AGENT = "Mozilla/5.0"
DATA_DIR = "data"

JCD = {"01": "桐生", "02": "戸田", "03": "江戸川", "04": "平和島", "05": "多摩川", "06": "浜名湖",
       "07": "蒲郡", "08": "常滑", "09": "津", "10": "三国", "11": "びわこ", "12": "住之江",
       "13": "尼崎", "14": "鳴門", "15": "丸亀", "16": "児島", "17": "宮島", "18": "徳山",
       "19": "下関", "20": "若松", "21": "芦屋", "22": "福岡", "23": "唐津", "24": "大村"}

KEY = ["日付", "場コード", "R"]

PAYOUT_COLS = ["日付", "場コード", "R",
               "3連単_組番", "3連単_払戻", "3連複_組番", "3連複_払戻",
               "2連単_組番", "2連単_払戻", "2連複_組番", "2連複_払戻",
               "単勝_艇番", "単勝_払戻", "複勝1_艇番", "複勝1_払戻", "複勝2_艇番", "複勝2_払戻"]

TANSHO_COLS = KEY + [f"単勝オッズ_{b}" for b in range(1, 7)]
COMBO_COLS = KEY + ["組番", "オッズ"]


# ======================================================================
# 通信(すべてここを通すので、間隔は必ず 3 秒以上あく)
# ======================================================================
_last_access = [0.0]


def polite_get(url, params=None):
    """3秒以上あけてアクセスする。通信エラーは RETRY 回まで試す。
    404 などのレスポンスはそのまま返し、最後まで通信できなければ None を返す。"""
    for attempt in range(1, RETRY + 1):
        gap = time.time() - _last_access[0]
        if gap < WAIT_SECONDS:
            time.sleep(WAIT_SECONDS - gap)
        try:
            res = requests.get(url, params=params, headers={"User-Agent": USER_AGENT}, timeout=30)
            _last_access[0] = time.time()
            if res.status_code >= 500:
                raise requests.HTTPError(f"サーバーエラー {res.status_code}")
            return res
        except Exception as e:
            _last_access[0] = time.time()
            print(f"  通信エラー({attempt}/{RETRY}回目): {e}")
    return None


# ======================================================================
# 番組表・成績ファイル(LZH)のダウンロードと解凍
# ======================================================================
def download_txt(prefix, date, workdir):
    """prefix="b"(番組表)/"k"(成績)。解凍したテキストのパスを返す。
    ファイルがない(404)ときは "notfound"、通信できないときは None。"""
    yyyymm, yymmdd = date[:6], date[2:]
    url = f"https://www1.mbrace.or.jp/od2/{prefix.upper()}/{yyyymm}/{prefix}{yymmdd}.lzh"
    res = polite_get(url)
    if res is None:
        return None
    if res.status_code == 404:
        return "notfound"
    if res.status_code != 200:
        print(f"  {url} : 状態コード {res.status_code}")
        return None
    lzh = os.path.join(workdir, f"{prefix}{yymmdd}.lzh")
    with open(lzh, "wb") as f:
        f.write(res.content)
    subprocess.run(["lha", "xf", os.path.basename(lzh)], cwd=workdir, capture_output=True)
    found = [p for p in glob.glob(os.path.join(workdir, "*"))
             if os.path.basename(p).lower() == f"{prefix}{yymmdd}.txt"]
    return found[0] if found else None


def read_lines(path):
    with open(path, encoding="cp932", errors="replace") as f:
        return f.read().splitlines()


# ======================================================================
# 番組表の読み取り(数字がくっついた行にも対応)
# ======================================================================
def parse_bangumi(path):
    rows, jcd, race, skipped = [], None, None, 0
    pattern = (r"^([1-6])\s(\d{4})(\S.*?)(\d{2})(\S+?)(\d{2})([AB][12])\s*"
               r"(\d{1,2}\.\d{2})\s*(\d{1,3}\.\d{2})\s*"
               r"(\d{1,2}\.\d{2})\s*(\d{1,3}\.\d{2})\s*"
               r"(\d{1,3})\s*(\d{1,3}\.\d{2})\s*"
               r"(\d{1,3})\s*(\d{1,3}\.\d{2})")
    for line in read_lines(path):
        m = re.match(r"^(\d{2})BBGN", line)
        if m:
            jcd, race = m.group(1), None
            continue
        s = unicodedata.normalize("NFKC", line)
        m = re.match(r"^\s*(\d{1,2})R\s", s)
        if m:
            race = int(m.group(1))
            continue
        if not (jcd and race and re.match(r"^[1-6]\s\d{4}", s)):
            continue
        m = re.match(pattern, s)
        try:
            g = m.groups()
            rows.append({
                "場コード": jcd, "場名": JCD.get(jcd, jcd), "R": race, "艇番": int(g[0]),
                "登録番号": g[1], "選手名": g[2].strip(), "年齢": int(g[3]), "支部": g[4],
                "体重": int(g[5]), "級別": g[6],
                "全国勝率": float(g[7]), "全国2連率": float(g[8]),
                "当地勝率": float(g[9]), "当地2連率": float(g[10]),
                "モーター番号": int(g[11]), "モーター2連率": float(g[12]),
                "ボート番号": int(g[13]), "ボート2連率": float(g[14]),
            })
        except Exception:
            skipped += 1
    if skipped:
        print(f"  (番組表で読めなかった選手の行: {skipped}行 → 飛ばしました)")
    return pd.DataFrame(rows)


# ======================================================================
# 競走成績の読み取り
# ======================================================================
def parse_seiseki(path):
    rows, jcd, race = [], None, None
    for line in read_lines(path):
        m = re.match(r"^(\d{2})KBGN", line)
        if m:
            jcd, race = m.group(1), None
            continue
        s = unicodedata.normalize("NFKC", line)
        m = re.match(r"^\s*(\d{1,2})R\s", s)
        if m:
            race = int(m.group(1))
            continue
        m = re.match(r"^\s*(\S+)\s+([1-6])\s+(\d{4})\s+(\S.*?)\s+(\d{1,3})\s+(\d{1,3})\s+"
                     r"(\S+)\s+([1-6])\s+(\S+)\s*(\S*)", s)
        if m and jcd and race:
            g = m.groups()
            rows.append({"場コード": jcd, "R": race, "艇番": int(g[1]),
                         "着順": g[0], "展示タイム": g[6], "進入": int(g[7]),
                         "スタートタイミング": g[8], "レースタイム": g[9]})
    df = pd.DataFrame(rows, columns=["場コード", "R", "艇番", "着順", "展示タイム",
                                     "進入", "スタートタイミング", "レースタイム"])
    df["着順_数字"] = pd.to_numeric(df["着順"], errors="coerce")
    return df


# ======================================================================
# 気象の読み取り
# ======================================================================
def parse_weather(path):
    rows, jcd = [], None
    pattern = (r"^\s*(\d{1,2})R\s.*?H(\d+)m\s+(\S+)\s+"
               r"風\s+(\S+)\s+(\d+)m\s+波\s+(\d+)cm")
    for line in read_lines(path):
        m = re.match(r"^(\d{2})KBGN", line)
        if m:
            jcd = m.group(1)
            continue
        s = unicodedata.normalize("NFKC", line)
        m = re.match(pattern, s)
        if m and jcd:
            g = m.groups()
            rows.append({"場コード": jcd, "R": int(g[0]), "距離": int(g[1]),
                         "天候": g[2], "風向": g[3], "風速": int(g[4]), "波高": int(g[5])})
    return pd.DataFrame(rows, columns=["場コード", "R", "距離", "天候", "風向", "風速", "波高"]
                        ).drop_duplicates(subset=["場コード", "R"])


# ======================================================================
# 払戻金の読み取り
# ======================================================================
def parse_payout(path):
    summary, tanfuku = {}, {}
    jcd, race = None, None
    combo = r"(\d(?:[-=]\d){1,2})\s+(\d+)"
    for line in read_lines(path):
        m = re.match(r"^(\d{2})KBGN", line)
        if m:
            jcd, race = m.group(1), None
            continue
        if not jcd:
            continue
        s = unicodedata.normalize("NFKC", line).replace(",", "")
        m = re.match(r"^\s*(\d{1,2})R\s.*H\d+m", s)
        if m:
            race = int(m.group(1))
            continue
        m = re.match(r"^\s*(\d{1,2})R\s+(.*)$", s)
        if m and race is None:
            pairs = re.findall(combo, m.group(2))
            if len(pairs) == 4:
                summary[(jcd, int(m.group(1)))] = {
                    "3連単_組番": pairs[0][0], "3連単_払戻": int(pairs[0][1]),
                    "3連複_組番": pairs[1][0], "3連複_払戻": int(pairs[1][1]),
                    "2連単_組番": pairs[2][0], "2連単_払戻": int(pairs[2][1]),
                    "2連複_組番": pairs[3][0], "2連複_払戻": int(pairs[3][1])}
            continue
        if race is not None:
            t = s.strip()
            if t.startswith("単勝"):
                m = re.match(r"単勝\s+(\d)\s+(\d+)", t)
                if m:
                    tanfuku.setdefault((jcd, race), {}).update(
                        {"単勝_艇番": int(m.group(1)), "単勝_払戻": int(m.group(2))})
            elif t.startswith("複勝"):
                d = tanfuku.setdefault((jcd, race), {})
                for k, (boat, yen) in enumerate(re.findall(r"(\d)\s+(\d+)", t[2:])[:2], start=1):
                    d[f"複勝{k}_艇番"] = int(boat)
                    d[f"複勝{k}_払戻"] = int(yen)
    rows = []
    for (j, r) in sorted(set(summary) | set(tanfuku)):
        row = {"場コード": j, "R": r}
        row.update(summary.get((j, r), {}))
        row.update(tanfuku.get((j, r), {}))
        rows.append(row)
    return pd.DataFrame(rows)


# ======================================================================
# オッズの読み取り
# ======================================================================
def odds_page(page, date, jcd, rno):
    res = polite_get(f"https://www.boatrace.jp/owpc/pc/race/{page}",
                     params={"rno": int(rno), "jcd": str(jcd).zfill(2), "hd": date})
    if res is None:
        raise ConnectionError("通信できませんでした")
    if res.status_code != 200:
        raise ConnectionError(f"状態コード {res.status_code}")
    res.encoding = "utf-8"
    return BeautifulSoup(res.text, "html.parser")


def to_num(txt):
    try:
        return float(txt.replace(",", ""))
    except ValueError:
        return None


def get_tansho(date, jcd, rno):
    soup = odds_page("oddstf", date, jcd, rno)
    odds = {}
    for tr in soup.select("tr"):
        b, o = tr.select_one('td[class*="is-boatColor"]'), tr.select_one("td.oddsPoint")
        if not (b and o) or not b.get_text(strip=True).isdigit():
            continue
        boat = int(b.get_text(strip=True))
        if boat not in odds:                       # 先に出てくるのが単勝(後は複勝)
            odds[boat] = to_num(o.get_text(strip=True))
    return odds


def get_2t(date, jcd, rno):
    soup = odds_page("odds2tf", date, jcd, rno)
    table = None
    for t in soup.select("table"):
        trs = [tr for tr in t.select("tbody tr") if tr.select("td.oddsPoint")]
        if len(trs) == 5 and all(len(tr.select("td.oddsPoint")) == 6 for tr in trs):
            table = trs
            break
    if table is None:
        return pd.DataFrame(columns=["組番", "オッズ"]), None
    recs, mismatch = [], 0
    for i, tr in enumerate(table):
        boats = [td.get_text(strip=True) for td in tr.select('td[class*="is-boatColor"]')]
        for col, td in enumerate(tr.select("td.oddsPoint")):
            first = col + 1
            second = [b for b in range(1, 7) if b != first][i]
            if len(boats) == 6 and boats[col] != str(second):
                mismatch += 1
            recs.append({"組番": f"{first}-{second}", "オッズ": to_num(td.get_text(strip=True))})
    df = pd.DataFrame(recs)
    df["_1着"] = df["組番"].str[0].astype(int)
    df["_2着"] = df["組番"].str[2].astype(int)
    df = df.sort_values(["_1着", "_2着"]).drop(columns=["_1着", "_2着"]).reset_index(drop=True)
    return df, mismatch


def get_3t(date, jcd, rno):
    soup = odds_page("odds3t", date, jcd, rno)
    table = next((t for t in soup.select("table") if t.select_one("td.oddsPoint")), None)
    if table is None:
        return pd.DataFrame(columns=["組番", "オッズ"]), None
    rows = [tr for tr in table.select("tbody tr") if tr.select("td.oddsPoint")]
    recs, mismatch = [], 0
    for i, tr in enumerate(rows):
        boats = [td.get_text(strip=True) for td in tr.select('td[class*="is-boatColor"]')]
        for col, td in enumerate(tr.select("td.oddsPoint")):
            first = col + 1
            second = [b for b in range(1, 7) if b != first][i // 4]
            third = [b for b in range(1, 7) if b not in (first, second)][i % 4]
            if len(boats) == 12 and boats[col * 2: col * 2 + 2] != [str(second), str(third)]:
                mismatch += 1
            elif len(boats) == 6 and boats[col] != str(third):
                mismatch += 1
            recs.append({"組番": f"{first}-{second}-{third}", "オッズ": to_num(td.get_text(strip=True))})
    return pd.DataFrame(recs), mismatch


# ======================================================================
# 日付の決め方
# ======================================================================
def resolve_date():
    s = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("INPUT_DATE", "")
    s = (s or "").strip()
    if not s:
        return (datetime.now(ZoneInfo("Asia/Tokyo")) - timedelta(days=1)).strftime("%Y%m%d")
    if not re.fullmatch(r"\d{8}", s):
        print(f"日付の形が正しくありません: {s!r}(例: 20261026)")
        sys.exit(1)
    try:
        datetime.strptime(s, "%Y%m%d")
    except ValueError:
        print(f"存在しない日付です: {s}")
        sys.exit(1)
    return s


def save(df, path):
    df.to_csv(path, index=False, encoding="utf-8-sig")
    print(f"  保存: {path}({len(df)} 行)")


# ======================================================================
# メイン
# ======================================================================
def main():
    date = resolve_date()
    print(f"===== {date} のデータを集めます(アクセス間隔 {WAIT_SECONDS} 秒) =====")

    if not shutil.which("lha"):
        print("lha(解凍の道具)が見つかりません。lhasa をインストールしてください。")
        sys.exit(1)

    workdir = tempfile.mkdtemp()
    try:
        # ---------- 1. 番組表 ----------
        b_txt = download_txt("b", date, workdir)
        if b_txt == "notfound":
            print(f"{date}: 開催なし(番組表のファイルがありません。まだ公開前の可能性もあります)")
            return
        if b_txt is None:
            print("番組表をダウンロードできませんでした(通信エラー)。")
            sys.exit(1)
        bangumi = parse_bangumi(b_txt)
        if len(bangumi) == 0:
            print(f"{date}: 開催なし(番組表にレースがありません)")
            return

        # ---------- 2. 競走成績(気象・払戻金もこの中) ----------
        k_txt = download_txt("k", date, workdir)
        if k_txt in (None, "notfound"):
            print("競走成績のファイルを取得できませんでした(未公開か通信エラー)。後でもう一度実行してください。")
            sys.exit(1)
        seiseki = parse_seiseki(k_txt)
        weather = parse_weather(k_txt)
        payout = parse_payout(k_txt)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    # ---------- 3. これまでと同じ形に組み立てる ----------
    race = pd.merge(bangumi, seiseki, on=["場コード", "R", "艇番"], how="left")
    race.insert(0, "日付", date)
    weather.insert(0, "日付", date)
    race = pd.merge(race, weather, on=KEY, how="left")   # 中止レースは気象が空のまま残す
    payout.insert(0, "日付", date)
    payout = payout.reindex(columns=PAYOUT_COLS)

    out_dir = os.path.join(DATA_DIR, date)
    os.makedirs(out_dir, exist_ok=True)
    save(race, os.path.join(out_dir, f"race_weather_{date}.csv"))
    save(payout, os.path.join(out_dir, f"payout_{date}.csv"))

    # ---------- 4. オッズ(単勝・2連単・3連単) ----------
    races = race[KEY].drop_duplicates().sort_values(KEY).reset_index(drop=True)
    stopped = set(map(tuple, race[race["天候"].isna()][KEY].drop_duplicates().values))
    print(f"オッズの対象: {len(races)} レース(うち中止 {len(stopped)})"
          f" / アクセス {len(races) * 3} 回 / 目安 約 {len(races) * 3 * WAIT_SECONDS / 60:.0f} 分")

    tan_rows, t2_list, t3_list, failed = [], [], [], []
    for n, (_, rr) in enumerate(races.iterrows(), start=1):
        jcd, r = rr["場コード"], int(rr["R"])

        try:
            odds = get_tansho(date, jcd, r)
            if len(odds) == 6:
                row = {"日付": date, "場コード": jcd, "R": r}
                for b in range(1, 7):
                    row[f"単勝オッズ_{b}"] = odds.get(b)
                tan_rows.append(row)
            else:
                failed.append(("単勝", jcd, r, f"{len(odds)}艇"))
        except Exception as e:
            failed.append(("単勝", jcd, r, str(e)[:60]))

        try:
            o2, mm = get_2t(date, jcd, r)
            if len(o2) != 30 or mm:
                failed.append(("2連単", jcd, r, f"{len(o2)}通り / ずれ {mm}"))
            if len(o2):
                o2.insert(0, "R", r); o2.insert(0, "場コード", jcd); o2.insert(0, "日付", date)
                t2_list.append(o2)
        except Exception as e:
            failed.append(("2連単", jcd, r, str(e)[:60]))

        try:
            o3, mm = get_3t(date, jcd, r)
            if len(o3) != 120 or mm:
                failed.append(("3連単", jcd, r, f"{len(o3)}通り / ずれ {mm}"))
            if len(o3):
                o3.insert(0, "R", r); o3.insert(0, "場コード", jcd); o3.insert(0, "日付", date)
                t3_list.append(o3)
        except Exception as e:
            failed.append(("3連単", jcd, r, str(e)[:60]))

        if n % 20 == 0:
            print(f"  {n} / {len(races)} レース 完了")

    tan = pd.DataFrame(tan_rows, columns=TANSHO_COLS)
    t2 = pd.concat(t2_list, ignore_index=True) if t2_list else pd.DataFrame(columns=COMBO_COLS)
    t3 = pd.concat(t3_list, ignore_index=True) if t3_list else pd.DataFrame(columns=COMBO_COLS)
    save(tan, os.path.join(out_dir, f"odds_{date}.csv"))
    save(t2, os.path.join(out_dir, f"odds2t_{date}.csv"))
    save(t3, os.path.join(out_dir, f"odds3t_{date}.csv"))

    # ---------- 5. 結果の表示 ----------
    all_keys = set(map(tuple, races.values))

    def report(name, df, per_race):
        got = set(map(tuple, df[KEY].drop_duplicates().values)) if len(df) else set()
        lack = sorted(all_keys - got)
        lack_other = [k for k in lack if k not in stopped]
        blank = int(df.filter(like="オッズ").isna().sum().sum()) if len(df) else 0
        line = f"  {name:<4}: {len(got)} / {len(all_keys)} レース, {len(df)} 行"
        if per_race:
            line += f"(期待 {len(got) * per_race} 行)"
        line += f", オッズ空 {blank}, 欠け 中止{len(lack) - len(lack_other)}/それ以外{len(lack_other)}"
        print(line)
        if lack_other:
            print("    中止以外の欠け:", [f"{JCD.get(j, j)}{r}R" for _, j, r in lack_other])

    print()
    print(f"===== {date} の結果 =====")
    print(f"  開催場 : {race['場コード'].nunique()} 場")
    print(f"  レース : {len(races)}(うち中止 {len(stopped)})")
    print(f"  番組表+成績+気象: {len(race)} 行 / {len(race.columns)} 列")
    print(f"  払戻金 : {len(payout)} レース / {len(payout.columns)} 列")
    report("単勝", tan, None)
    report("2連単", t2, 30)
    report("3連単", t3, 120)

    if failed:
        print()
        print(f"取れなかった・足りなかったレース: {len(failed)} 件")
        for kind, j, r, why in failed:
            mark = "(中止)" if (date, j, r) in stopped else ""
            print(f"  {kind:<4} {JCD.get(j, j)} {r}R : {why} {mark}")
    else:
        print("取れなかった・足りなかったレース: なし")


if __name__ == "__main__":
    main()
