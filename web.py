from flask import Flask, render_template, request, redirect, url_for, session
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
import os
from sqlalchemy.exc import IntegrityError
from datetime import datetime, time
import psycopg2  # PostgreSQL用
from typing import Optional

app = Flask(__name__)

# PostgreSQLの接続設定（render環境用）
DATABASE_URL = os.environ.get('DATABASE_URL', 'sqlite:///school3.db')
app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['DEBUG'] = True  # デバッグモードを有効にする
app.secret_key = os.environ.get("APP_SECRET", "dev-secret")
db = SQLAlchemy(app)
migrate = Migrate(app, db)  # Flask-Migrateの設定

# ====== DB Utils ======
def require_logs_auth(view_func):
    """ /logs 用の簡易パスワード認証 """
    @wraps(view_func)
    def wrapper(*args, **kwargs):
        if session.get("logs_ok"):
            return view_func(*args, **kwargs)
        return redirect(url_for("logs_login", next=request.path))
    return wrapper

def get_conn():
    """ PostgreSQL接続を取得（render用） """
    conn = psycopg2.connect(os.environ['DATABASE_URL'])
    conn.autocommit = True
    return conn

def column_exists(table: str, column: str) -> bool:
    """ 指定テーブルにカラムが存在するか確認 """
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(f"SELECT column_name FROM information_schema.columns WHERE table_name = '{table}'")
        columns = [row[0] for row in cur.fetchall()]
        return column in columns

# ====== Masters ======
def fetch_students():
    """List of students with gakka name."""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT s.学科ID, s.学生番号, s.生徒名, g.学科名
            FROM 生徒 s
            JOIN 学科 g ON g.学科ID = s.学科ID
            ORDER BY s.学科ID, s.学生番号
        """)
        return cur.fetchall()

def fetch_gakkas():
    """List of gakkas."""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT 学科ID, 学科名 FROM 学科 ORDER BY 学科ID")
        return cur.fetchall()

def get_official_student(学生番号: int, 学科ID: int) -> Optional[str]:
    """Get official student name from master."""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT 生徒名 FROM 生徒 WHERE 学生番号 = %s AND 学科ID = %s", (学生番号, 学科ID))
        row = cur.fetchone()
        return row[0] if row else None

def get_gakka_id_by_name(学科名: str) -> Optional[int]:
    """Resolve 学科名 -> 学科ID."""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT 学科ID FROM 学科 WHERE 学科名 = %s", (学科名,))
        row = cur.fetchone()
        return row[0] if row else None

# ====== TimeTable Utils ======
def _next_subject_id(conn):
    cur = conn.cursor()
    cur.execute("SELECT COALESCE(MAX(授業科目ID), 0) + 1 FROM 授業科目")
    return cur.fetchone()[0]

def _parse_int(value, default=None):
    try:
        return int(value)
    except Exception:
        return default

def _parse_hhmm_or_hhmmss(s: str) -> time:
    """'8:50' / '08:50' / '08:50:00' を time に変換"""
    s = (s or "").strip()
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).time()
        except ValueError:
            pass
    parts = s.split(":")
    if len(parts) == 2:
        h, m = parts
        h = h.zfill(2)
        m = m.zfill(2)
        return datetime.strptime(f"{h}:{m}", "%H:%M").time()
    raise ValueError(f"Invalid time format: {s}")

def get_subject_name_by_id(subject_id):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT 授業科目名 FROM 授業科目 WHERE 授業科目ID = %s", (subject_id,))
        row = cur.fetchone()
        return row[0] if row else '未設定'

def load_timetable():
    """TimeTable を読み込み、(period, start, end) の dict のリストを返す（時限昇順）。"""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT 時限, 開始時刻, 終了時刻
            FROM TimeTable
            ORDER BY 時限
        """)
        rows = cur.fetchall()
    result = []
    for r in rows:
        start_t = _parse_hhmm_or_hhmmss(r["開始時刻"])
        end_t   = _parse_hhmm_or_hhmmss(r["終了時刻"])
        result.append({
            "period": r["時限"],
            "start":  start_t,
            "end":    end_t
        })
    return result

