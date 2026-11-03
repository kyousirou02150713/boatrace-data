#!/usr/bin/env python3
"""
決まり手(各レースの1着の決まり手)を集めるプログラム(GitHub Actions 用・箱3)

・集めるもの: 公式サイトのレース結果ページにある「決まり手」
    (逃げ・差し・まくり・まくり差し・抜き・恵まれ)
・1レース1行。列: 日付, 場コード, R, 決まり手, 備考, 取得時刻
    決まり手が載っていないレース(中止・不成立など)は、決まり手を空欄にして、備考に理由を書く
・保存先: data/kimarite/kimarite_日付.csv(1日1ファイル。開催なしの日は空のファイル)
・取る順番: 昨日(日本時間)から、2026年4月24日に向かって、新しい日から古い日へ
    → 毎日の分と、過去分のさかのぼりを、同じ仕組みで行う
・すでに kimarite_日付.csv がある日は飛ばす(止まっても、続きから再開できる)
・1回の実行で、最大 MAX_DAYS 日分まで(時間の上限 MAX_MINUTES を過ぎたら、次の日には進まない)
・アクセス間隔 3 秒 / 1 回の待ち時間 30 秒 / だめなら最大 3 回まで取り直す
・既存の収集(collect.py・collect_beforeinfo.py)とは別のファイル・別のフォルダ。
  ここが失敗しても、既存の収集には影響しません。

使い方:
    python collect_kimarite.py              ← いつもの使い方(最大5日分)
    python collect_kimarite.py 3            ← 今回だけ 3 日分にする
    python collect_kimarite.py --summary    ← 取れた日数・行数・決まり手の内訳を表示する(通信なし)
    python collect_kimarite.py --probe 20261028 01 1
                                            ← 1レースだけ試して、読み取りの結果を表示する(保存しない)

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
MAX_DAYS = 5                 # 1回の実行で取る最大の日数
MAX_MINUTES = 140            # この時間を過ぎたら、次の日には進まない(Actions の上限 180 分より前に終える)
FAIL_RATE_LIMIT = 0.10       # 通信エラーで取れなかったレースがこの割合を超えた日は保存しない(次回取り直す)
BLANK_RATE_LIMIT = 0.30      # 「理由の分からない空欄」がこの割合を超えた日は保存しない(ページの形が変わった等に気づくため)
BLANK_CHECK_MIN_RACES = 10   # 上の判定は、レース数がこれ以上の日だけ行う

WAIT_SECONDS = 3             # アクセスの間隔(秒)
TIMEOUT = 30                 # 1回あたり、返事を待つ最大の秒数
RETRY = 3                    # だめだったときに試す最大の回数
RETRY_WAIT = 5               # 取り直す前に待つ秒数

OUT_DIR = os.path.join("data", "kimarite")
SUMMARY_PATH = os.path.join(OUT_DIR, "_summary.txt")   # .csv ではないので、パソコン側の取り込みの対象にならない

COLS = ["日付", "場コード", "R", "決まり手", "備考", "取得時刻"]

# 決まり手の種類(長いものを先に調べる:「まくり差し」を「まくり」と読み間違えないため)
KIMARITE = ["まくり差し", "まくり", "逃げ", "差し", "抜き", "恵まれ"]
# 理由が分かっている空欄(これらは「ページの形が変わった」とは数えない)
KNOWN_BLANK = ("中止", "不成立", "順延")


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
# 番組表から、その日のレースの一覧を作る(直前情報の収集と同じ方法)
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
# レース結果のページから、決まり手を読む
# ======================================================================
def _find_kimarite_word(text):
    """文字列の中から決まり手の言葉を探す(なければ None)"""
    text = unicodedata.normalize("NFKC", text or "")
    for k in KIMARITE:
        if k in text:
            return k
    return None


def parse_result_html(html):
    """結果ページの HTML から (決まり手, 備考) を返す。決まり手が読めないときは ("", 理由)。"""
    soup = BeautifulSoup(html, "html.parser")
    page_text = unicodedata.normalize("NFKC", soup.get_text(" ", strip=True))

    # (1) 「決まり手」という見出しの表の中身を読む(本命の読み方)
    for th in soup.find_all("th"):
        if th.get_text(strip=True) == "決まり手":
            table = th.find_parent("table")
            body = table.find("tbody") if table else None
            cell_text = (body or table).get_text(" ", strip=True) if table else ""
            word = _find_kimarite_word(cell_text)
            if word:
                return word, ""
            if cell_text.strip():
                # 見出しはあるが、知らない言葉が入っていた → そのまま残して、備考に印をつける
                return cell_text.strip()[:20], "未知の決まり手"
            return "", "決まり手の欄が空"

    # (2) 見出しの形が違っていたときの保険:「決まり手 ○○」と並んでいる文字を探す
    m = re.search(r"決まり手\s*(逃げ|差し|まくり差し|まくり|抜き|恵まれ)", page_text)
    if m:
        return m.group(1), ""

    # (3) 決まり手がない理由を、ページの文字から読む
    if "不成立" in page_text:
        return "", "不成立"
    if "中止" in page_text:
        return "", "中止"
    if "順延" in page_text:
        return "", "順延"
    if "着順" not in page_text and "レースタイム" not in page_text:
        return "", "結果の表が見つかりません(未発表・中止など)"
    return "", "決まり手の表示なし"


def get_kimarite(date, jcd, rno):
    """戻り値: (決まり手, 備考)。通信エラーのときは ConnectionError を出す"""
    res = polite_get("https://www.boatrace.jp/owpc/pc/race/raceresult",
                     params={"rno": int(rno), "jcd": str(jcd).zfill(2), "hd": str(date)})
    res.encoding = "utf-8"
    if res.status_code != 200:
        return "", f"ページの状態コード {res.status_code}"
    return parse_result_html(res.text)


# ======================================================================
# 1日分を取る
# ======================================================================
def _is_unexplained(note):
    """理由の分からない空欄か(ページの形が変わったときに増える種類)"""
    return bool(note) and note != "未知の決まり手" and note not in KNOWN_BLANK


def save_csv(df, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".part"
    df.to_csv(tmp, index=False, encoding="utf-8-sig")
    os.replace(tmp, path)


def collect_one_day(date, races=None, getter=None):
    """戻り値: (保存したか, メッセージ)。races / getter はテスト用に差し替えられる"""
    getter = getter or get_kimarite
    path = os.path.join(OUT_DIR, f"kimarite_{date}.csv")
    if races is None:
        try:
            races = race_list_from_bangumi(date)
        except Exception as e:
            return False, f"番組表が取れませんでした({e})。次回取り直します"

    if not races:
        save_csv(pd.DataFrame(columns=COLS), path)       # 開催なしの日も、取り終わった印を置く
        return True, "開催なし(空のファイルを保存)"

    rows, comm_errors, problems = [], 0, []
    for jcd, r in races:
        try:
            k, note = getter(date, jcd, r)
        except Exception as e:
            comm_errors += 1
            print(f"    場コード{jcd} {r}R : 通信エラー({str(e)[:60]})")
            problems.append((jcd, r, "通信エラー"))
            continue
        rows.append({"日付": date, "場コード": str(jcd).zfill(2), "R": int(r),
                     "決まり手": k, "備考": note, "取得時刻": now_str()})
        if note:
            problems.append((jcd, r, note))

    if comm_errors > len(races) * FAIL_RATE_LIMIT:
        for jcd, r, why in problems[:30]:
            print(f"    場コード{jcd} {r}R : {why}")
        return False, (f"通信エラーが多い({comm_errors}/{len(races)} レース)ので保存しません。"
                       "次回取り直します")

    unexplained = sum(1 for x in rows if not x["決まり手"] and _is_unexplained(x["備考"]))
    if len(races) >= BLANK_CHECK_MIN_RACES and unexplained > len(races) * BLANK_RATE_LIMIT:
        for jcd, r, why in problems[:30]:
            print(f"    場コード{jcd} {r}R : {why}")
        return False, (f"理由の分からない空欄が多い({unexplained}/{len(races)} レース)ので保存しません。"
                       "公式ページの形が変わった可能性があります")

    # 通信エラーで取れなかったレースは、ファイルには入れず(=空欄の行も作らず)、備考付きの行で残す
    err_rows = [{"日付": date, "場コード": str(j).zfill(2), "R": int(r), "決まり手": "",
                 "備考": why, "取得時刻": now_str()}
                for j, r, why in problems if why == "通信エラー"]
    df = pd.DataFrame(rows + err_rows, columns=COLS)
    df = df.sort_values(["場コード", "R"]).reset_index(drop=True)
    save_csv(df, path)

    got = int((df["決まり手"] != "").sum())
    print(f"    決まり手が取れたレース {got}/{len(races)}")
    if problems:
        print(f"    決まり手がない・取れなかったレース: {len(problems)} 件")
        for jcd, r, why in problems[:30]:
            print(f"      場コード{jcd} {r}R : {why}")
        if len(problems) > 30:
            print(f"      …ほか {len(problems) - 30} 件")
    return True, f"保存しました: {path}"


# ======================================================================
# まとめ(日数・行数・決まり手の内訳)
# ======================================================================
def _race_weather_winners(date):
    """その日の race_weather から、1着の艇があるレース数を数える(なければ None)"""
    cands = glob.glob(os.path.join("data", date, f"race_weather_{date}.csv"))
    if not cands:
        return None
    try:
        df = pd.read_csv(cands[0], encoding="utf-8-sig", dtype={"場コード": str})
        w = df[pd.to_numeric(df["着順_数字"], errors="coerce") == 1]
        return w.groupby(["場コード", "R"]).ngroups
    except Exception:
        return None


def make_summary():
    files = sorted(glob.glob(os.path.join(OUT_DIR, "kimarite_*.csv")))
    lines = ["===== 決まり手の収集 まとめ =====",
             f"作成: {now_str()}"]
    if not files:
        lines.append("まだ1日分も集まっていません。")
        return "\n".join(lines)
    frames, empty_days = [], 0
    for f in files:
        try:
            d = pd.read_csv(f, encoding="utf-8-sig", dtype=str).fillna("")
        except Exception:
            continue
        if len(d) == 0:
            empty_days += 1
        else:
            frames.append(d)
    allrows = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=COLS)
    dates = [os.path.basename(f)[9:17] for f in files]
    lines.append(f"日数: {len(files)} 日({min(dates)}〜{max(dates)}) / うち開催なし {empty_days} 日")
    lines.append(f"行数(レース数): {len(allrows)}")
    lines.append("決まり手の内訳:")
    vc = allrows["決まり手"].replace("", "(空欄)").value_counts()
    for k in KIMARITE + ["(空欄)"]:
        n = int(vc.get(k, 0))
        pct = n / max(1, len(allrows)) * 100
        lines.append(f"    {k:<6} {n:>7} 件 ({pct:4.1f}%)")
    others = [k for k in vc.index if k not in KIMARITE and k != "(空欄)"]
    for k in others:
        lines.append(f"    {k} {int(vc[k])} 件 ← 想定外の言葉")
    blanks = allrows[allrows["決まり手"] == ""]
    if len(blanks):
        lines.append("空欄の理由:")
        for why, n in blanks["備考"].replace("", "(理由なし)").value_counts().items():
            lines.append(f"    {why}: {int(n)} 件")

    # 全期間が終わっているか
    yesterday = (datetime.now(ZoneInfo("Asia/Tokyo")) - timedelta(days=1)).strftime("%Y%m%d")
    missing = []
    d = datetime.strptime(START_DATE, "%Y%m%d")
    while d.strftime("%Y%m%d") <= yesterday:
        ds = d.strftime("%Y%m%d")
        if not os.path.exists(os.path.join(OUT_DIR, f"kimarite_{ds}.csv")):
            missing.append(ds)
        d += timedelta(days=1)
    if missing:
        lines.append(f"まだ取っていない日: {len(missing)} 日(例: {missing[0]})")
    else:
        lines.append(f"{START_DATE}〜{yesterday} は、すべて取り終わっています。")

    # race_weather(着順)と件数を突き合わせる(リポジトリに race_weather がある日だけ)
    checked = mismatch = 0
    detail = []
    for f in files:
        ds = os.path.basename(f)[9:17]
        n_rw = _race_weather_winners(ds)
        if n_rw is None:
            continue
        try:
            d = pd.read_csv(f, encoding="utf-8-sig", dtype=str).fillna("")
        except Exception:
            continue
        n_k = int((d["決まり手"] != "").sum()) if len(d) else 0
        checked += 1
        if n_k != n_rw:
            mismatch += 1
            if len(detail) < 10:
                detail.append(f"    {ds}: 決まり手 {n_k} 件 / 1着の艇があるレース {n_rw} 件")
    lines.append(f"着順データとの突き合わせ: {checked} 日分を確認 / 件数が合わない日 {mismatch} 日")
    lines.extend(detail)
    return "\n".join(lines)


# ======================================================================
# メイン
# ======================================================================
def main():
    args = sys.argv[1:]
    if args and args[0] == "--summary":
        print(make_summary())
        return
    if args and args[0] == "--probe":
        if len(args) < 4:
            print("使い方: python collect_kimarite.py --probe 日付 場コード R   (例: --probe 20261028 01 1)")
            return
        date, jcd, r = args[1], args[2], int(args[3])
        res = polite_get("https://www.boatrace.jp/owpc/pc/race/raceresult",
                         params={"rno": r, "jcd": str(jcd).zfill(2), "hd": date})
        res.encoding = "utf-8"
        k, note = parse_result_html(res.text)
        print(f"状態コード {res.status_code} / 読み取り結果: 決まり手='{k}' 備考='{note}'")
        txt = unicodedata.normalize("NFKC", BeautifulSoup(res.text, "html.parser").get_text(" ", strip=True))
        i = txt.find("決まり手")
        print("「決まり手」の前後の文字:", txt[max(0, i - 40): i + 60] if i >= 0 else "(「決まり手」の文字がページにありません)")
        return

    max_days = int(args[0]) if args and args[0].strip().isdigit() else MAX_DAYS
    if not shutil.which("lha"):
        print("lha(解凍の道具)が見つかりません。lhasa をインストールしてください。")
        sys.exit(1)
    os.makedirs(OUT_DIR, exist_ok=True)

    yesterday = (datetime.now(ZoneInfo("Asia/Tokyo")) - timedelta(days=1)).strftime("%Y%m%d")
    todo, total = [], 0
    d = datetime.strptime(yesterday, "%Y%m%d")
    stop = datetime.strptime(START_DATE, "%Y%m%d")
    while d >= stop:                       # 昨日から古い日へ
        ds = d.strftime("%Y%m%d")
        total += 1
        if not os.path.exists(os.path.join(OUT_DIR, f"kimarite_{ds}.csv")):
            todo.append(ds)
        d -= timedelta(days=1)

    print("===== 決まり手の収集 =====")
    print(f"{START_DATE}〜{yesterday}: 残り {len(todo)} / {total} 日")
    print(f"今回は最大 {max_days} 日分")
    if not todo:
        print("すべての日を取り終わっています。")
    else:
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
        print()
        print(f"完了: 今回 {done} 日分を保存 / 残り {len(todo) - done} 日 / 合計 {(time.time() - began) / 60:.0f} 分")

    # まとめを表示(全期間が終わっていれば、確認用のファイルにも書く)
    text = make_summary()
    print()
    print(text)
    if "すべて取り終わっています" in text.splitlines()[-1] or "すべて取り終わっています" in text:
        try:
            with open(SUMMARY_PATH, "w", encoding="utf-8") as f:
                f.write(text + "\n")
        except Exception:
            pass


if __name__ == "__main__":
    main()
