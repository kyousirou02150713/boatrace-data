#!/usr/bin/env python3
"""
過去分の直前情報(展示タイムなど)を集めるプログラム(GitHub Actions 用・箱2)

・取る順番:
    (1) まず 2026年10月21日〜昨日(日本時間)の分を、新しい日から順に取る
    (2) (1)が全部終わったら、2026年10月20日〜4月24日へ、新しい日から古い日に向かって取る
・すでに data/beforeinfo/beforeinfo_日付.csv がある日は飛ばす
・1回の実行で、最大 MAX_DAYS 日分まで(時間の上限 MAX_MINUTES を過ぎたら、次の日には進まない)
・アクセス間隔 3 秒 / 1 回の待ち時間 30 秒 / だめなら最大 3 回まで取り直す

使い方:
    python collect_beforeinfo.py            ← いつもの使い方
    python collect_beforeinfo.py 3          ← 今回だけ 3 日分にする

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

# ===== 設定 =====
START_DATE = "20260424"      # 過去分の一番古い日
END_DATE = "20261020"        # 過去分の一番新しい日
RECENT_START = "20261021"    # この日から「昨日」までは、過去分より先に取る
MAX_DAYS = 5                 # 1回の実行で取る最大の日数
MAX_MINUTES = 140            # この時間を過ぎたら、次の日には進まない(Actions の上限 180 分より前に終える)
FAIL_RATE_LIMIT = 0.10       # 通信エラーで取れなかったレースがこの割合を超えた日は保存しない(次回取り直す)

WAIT_SECONDS = 3             # アクセスの間隔(秒)
TIMEOUT = 30                 # 1回あたり、返事を待つ最大の秒数
RETRY = 3                    # だめだったときに試す最大の回数
RETRY_WAIT = 5               # 取り直す前に待つ秒数

OUT_DIR = os.path.join("data", "beforeinfo")

COLS = ["日付", "場コード", "R", "艇番", "登録番号", "選手名", "体重", "調整重量",
        "展示タイム", "チルト", "プロペラ", "部品交換", "展示進入", "展示ST",
        "直前_気温", "直前_天候", "直前_風速", "直前_風向番号", "直前_水温", "直前_波高", "取得時刻"]


def now_str():
    return datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%Y-%m-%d %H:%M:%S")


# ======================================================================
# 通信:3秒以上あけて、だめなら最大3回まで取り直す
# ======================================================================
_last_access = [0.0]


def polite_get(url, params=None):
    last_error = None
    for attempt in range(1, RETRY + 1):
        gap = time.time() - _last_access[0]
        if gap < WAIT_SECONDS:
            time.sleep(WAIT_SECONDS - gap)
        try:
            res = requests.get(url, params=params,
                               headers={"User-Agent": "Mozilla/5.0"}, timeout=TIMEOUT)
            _last_access[0] = time.time()
            if res.status_code >= 500:
                raise requests.HTTPError(f"サーバーエラー {res.status_code}")
            return res
        except Exception as e:
            _last_access[0] = time.time()
            last_error = e
            if attempt < RETRY:
                time.sleep(RETRY_WAIT)
    raise ConnectionError(f"{RETRY}回試しても取れませんでした({type(last_error).__name__})")


# ======================================================================
# 番組表から、その日のレースの一覧を作る
# ======================================================================
def race_list_from_bangumi(date):
    """戻り値: レースの一覧 [(場コード, R), ...]。番組表がない日は [](開催なし)"""
    yyyymm, yymmdd = date[:6], date[2:]
    res = polite_get(f"https://www1.mbrace.or.jp/od2/B/{yyyymm}/b{yymmdd}.lzh")
    if res.status_code == 404:
        return []
    if res.status_code != 200:
        raise ConnectionError(f"番組表: 状態コード {res.status_code}")
    workdir = tempfile.mkdtemp()
    try:
        lzh = os.path.join(workdir, f"b{yymmdd}.lzh")
        with open(lzh, "wb") as f:
            f.write(res.content)
        subprocess.run(["lha", "xf", os.path.basename(lzh)], cwd=workdir, capture_output=True)
        txt = [p for p in glob.glob(os.path.join(workdir, "*"))
               if os.path.basename(p).lower() == f"b{yymmdd}.txt"]
        if not txt:
            raise RuntimeError("番組表を解凍できませんでした")
        races, jcd = set(), None
        with open(txt[0], encoding="cp932", errors="replace") as f:
            for line in f.read().splitlines():
                m = re.match(r"^(\d{2})BBGN", line)
                if m:
                    jcd = m.group(1)
                    continue
                m = re.match(r"^\s*(\d{1,2})R\s", unicodedata.normalize("NFKC", line))
                if m and jcd:
                    races.add((jcd, int(m.group(1))))
        return sorted(races)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# ======================================================================
# 直前情報のページを読む(Colab で成功したものと同じ)
# ======================================================================
def _num(txt):
    m = re.search(r"-?\d+(\.\d+)?", txt or "")
    return float(m.group()) if m else None


def get_beforeinfo(date, jcd, rno):
    """戻り値: (表, 状態)。通信エラーのときは ConnectionError を出す"""
    jcd = str(jcd).zfill(2)
    res = polite_get("https://www.boatrace.jp/owpc/pc/race/beforeinfo",
                     params={"rno": int(rno), "jcd": jcd, "hd": str(date)})
    res.encoding = "utf-8"
    soup = BeautifulSoup(res.text, "html.parser")

    table = next((t for t in soup.select("table") if "展示タイム" in t.get_text()), None)
    if table is None:
        msg = "中止" if "中止" in soup.get_text() else "表が見つかりません(未発表・中止など)"
        return pd.DataFrame(), msg

    rows = []
    for tb in table.select("tbody"):
        trs = tb.select("tr")
        if not trs:
            continue
        tds = trs[0].select("td")
        texts = [td.get_text(" ", strip=True) for td in tds]
        boat_td = tb.select_one('td[class*="is-boatColor"]')
        if not boat_td or not boat_td.get_text(strip=True).isdigit():
            continue

        link = tb.select_one('a[href*="toban="]')
        toban = re.search(r"toban=(\d{4})", link["href"]).group(1) if link else None
        name = link.get_text(strip=True).replace("\u3000", " ") if link else None

        w_idx = next((i for i, t in enumerate(texts) if "kg" in t), None)

        def after(k):
            return texts[w_idx + k] if w_idx is not None and w_idx + k < len(texts) else ""

        ex_time = _num(after(1))
        tilt = _num(after(2))
        parts_td = tds[w_idx + 4] if w_idx is not None and w_idx + 4 < len(tds) else None
        parts = " ".join(li.get_text(strip=True) for li in parts_td.select("li")) if parts_td else ""
        if parts_td is not None and not parts:
            parts = parts_td.get_text(" ", strip=True)

        adj = _num(trs[1].get_text(" ", strip=True)) if len(trs) > 1 else None

        rows.append({
            "日付": date, "場コード": jcd, "R": int(rno),
            "艇番": int(boat_td.get_text(strip=True)),
            "登録番号": toban, "選手名": name,
            "体重": _num(after(0)), "調整重量": adj,
            "展示タイム": ex_time if ex_time and 5 < ex_time < 9 else None,
            "チルト": tilt,
            "プロペラ": after(3),
            "部品交換": parts,
        })
    df = pd.DataFrame(rows)

    st = []
    for course, box in enumerate(soup.select('div[class*="table1_boatImage1"]'), start=1):
        n = box.select_one('[class*="boatImage1Number"]')
        t = box.select_one('[class*="boatImage1Time"]')
        if n and n.get_text(strip=True).isdigit():
            st.append({"艇番": int(n.get_text(strip=True)), "展示進入": course,
                       "展示ST": t.get_text(strip=True) if t else None})
    if len(df) and st:
        df = df.merge(pd.DataFrame(st).drop_duplicates("艇番"), on="艇番", how="left")

    wx = {}
    for unit in soup.select('div[class*="weather1_bodyUnit"]'):
        title = unit.select_one('[class*="LabelTitle"]')
        data = unit.select_one('[class*="LabelData"]')
        title = title.get_text(strip=True) if title else ""
        data = data.get_text(strip=True) if data else ""
        cls = " ".join(unit.get("class", []))
        if "気温" in title:
            wx["直前_気温"] = _num(data)
        elif "水温" in title:
            wx["直前_水温"] = _num(data)
        elif "風速" in title:
            wx["直前_風速"] = _num(data)
        elif "波高" in title:
            wx["直前_波高"] = _num(data)
        elif "is-weather" in cls and title:
            wx["直前_天候"] = title
        elif "is-windDirection" in cls:
            icon = unit.select_one('[class*="is-wind"]')
            m = re.search(r"is-wind(\d+)", " ".join(icon.get("class", []))) if icon else None
            wx["直前_風向番号"] = int(m.group(1)) if m else None
    for k, v in wx.items():
        df[k] = v
    df["取得時刻"] = now_str()

    if len(df) == 0:
        return df, "6艇の行が読めませんでした"
    if df["展示タイム"].isna().all():
        return df, "展示タイムがありません(中止など)"
    return df, "OK"


# ======================================================================
# 1日分を取る
# ======================================================================
def collect_one_day(date):
    """戻り値: (保存したか, メッセージ)"""
    try:
        races = race_list_from_bangumi(date)
    except Exception as e:
        return False, f"番組表が取れませんでした({e})。次回取り直します"

    path = os.path.join(OUT_DIR, f"beforeinfo_{date}.csv")
    if not races:
        # 開催なしの日も、空のファイルを置いて「取り終わった」印にする
        pd.DataFrame(columns=COLS).to_csv(path, index=False, encoding="utf-8-sig")
        return True, "開催なし(空のファイルを保存)"

    tables, problems, comm_errors = [], [], 0
    for jcd, r in races:
        try:
            df, status = get_beforeinfo(date, jcd, r)
            if len(df):
                tables.append(df)
            if status != "OK":
                problems.append((jcd, r, status))
        except Exception as e:
            comm_errors += 1
            problems.append((jcd, r, f"エラー: {str(e)[:60]}"))

    if comm_errors > len(races) * FAIL_RATE_LIMIT:
        for jcd, r, why in problems:
            print(f"    場コード{jcd} {r}R : {why}")
        return False, (f"通信エラーが多い({comm_errors}/{len(races)} レース)ので保存しません。"
                       "次回取り直します")

    bi = pd.concat(tables, ignore_index=True) if tables else pd.DataFrame(columns=COLS)
    bi = bi.reindex(columns=COLS).sort_values(["場コード", "R", "艇番"]).reset_index(drop=True)
    bi.to_csv(path, index=False, encoding="utf-8-sig")

    got = bi.groupby(["場コード", "R"]).ngroups if len(bi) else 0
    print(f"    取れたレース {got}/{len(races)} / {len(bi)} 行 / 展示タイムが空 {bi['展示タイム'].isna().sum()} 行")
    if problems:
        print(f"    取れなかった・展示タイムがないレース: {len(problems)} 件")
        for jcd, r, why in problems:
            print(f"      場コード{jcd} {r}R : {why}")
    return True, f"保存しました: {path}"


# ======================================================================
# メイン
# ======================================================================
def main():
    max_days = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].strip().isdigit() else MAX_DAYS
    if not shutil.which("lha"):
        print("lha(解凍の道具)が見つかりません。lhasa をインストールしてください。")
        sys.exit(1)
    os.makedirs(OUT_DIR, exist_ok=True)

    def missing_days(newest, oldest):
        """newest から oldest へ、新しい日から順に、まだ保存していない日を並べる"""
        d = datetime.strptime(newest, "%Y%m%d")
        stop = datetime.strptime(oldest, "%Y%m%d")
        days, total = [], 0
        while d >= stop:
            ds = d.strftime("%Y%m%d")
            total += 1
            if not os.path.exists(os.path.join(OUT_DIR, f"beforeinfo_{ds}.csv")):
                days.append(ds)
            d -= timedelta(days=1)
        return days, total

    # (1) 10月21日〜昨日(日本時間)
    yesterday = (datetime.now(ZoneInfo("Asia/Tokyo")) - timedelta(days=1)).strftime("%Y%m%d")
    if yesterday >= RECENT_START:
        recent, recent_total = missing_days(yesterday, RECENT_START)
    else:
        recent, recent_total = [], 0
    # (2) 10月20日〜4月24日
    past, past_total = missing_days(END_DATE, START_DATE)

    todo = recent + past            # (1)を先に、そのあと(2)
    print("===== 直前情報の収集 =====")
    print(f"(1) {RECENT_START}〜{yesterday}: 残り {len(recent)} / {recent_total} 日")
    print(f"(2) {START_DATE}〜{END_DATE}: 残り {len(past)} / {past_total} 日")
    print(f"今回は最大 {max_days} 日分")
    if not todo:
        print("すべての日を取り終わっています。")
        return

    began = time.time()
    done = 0
    for date in todo[:max_days]:
        elapsed = (time.time() - began) / 60
        if elapsed > MAX_MINUTES:
            print(f"時間の上限({MAX_MINUTES}分)を過ぎたので、ここで終わります。")
            break
        print(f"[{date}] 開始({elapsed:.0f}分経過)")
        ok, msg = collect_one_day(date)
        print(f"[{date}] {msg}")
        if ok:
            done += 1

    remain = len(todo) - done
    print()
    print(f"完了: 今回 {done} 日分を保存 / 残り {remain} 日 / 合計 {(time.time() - began) / 60:.0f} 分")


if __name__ == "__main__":
    main()