def resolve_period_for(ts_dt: datetime):
    ttable = load_timetable()
    if not ttable:
        return None
    t = ts_dt.time()

    for rec in ttable:
        if rec["start"] <= t < rec["end"]:
            return rec

    first_rec = ttable[0]
    last_rec  = ttable[-1]
    if t < first_rec["start"]:
        return first_rec
    if t >= last_rec["end"]:
        return last_rec
    for i in range(len(ttable)-1):
        if ttable[i]["end"] <= t < ttable[i+1]["start"]:
            return ttable[i+1]
    return last_rec

# ====== Common Utils ======
def normalize_ts(ts_input: Optional[str]) -> Optional[str]:
    if not ts_input:
        return None
    s = ts_input.strip().replace('T', ' ')
    fmts = ["%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"]
    for f in fmts:
        try:
            dt = datetime.strptime(s, f)
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
    return None

# 時間割テーブル
class TimeTable(db.Model):
    __tablename__ = 'TimeTable'
    id = db.Column(db.Integer, primary_key=True)
    時限 = db.Column(db.SmallInteger, nullable=False)
    開始時刻 = db.Column(db.Time, nullable=False)
    終了時刻 = db.Column(db.Time, nullable=False)
    備考 = db.Column(db.Text, nullable=True)

# 入退室_入力テーブル
class 入退室_入力(db.Model):
    __tablename__ = '入退室_入力'
    記録ID = db.Column(db.Integer, primary_key=True)
    学生番号 = db.Column(db.Integer, nullable=False)
    生徒名 = db.Column(db.String(32), nullable=False)
    学科ID = db.Column(db.SmallInteger, nullable=False)
    入退出時間 = db.Column(db.DateTime, nullable=False)
    入室区分 = db.Column(db.String(10), nullable=False)

# 授業計画テーブル
class 授業計画(db.Model):
    __tablename__ = '授業計画'
    日付 = db.Column(db.Date, primary_key=True)
    期 = db.Column(db.SmallInteger, nullable=False)
    授業曜日 = db.Column(db.SmallInteger, nullable=False)
    備考 = db.Column(db.String(50), nullable=True)

# 期マスタテーブル
class 期マスタ(db.Model):
    __tablename__ = '期マスタ'
    期ID = db.Column(db.SmallInteger, primary_key=True)
    期名 = db.Column(db.String(32), nullable=False)
    備考 = db.Column(db.String(50), nullable=True)

# カメラログテーブル
class カメラログ(db.Model):
    __tablename__ = 'カメラログ'
    id = db.Column(db.Integer, primary_key=True)
    記録時刻 = db.Column(db.String, nullable=False)
    ソース = db.Column(db.String, nullable=True)
    ステータス = db.Column(db.String, nullable=True)
    マーカー名 = db.Column(db.String, nullable=True)
    スコア = db.Column(db.Float, nullable=True)
    メッセージ = db.Column(db.String, nullable=True)

# 学科テーブル
class 学科(db.Model):
    __tablename__ = '学科'
    学科ID = db.Column(db.SmallInteger, primary_key=True)
    学科名 = db.Column(db.String(32), nullable=False)
    備考 = db.Column(db.String(50), nullable=True)

# 教室テーブル
class 教室(db.Model):
    __tablename__ = '教室'
    教室ID = db.Column(db.SmallInteger, primary_key=True)
    教室名 = db.Column(db.String(32), nullable=False)
    収容人数 = db.Column(db.SmallInteger, nullable=False)
    備考 = db.Column(db.String(50), nullable=True)

# 生徒テーブル
class 生徒(db.Model):
    __tablename__ = '生徒'
    学科ID = db.Column(db.Integer, db.ForeignKey('学科.学科ID'), primary_key=True)
    学生番号 = db.Column(db.Integer, primary_key=True)
    生徒名 = db.Column(db.String(100), nullable=False)
    備考 = db.Column(db.Text, nullable=True)

# 授業科目テーブル
class 授業科目(db.Model):
    __tablename__ = '授業科目'
    授業科目ID = db.Column(db.SmallInteger, primary_key=True)
    授業科目名 = db.Column(db.String(32), nullable=False)
    学科ID = db.Column(db.SmallInteger, db.ForeignKey('学科.学科ID'), nullable=False)
    単位 = db.Column(db.SmallInteger, nullable=False)
    学科フラグ = db.Column(db.SmallInteger, nullable=False)
    備考 = db.Column(db.String(50), nullable=True)

# 曜日マスタテーブル
class 曜日マスタ(db.Model):
    __tablename__ = '曜日マスタ'
    曜日ID = db.Column(db.SmallInteger, primary_key=True)
    曜日名 = db.Column(db.String(10), nullable=False)
    備考 = db.Column(db.String(50), nullable=True)

# 週時間割テーブル
class 週時間割(db.Model):
    __tablename__ = '週時間割'
    年度 = db.Column(db.Integer, primary_key=True)
    学科ID = db.Column(db.SmallInteger, db.ForeignKey('学科.学科ID'), nullable=False)
    期 = db.Column(db.SmallInteger, nullable=False)
    曜日 = db.Column(db.SmallInteger, db.ForeignKey('曜日マスタ.曜日ID'), nullable=False)
    時限 = db.Column(db.SmallInteger, nullable=False)
    科目ID = db.Column(db.SmallInteger, db.ForeignKey('授業科目.授業科目ID'), nullable=True)
    教室ID = db.Column(db.SmallInteger, db.ForeignKey('教室.教室ID'), nullable=True)
    備考 = db.Column(db.String(50), nullable=True)

@app.cli.command('init-db')
def init_db_command():
    """データベースを初期化し、データを投入する"""
    from datetime import datetime

    # データ投入ロジック
    with app.app_context():
        try:
            # --- TimeTableデータ投入 ---
            timetable_data = [
                (1, time(8, 50), time(10, 30), "1限目"),
                (2, time(10, 35), time(12, 15), "2限目"),
                (3, time(13, 0), time(14, 40), "3限目"),
                (4, time(14, 45), time(16, 25), "4限目"),
                (5, time(16, 40), time(18, 20), "5限目")
            ]
            for data in timetable_data:
                timetable = TimeTable(時限=data[0], 開始時刻=data[1], 終了時刻=data[2], 備考=data[3])
                db.session.add(timetable)

            # --- 授業計画データ投入 ---
            授業計画_data = [
                ('2025-04-08', 1, 2),('2025-04-09', 1, 3),('2025-04-10', 1, 4),('2025-04-11', 1, 5),('2025-04-14', 1, 1),
                ('2025-04-15', 1, 2),('2025-04-16', 1, 3),('2025-04-17', 1, 4),('2025-04-18', 1, 5),('2025-04-21', 1, 1),
                ('2025-04-22', 1, 2),('2025-04-23', 1, 3),('2025-04-24', 1, 4),('2025-04-25', 1, 5),('2025-04-28', 1, 1),
                ('2025-05-07', 1, 3),('2025-05-08', 1, 4),('2025-05-09', 1, 5),('2025-05-12', 1, 1),('2025-05-13', 1, 2),
                ('2025-05-15', 1, 4),('2025-05-16', 1, 5),('2025-05-19', 1, 1),('2025-05-20', 1, 2),('2025-05-21', 1, 3),
                ('2025-05-22', 1, 4),('2025-05-23', 1, 5),('2025-05-26', 1, 1),('2025-05-27', 1, 2),('2025-05-28', 1, 3),
                ('2025-05-29', 1, 4),('2025-05-30', 1, 5),('2025-06-02', 1, 1),('2025-06-03', 1, 2),('2025-06-04', 1, 3),
                ('2025-06-05', 1, 4),('2025-06-06', 1, 5),('2025-06-09', 1, 1),('2025-06-10', 1, 2),('2025-06-11', 1, 3),
                ('2025-06-12', 1, 4),('2025-06-13', 1, 5),('2025-06-16', 1, 1),('2025-06-17', 1, 2),('2025-06-18', 1, 3),
                ('2025-06-19', 2, 4),('2025-06-20', 2, 5),('2025-06-23', 2, 1),('2025-06-24', 2, 2),('2025-06-25', 2, 3),
                ('2025-06-26', 2, 4),('2025-06-27', 2, 5),('2025-06-30', 2, 1),('2025-07-01', 2, 2),('2025-07-02', 2, 3),
                ('2025-07-03', 2, 4),('2025-07-04', 2, 5),('2025-07-07', 2, 1),('2025-07-08', 2, 2),('2025-07-09', 2, 3),
                ('2025-07-10', 2, 4),('2025-07-11', 2, 5),('2025-07-14', 2, 1),('2025-07-15', 9, 0),('2025-07-16', 9, 0),
                ('2025-07-17', 9, 0),('2025-07-18', 9, 0),('2025-07-21', 9, 0),('2025-07-22', 9, 0),('2025-07-23', 9, 0),
                ('2025-07-24', 9, 0),('2025-07-25', 9, 0),('2025-08-20', 2, 3),('2025-08-21', 2, 4),('2025-08-22', 2, 5),
                ('2025-08-23', 2, 2),('2025-08-25', 2, 1),('2025-08-26', 2, 2),('2025-08-27', 2, 3),('2025-08-28', 2, 4),
                ('2025-08-29', 2, 5),('2025-09-01', 2, 1),('2025-09-02', 2, 2),('2025-09-03', 2, 3),('2025-09-04', 2, 4),
                ('2025-09-05', 2, 5),('2025-09-08', 2, 1),('2025-09-09', 2, 2),('2025-09-10', 2, 3),('2025-09-11', 2, 4),
                ('2025-09-12', 2, 5),('2025-09-16', 2, 2),('2025-09-17', 2, 3),('2025-09-18', 2, 1),('2025-09-19', 2, 5),
                ('2025-09-22', 2, 1),('2025-09-24', 2, 3),('2025-09-25', 2, 4),('2025-09-26', 2, 2),('2025-09-29', 2, 0),
                ('2025-09-30', 10, 0),('2025-10-01', 10, 0),('2025-10-02', 10, 0),('2025-10-03', 10, 0),('2025-10-06', 10, 0),
                ('2025-10-07', 10, 0),('2025-10-08', 10, 0),('2025-10-09', 10, 0),('2025-10-10', 10, 0),('2025-10-14', 3, 2),
                ('2025-10-15', 3, 3),('2025-10-16', 3, 4),('2025-10-17', 3, 5),('2025-10-20', 3, 1),('2025-10-21', 3, 2),
                ('2025-10-22', 3, 3),('2025-10-23', 3, 4),('2025-10-24', 3, 5),('2025-10-27', 3, 1),('2025-10-28', 3, 2),
                ('2025-10-29', 3, 3),('2025-10-30', 3, 4),('2025-10-31', 3, 5),('2025-11-04', 3, 2),('2025-11-05', 3, 3),
                ('2025-11-06', 3, 1),('2025-11-07', 3, 5),('2025-11-10', 3, 1),('2025-11-11', 3, 2),('2025-11-12', 3, 3),
                ('2025-11-13', 3, 4),('2025-11-14', 3, 5),('2025-11-17', 3, 1),('2025-11-18', 3, 2),('2025-11-19', 3, 3),
                ('2025-11-20', 3, 4),('2025-11-21', 3, 5),('2025-11-25', 3, 1),('2025-11-26', 3, 3),('2025-11-27', 3, 4),
                ('2025-11-28', 3, 5),('2025-12-01', 3, 1),('2025-12-02', 3, 2),('2025-12-03', 3, 3),('2025-12-04', 3, 4),
                ('2025-12-08', 3, 1),('2025-12-09', 3, 2),('2025-12-10', 3, 3),('2025-12-11', 3, 4),('2025-12-12', 3, 5),
                ('2025-12-15', 3, 1),('2025-12-16', 3, 2),('2025-12-17', 4, 3),('2025-12-18', 3, 4),('2025-12-19', 3, 5),
                ('2025-12-22', 4, 1),('2025-12-23', 4, 2),('2025-12-24', 4, 3),('2025-12-25', 4, 4),('2025-12-26', 4, 5),
                ('2026-01-13', 4, 1),('2026-01-14', 4, 3),('2026-01-15', 4, 4),('2026-01-16', 4, 5),('2026-01-19', 4, 1),
                ('2026-01-20', 4, 2),('2026-01-21', 4, 3),('2026-01-22', 4, 4),('2026-01-23', 4, 5),('2026-01-26', 4, 1),
                ('2026-01-27', 4, 2),('2026-01-28', 4, 3),('2026-01-29', 4, 4),('2026-01-30', 4, 5),('2026-02-02', 4, 1),
                ('2026-02-03', 4, 2),('2026-02-04', 4, 3),('2026-02-06', 4, 5),('2026-02-09', 4, 1),('2026-02-10', 4, 2),
                ('2026-02-12', 4, 4),('2026-02-13', 4, 5),('2026-02-16', 4, 1),('2026-02-17', 4, 2),('2026-02-18', 4, 3),
                ('2026-02-19', 4, 4),('2026-02-20', 4, 5),('2026-02-21', 4, 4),('2026-02-24', 4, 2),('2026-02-25', 4, 3),
                ('2026-02-26', 4, 4),('2026-02-27', 4, 5),('2026-03-02', 4, 1),('2026-03-03', 4, 2),('2026-03-04', 4, 3),
                ('2026-03-05', 4, 4),('2026-03-06', 4, 5),('2026-03-09', 4, 1),('2026-03-10', 4, 2),('2026-03-11', 4, 0)
            ]
            for data in 授業計画_data:
                授業計画_record = 授業計画(日付=data[0], 期=data[1], 授業曜日=data[2])
                db.session.add(授業計画_record)

            # --- 期マスタデータ投入 ---
            期マスタ_data = [
                (1, "Ⅰ"),(2, "Ⅱ"),(3, "Ⅲ"),(4, "Ⅳ"),(5, "Ⅴ"),(6, "Ⅵ"),(7, "Ⅶ"),(8, "Ⅷ"),(9, "前期(Ⅱ期)集中"),
                (10, "後期(Ⅲ期)集中")
            ]
            for data in 期マスタ_data:
                期マスタ_record = 期マスタ(期ID=data[0], 期名=data[1])
                db.session.add(期マスタ_record)

            # --- 学科データ投入 ---
            学科_data = [
                (1, "生産機械システム技術科"), (2, "生産電気システム技術科"), (3, "生産電子情報システム技術科")
            ]
            for data in 学科_data:
                学科_record = 学科(学科ID=data[0], 学科名=data[1])
                db.session.add(学科_record)

            # --- 教室データ投入 ---
            教室_data = [
                (1205, "A205", 20),(2102, "B102/103", 20),(2201, "B201", 20),(2202, "B202", 20),(2204, "B204", 20),
                (2205, "B205", 20),(2301, "B301", 20),(2302, "B302", 20),(2303, "B303", 20),(2304, "B304", 20),
                (2305, "B305", 20),(2306, "B306(視聴覚室)", 20),(3101, "C101(生産ロボット室)", 20),(3103, "C103(開発課題実習室)", 20),
                (3201, "C201", 20),(3202, "C202(応用課程計測制御応用実習室)", 20),(3203, "C203", 20),(3204, "C204", 20),
                (3231, "C231(資料室)", 20),(3301, "C301(マルチメディア実習室)", 20),(3302, "C302(システム開発実習室)", 20),(3303, "C303(システム開発実習室Ⅱ)", 20),
                (3304, "C304/305(応用課程生産管理ネットワーク応用実習室)", 20),(3306, "C306(共通実習室)", 20),(4102, "D102(回路基板加工室)", 20),
                (4201, "D201(開発課題実習室)", 20),(4202, "D202(電子情報技術科教官室)", 20),(4231, "D231(準備室)", 20),
                (4301, "D301", 20),(4302, "D302(PC実習室)", 20)
            ]
            for data in 教室_data:
                教室_record = 教室(教室ID=data[0], 教室名=data[1], 収容人数=data[2])
                db.session.add(教室_record)

            # --- 生徒データ投入 ---
            生徒_data = [
                (1, 1, "青井渓一郎"),(1, 2, "赤坂龍成"),(1, 3, "秋好拓海"),(1, 4, "伊川翔"),(1, 5, "岩切亮太"),
                (1, 6, "上田和輝"),(1, 7, "江本龍之介"),(1, 8, "大久保碧瀧"),(1, 9, "加來涼雅"),(1, 10, "梶原悠平"),
                (1, 11, "管野友富紀"),(1, 12, "髙口翔真"),(1, 13, "古城静雅"),(1, 14, "小柳知也"),(1, 15, "酒元翼"),
                (1, 16, "座光寺孝彦"),(1, 17, "佐野勇太"),(1, 18, "清水健心"),(1, 19, "新谷雄飛"),(1, 20, "関原響樹"),
                (1, 21, "髙橋優人"),(1, 22, "武富義樹"),(1, 23, "内藤俊介"),(1, 24, "野田千尋"),(1, 25, "野中雄学"),
                (1, 26, "東奈月"),(1, 27, "古田雅也"),(1, 28, "牧野倭大"),(1, 29, "松隈駿介"),(1, 30, "宮岡嘉熙"),
                (3, 1, "青井渓一郎"),(3, 2, "赤坂龍成"),(3, 3, "秋好拓海"),(3, 4, "伊川翔"),(3, 5, "岩切亮太"),
                (3, 6, "上田和輝"),(3, 7, "江本龍之介"),(3, 8, "大久保碧瀧"),(3, 9, "加來涼雅"),(3, 10, "梶原悠平"),
                (3, 11, "管野友富紀"),(3, 12, "髙口翔真"),(3, 13, "古城静雅"),(3, 14, "小柳知也"),(3, 15, "酒元翼"),
                (3, 16, "座光寺孝彦"),(3, 17, "佐野勇太"),(3, 18, "清水健心"),(3, 19, "新谷雄飛"),(3, 20, "関原響樹"),
                (3, 21, "髙橋優人"),(3, 22, "武富義樹"),(3, 23, "内藤俊介"),(3, 24, "野田千尋"),(3, 25, "野中雄学"),
                (3, 26, "東奈月"),(3, 27, "古田雅也"),(3, 28, "牧野倭大"),(3, 29, "松隈駿介"),(3, 30, "宮岡嘉熙")
            ]
            for data in 生徒_data:
                生徒_record = 生徒(学科ID=data[0], 学生番号=data[1], 生徒名=data[2])
                db.session.add(生徒_record)

            # --- 授業科目データ投入 ---
            授業科目_data = [
                (301, "工業技術英語", 3, 2, 0),(302, "生産管理", 3, 2, 0),
                (303, "品質管理", 3, 2, 0),(304, "経営管理", 3, 2, 0),
                (305, "創造的開発技法", 3, 2, 0),(306, "工業法規", 3, 2, 0),
                (307, "職業能力開発体系論", 3, 2, 0),(308, "機械工学概論", 3, 2, 0),
                (309, "アナログ回路応用設計技術", 3, 2, 0),(310, "ディジタル回路応用設計技術", 3, 2, 0),
                (311, "複合電子回路応用設計技術", 3, 2, 0),(312, "ロボット工学", 3, 2, 0),
                (313, "通信プロトコル実装設計", 3, 2, 0),(314, "セキュアシステム設計", 3, 2, 0),
                (315, "組込システム設計", 3, 4, 0),(316, "安全衛生管理", 3, 2, 0),
                (317, "機械工作・組立実習", 3, 4, 0),(318, "実装設計製作実習", 3, 4, 0),
                (319, "EMC応用実習", 3, 4, 0),(320, "電子回路設計製作応用実習", 3, 4, 0),
                (321, "制御回路設計製作実習", 3, 4, 0),(322, "センシングシステム構築実習", 3, 4, 0),
                (323, "ロボット工学実習", 3, 2, 0),(324, "通信プロトコル実装実習", 3, 4, 0),
                (325, "セキュアシステム構築実習", 3, 4, 0),(326, "生産管理システム構築実習Ⅰ", 3, 2, 0),
                (327, "生産管理システム構築実習Ⅱ", 3, 2, 0),(328, "組込システム構築実習", 3, 4, 0),
                (329, "組込デバイス設計実習", 3, 4, 0),(330, "組込システム構築課題実習", 3, 10, 0),
                (331, "電子通信機器設計制作課題実習", 3, 10, 0),(332, "ロボット機器制作課題実習(電子情報)", 3, 10, 0),
                (333, "ロボット機器運用課題実習(電子情報)", 3, 10, 0),(380, "標準課題Ⅰ", 3, 10, 0),
                (381, "標準課題Ⅱ", 3, 10, 0),(334, "電子装置設計製作応用課題実習", 3, 54, 0),
                (335, "組込システム応用課題実習", 3, 54, 0),(336, "通信システム応用課題実習", 3, 54, 0),
                (337, "ロボットシステム応用課題実習", 3, 54, 0),(390, "開発課題", 3, 54, 0)
            ]
            for data in 授業科目_data:
                授業科目_record = 授業科目(授業科目ID=data[0], 授業科目名=data[1], 学科ID=data[2], 単位=data[3], 学科フラグ=data[4])
                db.session.add(授業科目_record)

            # --- 曜日マスタデータ投入 ---
            曜日マスタ_data = [
                (0, "授業日"), (1, "月曜日"), (2, "火曜日"), (3, "水曜日"), (4, "木曜日"), (5, "金曜日"), (6, "土曜日"),
                (7, "日曜日"), (8, "祝祭日")
            ]
            for data in 曜日マスタ_data:
                曜日マスタ_record = 曜日マスタ(曜日ID=data[0], 曜日名=data[1])
                db.session.add(曜日マスタ_record)

            # --- 週時間割データ投入 ---
            週時間割_data = [
                (2025, 3, 1, 1, 1, 325, 3301, "C304/寺内"),    (2025, 3, 1, 1, 2, 325, 3301, "C304/寺内"),
                (2025, 3, 1, 1, 3, 301, 2201, "/ワット"),    (2025, 3, 1, 1, 4, 313, 3301, "C302/中山"),
                (2025, 3, 1, 2, 1, 314, 3301, "C304/寺内"),    (2025, 3, 1, 2, 2, 309, 3301, "C304/諏訪原"),
                (2025, 3, 1, 2, 3, 310, 3301, "/岡田"),    (2025, 3, 1, 2, 4, 311, 3301, "C302/近藤"),
                (2025, 3, 1, 3, 1, 312, 2301, "B102/玉井"),    (2025, 3, 1, 3, 2, 312, 2301, "B102/玉井"),
                (2025, 3, 1, 4, 1, 315, 3302, "/下泉"),    (2025, 3, 1, 4, 2, 328, 3302, "/下泉"),
                (2025, 3, 1, 4, 3, 322, 3302, "/寺内"),    (2025, 3, 1, 4, 4, 322, 3302, "/寺内"),
                (2025, 3, 1, 5, 1, 315, 3302, "/下泉"),    (2025, 3, 1, 5, 2, 328, 3302, "/下泉"),
                (2025, 3, 1, 5, 3, 318, 3302, "/近藤"),    (2025, 3, 1, 5, 4, 318, 3302, "/近藤"),
                (2025, 3, 2, 1, 1, 325, 3301, "/寺内"),    (2025, 3, 2, 1, 2, 325, 3301, "/寺内"),
                (2025, 3, 2, 1, 3, 301, 2201, "/ワット"),    (2025, 3, 2, 1, 4, 313, 3301, "/中山"),
                (2025, 3, 2, 2, 1, 325, 3301, "/寺内"),    (2025, 3, 2, 2, 2, 309, 3301, "/諏訪原"),
                (2025, 3, 2, 2, 3, 310, 3301, "/岡田"),    (2025, 3, 2, 2, 4, 311, 3302, "/近藤"),
                (2025, 3, 2, 3, 1, 324, 3301, "/中山"),    (2025, 3, 2, 3, 2, 324, 3301, "/中山"),
                (2025, 3, 2, 4, 1, 323, 3101, "/電気系"),    (2025, 3, 2, 4, 2, 323, 3101, "/電気系"),
                (2025, 3, 2, 4, 3, 315, 3302, "/下泉"),    (2025, 3, 2, 4, 4, 328, 3302, "/下泉"),
                (2025, 3, 2, 5, 3, 322, 3302, "/玉井"),    (2025, 3, 2, 5, 4, 322, 3302, "/玉井"),
                (2025, 3, 3, 1, 1, 327, 3301, "/中山"),    (2025, 3, 3, 1, 2, 327, 3301, "/中山"),
                (2025, 3, 3, 1, 3, 380, 3301, "C302/電子情報系"),    (2025, 3, 3, 1, 4, 380, 3301, "C302/電子情報系"),
                (2025, 3, 3, 2, 1, 317, 3302, "K302/機械系"),    (2025, 3, 3, 2, 2, 317, 3302, "K302/機械系"),
                (2025, 3, 3, 2, 3, 380, 3301, "C302/電子情報系"),    (2025, 3, 3, 2, 4, 380, 3301, "C302/電子情報系"),
                (2025, 3, 3, 3, 1, 329, 3301, "/岡田"),    (2025, 3, 3, 3, 2, 329, 3301, "/岡田"),
                (2025, 3, 3, 3, 3, 308, 2301, "/上野"),    (2025, 3, 3, 4, 1, 380, 3301, "C302/電子情報系"),
                (2025, 3, 3, 4, 2, 380, 3301, "C302/電子情報系"),    (2025, 3, 3, 4, 3, 380, 3301, "C302/電子情報系"),
                (2025, 3, 3, 4, 4, 380, 3301, "C302/電子情報系"),    (2025, 3, 3, 5, 1, 321, 3302, "/玉井"),
                (2025, 3, 3, 5, 2, 321, 3302, "/玉井"),    (2025, 3, 3, 5, 3, 380, 3301, "C302/電子情報系"),
                (2025, 3, 3, 5, 4, 380, 3301, "C302/電子情報系"),    (2025, 3, 4, 1, 1, 381, 3302, "C101/電子情報系"),
                (2025, 3, 4, 1, 2, 381, 3302, "C101/電子情報系"),    (2025, 3, 4, 2, 1, 317, 3302, "K302/機械系"),
                (2025, 3, 4, 2, 2, 317, 3302, "K302/機械系"),    (2025, 3, 4, 2, 3, 381, 3302, "C101/電子情報系"),
                (2025, 3, 4, 2, 4, 381, 3302, "C101/電子情報系"),    (2025, 3, 4, 3, 1, 329, 3301, "/岡田"),
                (2025, 3, 4, 3, 2, 329, 3301, "/岡田"),    (2025, 3, 4, 3, 3, 308, 2301, "/上野"),
                (2025, 3, 4, 4, 1, 331, 3302, "C101/電子情報系"),    (2025, 3, 4, 4, 2, 331, 3302, "C101/電子情報系"),
                (2025, 3, 4, 4, 3, 331, 3302, "C101/電子情報系"),    (2025, 3, 4, 4, 4, 331, 3302, "C101/電子情報系"),
                (2025, 3, 4, 5, 1, 331, 3302, "C101/電子情報系"),    (2025, 3, 4, 5, 2, 331, 3302, "C101/電子情報系")
            ]
            for data in 週時間割_data:
                週時間割_record = 週時間割(年度=data[0], 学科ID=data[1], 期=data[2], 曜日=data[3], 時限=data[4], 科目ID=data[5], 教室ID=data[6], 備考=data[7])
                db.session.add(週時間割_record)
            # データベースに反映
            db.session.commit()
            print("データベースの初期化とデータ投入が完了しました！")

        except IntegrityError:
            db.session.rollback()  # エラー時にロールバック
            print("データ投入時にエラーが発生しました。")
        except Exception as e:
            db.session.rollback()  # エラー時にロールバック
            print(f"予期しないエラーが発生しました: {e}")
            
@app.route("/")
def index():
    return "Welcome to the School Management System"

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
