#!/usr/bin/env python3
"""محرّك بحث «أم القرى» و«صوت الحجاز» — خادم محليّ.

  GET /                    الصفحة
  GET /api/search?q=&paper=&limit=   بحث فوري → JSON
  GET /api/crop?key=&i=    قصاصة العمود حول السطر المطابق (مع تظليل أصفر) → JPEG
  GET /api/page?key=       صفحة الجريدة كاملة → JPEG
"""
import sqlite3, re, os, io, json, threading, unicodedata
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from PIL import Image, ImageDraw

SITE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(SITE)                      # صحف الدارة
DB   = os.environ.get('DB_PATH') or os.path.join(BASE, 'فهرس_الصحف.db')
CACHE = os.path.join(SITE, 'كاش'); os.makedirs(CACHE, exist_ok=True)
ORIG  = os.path.join(SITE, 'كاش_أصول'); os.makedirs(ORIG, exist_ok=True)

# النشر: IMG_BASE قاعدةُ روابط صور الصفحات (مثل https://img.example.org) — تُقرأ من البيئة
# لا من الكود؛ فإن ضُبطت حُوِّل /api/page إليها وجُلبت أصول القصاصات منها عند غيابها محليًّا.
IMG_BASE = (os.environ.get('IMG_BASE') or '').rstrip('/')
BIND = os.environ.get('BIND', '127.0.0.1')
PORT = int(os.environ.get('PORT', '8017'))
CROP_CACHE_MAX = 2_000_000_000     # سقف كاش القصاصات بالبايت
ORIG_CACHE_MAX = 6_000_000_000     # سقف كاش الأصول المجلوبة من IMG_BASE

def trim_dir(d, cap):
    """يبقي حجم المجلد تحت السقف بحذف الأقدم وصولًا."""
    fs = []
    for n in os.listdir(d):
        p = os.path.join(d, n)
        try: st = os.stat(p)
        except OSError: continue
        fs.append((st.st_mtime, st.st_size, p))
    total = sum(s for _, s, _ in fs)
    for _, s, p in sorted(fs):
        if total <= cap: break
        try: os.remove(p); total -= s
        except OSError: pass

def page_image_path(rel):
    """مسار صورة الصفحة: محليًّا تحت BASE (مع صدّ path traversal)، وإلا جلبٌ من IMG_BASE بكاش."""
    rel = (rel or '').replace('\\', '/').lstrip('/')
    root = os.path.realpath(BASE)
    p = os.path.realpath(os.path.join(BASE, rel))
    if p.startswith(root + os.sep) and os.path.exists(p):
        return p
    if not IMG_BASE:
        return None
    dst = os.path.join(ORIG, rel.replace('/', '_'))
    if not os.path.exists(dst):
        from urllib.request import urlopen
        from urllib.parse import quote
        tmp = dst + '.tmp'
        try:
            with urlopen(IMG_BASE + '/' + quote(rel), timeout=30) as r, open(tmp, 'wb') as f:
                while True:
                    b = r.read(1 << 20)
                    if not b: break
                    f.write(b)
            os.replace(tmp, dst)
        except Exception:
            try: os.remove(tmp)
            except OSError: pass
            return None
        trim_dir(ORIG, ORIG_CACHE_MAX)
    return dst

TASHKEEL = re.compile('[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED\u0640]')
def norm(s):
    s = unicodedata.normalize('NFKC', s); s = TASHKEEL.sub('', s)
    s = re.sub(r'[أإآٱ]', 'ا', s); s = re.sub(r'ى', 'ي', s); s = re.sub(r'ؤ', 'و', s)
    s = re.sub(r'ئ', 'ي', s); s = re.sub(r'ة', 'ه', s)
    s = re.sub(r'[^\w\s\u0600-\u06FF]', ' ', s)
    return re.sub(r'\s+', ' ', s).strip()

# نمط مرن يطابق الكلمة على النص الخام مهما اختلف رسم الهمزات والتشكيل
CLS = {'ا': '[اأإآٱ]', 'ي': '[يىئ]', 'و': '[وؤ]', 'ه': '[هة]'}
DIA = '[\u0610-\u061A\u064B-\u065F\u0670\u0640]*'
def flex(word):
    return DIA.join(CLS.get(c, re.escape(c)) for c in norm(word))
def flex_phrase(words):
    return (DIA + r'[\sـ]+' + DIA).join(flex(w) for w in words)

_loc = threading.local()
def con():
    if not hasattr(_loc, 'db'):
        _loc.db = sqlite3.connect(f'file:{DB}?mode=ro', uri=True)
    return _loc.db

# ───────── التواريخ: تحليل سطر الترويسة الخام وترميمه ─────────
# حقل greg في الفهرس يحمل سطر التاريخ كما قرأه OCR (أرقام شرقية، هجري وميلادي بأي
# ترتيب، وتشويهات)؛ نحلله هنا، ونرمم الميلادي من الهجري بالتحويل الحسابي عند الحاجة.
EAST = str.maketrans('٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹', '01234567890123456789')

def _jdn(y, m, d):
    a = (14 - m) // 12; yy = y + 4800 - a; mm = m + 12 * a - 3
    return d + (153 * mm + 2) // 5 + 365 * yy + yy // 4 - yy // 100 + yy // 400 - 32045

def _from_jdn(j):
    a = j + 32044; b = (4 * a + 3) // 146097; c2 = a - 146097 * b // 4
    d2 = (4 * c2 + 3) // 1461; e = c2 - 1461 * d2 // 4; m2 = (5 * e + 2) // 153
    return (100 * b + d2 - 4800 + m2 // 10, m2 + 3 - 12 * (m2 // 10),
            e - (153 * m2 + 2) // 5 + 1)

def h2g(hy, hm, hd):
    """هجري (جدولي/كويتي) → ميلادي. مُعايَر: 15-5-1343→12-12-1924، 24-11-1398→26-10-1978."""
    return _from_jdn(hd + (59 * (hm - 1) + 1) // 2 + (hy - 1) * 354
                     + (3 + 11 * hy) // 30 + 1948439)

_H_DMY = re.compile(r'(?<!\d)(\d{1,2})\D{1,3}(\d{1,2})\D{1,3}(1[234]\d\d)(?!\d)')
_H_YMD = re.compile(r'(?<!\d)(1[234]\d\d)\D{1,3}(\d{1,2})\D{1,3}(\d{1,2})(?!\d)')
_G_DMY = re.compile(r'(?<!\d)(\d{1,2})\D{1,3}(\d{1,2})\D{1,3}(19\d\d)(?!\d)')
_G_MYD = re.compile(r'(?<!\d)(\d{1,2})\D{1,3}(19\d\d)\D{1,3}(\d{1,2})(?!\d)')
_G_Y   = re.compile(r'(?<!\d)(19\d\d)(?!\d)')

def parse_date_line(raw):
    """يعيد (هجري=(س,ش,ي)|None، ميلادي=(س,ش|None,ي|None)|None) من سطر خام."""
    s = (raw or '').translate(EAST)
    hij = None
    m = _H_DMY.search(s)
    if m and int(m.group(2)) <= 12 and 1 <= int(m.group(1)) <= 30:
        hij = (int(m.group(3)), int(m.group(2)), int(m.group(1)))
    else:
        m = _H_YMD.search(s)
        if m and int(m.group(2)) <= 12 and 1 <= int(m.group(3)) <= 30:
            hij = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    greg = None
    m = _G_DMY.search(s)
    if m and int(m.group(2)) <= 12 and 1 <= int(m.group(1)) <= 31:
        greg = (int(m.group(3)), int(m.group(2)), int(m.group(1)))
    if greg is None:
        m = _G_MYD.search(s)
        if m and int(m.group(1)) <= 12:
            d = int(m.group(3))
            greg = (int(m.group(2)), int(m.group(1)), d if 1 <= d <= 31 else None)
    if greg is None:
        m = _G_Y.search(s)
        if m: greg = (int(m.group(1)), None, None)
    # ترميم الميلادي الناقص/المشوه من الهجري الكامل
    if hij and (greg is None or greg[1] is None or greg[2] is None):
        g = h2g(*hij)
        if greg is None or abs(g[0] - greg[0]) <= 1:
            greg = g
    return hij, greg

def fmt_date(hij, greg):
    parts = []
    if hij: parts.append('%d-%d-%d هـ' % (hij[2], hij[1], hij[0]))
    if greg:
        y, mo, d = greg
        g = ('%d-%d-%d' % (d, mo, y)) if d and mo else (('%d-%d' % (mo, y)) if mo else '%d' % y)
        parts.append(('الموافق ' if hij else '') + g + ' م')
    return ' '.join(parts)

PAPER_SPAN = {'أم القرى': (1924, 1990, 48.6, 1), 'صوت الحجاز': (1932, 1941, 48.0, 51)}

ISSUE_DATE = {}     # (paper, issue) → {'disp': نص جاهز, 'yf': سنة كسرية}
ANCHORS = {}        # paper → [(issue, yf)] مرتّبة للاستيفاء
def build_issue_dates():
    c = sqlite3.connect(f'file:{DB}?mode=ro', uri=True)
    for paper, issue, page, greg in c.execute(
            "SELECT paper,issue,page,greg FROM pages WHERE greg IS NOT NULL AND greg!='' "
            "ORDER BY paper,issue,page"):
        k = (paper, issue)
        if k in ISSUE_DATE: continue
        hij, g = parse_date_line(greg)
        if g is None: continue
        lo, hi = PAPER_SPAN.get(paper, (1900, 1999, 50, 1))[:2]
        if not (lo <= g[0] <= hi): continue
        yf = g[0] + ((g[1] or 6) - 0.5) / 12
        ISSUE_DATE[k] = {'disp': fmt_date(hij, g), 'yf': yf}
    c.close()
    for (paper, issue), v in ISSUE_DATE.items():
        ANCHORS.setdefault(paper, []).append((issue, v['yf']))
    for paper in ANCHORS:
        ANCHORS[paper].sort()
build_issue_dates()

import bisect
def est_yearf(paper, issue):
    """تقدير سنة عددٍ بلا تاريخ بالاستيفاء بين أقرب عددين مؤرَّخين."""
    lo, hi, slope, first = PAPER_SPAN.get(paper, (1900, 1999, 50, 1))
    an = ANCHORS.get(paper, [])
    if an:
        i = bisect.bisect_left(an, (issue, -1e9))
        left = an[i - 1] if i > 0 else None
        right = an[i] if i < len(an) else None
        if left and right and right[0] != left[0]:
            yf = left[1] + (issue - left[0]) * (right[1] - left[1]) / (right[0] - left[0])
        elif left:  yf = left[1] + (issue - left[0]) / slope
        else:       yf = right[1] - (right[0] - issue) / slope
    else:
        yf = lo + (issue - first) / slope
    return min(max(yf, lo), hi + 0.99)

def year_of(greg, paper, issue):
    """سنة للترتيب: من سطر الصفحة إن حُلّ، وإلا تاريخ العدد، وإلا الاستيفاء."""
    _, g = parse_date_line(greg)
    lo, hi = PAPER_SPAN.get(paper, (1900, 1999))[0:2]
    if g and lo <= g[0] <= hi:
        return g[0] + ((g[1] or 6) - 0.5) / 12
    v = ISSUE_DATE.get((paper, issue))
    return v['yf'] if v else est_yearf(paper, issue)

def parse_query(raw):
    """يعيد (fts_query, أنماط للتظليل).  عبارة بين تنصيص = متلاصقة؛ OR؛ وإلا AND."""
    raw = raw.strip()
    if (raw.startswith('"') and raw.endswith('"')) or (raw.startswith('«') and raw.endswith('»')):
        ws = norm(raw.strip('"«»')).split()
        if not ws: return None, []
        return '"%s"' % ' '.join(ws), [re.compile(flex_phrase(ws))]
    if ' OR ' in raw:
        parts = [p for p in raw.split(' OR ') if norm(p)]
        return ' OR '.join('"%s"' % norm(p) for p in parts), [re.compile(flex_phrase(norm(p).split())) for p in parts]
    ws = [w for w in raw.split() if norm(w)]
    if not ws: return None, []
    return ' AND '.join('"%s"' % norm(w) for w in ws), [re.compile(flex(w)) for w in ws]

def esc(s): return s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

def snippet(txt, pats, width=150):
    spans = []
    for p in pats:
        for m in p.finditer(txt): spans.append((m.start(), m.end()))
    if not spans: return None, 0
    spans.sort(); merged = [list(spans[0])]
    for a, b in spans[1:]:
        if a <= merged[-1][1]: merged[-1][1] = max(merged[-1][1], b)
        else: merged.append([a, b])
    # اقتطاع حول أول إصابة
    a0 = merged[0][0]; start = 0; end = len(txt); pre = post = ''
    if len(txt) > width:
        start = max(0, a0 - width // 2); end = min(len(txt), start + width)
        pre = '…' if start > 0 else ''; post = '…' if end < len(txt) else ''
    out, pos = pre, start
    for a, b in merged:
        if b < start or a > end: continue
        a, b = max(a, start), min(b, end)
        out += esc(txt[pos:a]) + '<mark>' + esc(txt[a:b]) + '</mark>'; pos = b
    out += esc(txt[pos:end]) + post
    return out, len(merged)

def search(qraw, paper=None, limit=400):
    fq, pats = parse_query(qraw)
    if not fq: return {'total_pages': 0, 'shown_pages': 0, 'hits': [], 'capped': False}
    c = con()
    sql = 'SELECT p.paper,p.issue,p.page,p.key,p.greg,p.hijri FROM fts JOIN pages p ON p.key=fts.key WHERE fts MATCH ?'
    par = [fq]
    if paper: sql += ' AND p.paper=?'; par.append(paper)
    pages = c.execute(sql + ' LIMIT 4000', par).fetchall()
    pages.sort(key=lambda r: (year_of(r[4], r[0], r[1]), r[0], r[1], r[2]))
    hits, capped = [], False
    for pp, iss, pg, key, greg, hij in pages:
        rows = c.execute('SELECT i,x,ytop,w,h,txt FROM lines WHERE key=? ORDER BY i', [key]).fetchall()
        scored = []
        for i, x, yt, w, h, t in rows:
            n = sum(1 for p in pats if p.search(t))
            if n: scored.append((-n, i, t))
        scored.sort()
        # تاريخ النتيجة: سطر الصفحة محلَّلًا ومرمَّمًا، وإلا تاريخ العدد، وإلا استيفاء
        ph, pg_date = parse_date_line(greg)
        lo, hi = PAPER_SPAN.get(pp, (1900, 1999))[0:2]
        if pg_date and lo <= pg_date[0] <= hi:
            date, est = fmt_date(ph, pg_date), ''
        else:
            v = ISSUE_DATE.get((pp, iss))
            if v: date, est = v['disp'], ''
            else: date, est = '', '≈ %d م' % int(est_yearf(pp, iss))
        for _, i, t in scored[:6]:
            sn, _n = snippet(t, pats)
            if not sn: continue
            hits.append({'paper': pp, 'issue': iss, 'page': pg, 'key': key,
                         'greg': date, 'est': est, 'hijri': '',
                         'i': i, 'snippet': sn})
            if len(hits) >= limit: capped = True; break
        if capped: break
    return {'total_pages': len(pages), 'shown_pages': len({h["key"] for h in hits}),
            'hits': hits, 'capped': capped}

def ov_ratio(a0, a1, b0, b1):
    o = min(a1, b1) - max(a0, b0)
    return o / max(1e-6, min(a1 - a0, b1 - b0))

def column_window(rows, tgt):
    """حدود عمود السطر المستهدف أفقيًّا [colL,colR] — مشتركة بين القصاصة ونصّها."""
    _, tx, ty, tw, th = tgt[:5]
    xl, xr = tx, tx + tw
    # عمود السطر: أسطرٌ تتداخل أفقيًا ≥ 55٪ مع الهدف، مع استبعاد الأسطر العابرة للصفحة
    same = [r for r in rows if r[3] < 0.55 and ov_ratio(r[1], r[1] + r[3], xl, xr) > 0.55]
    colL = min([r[1] for r in same] + [xl]); colR = max([r[1] + r[3] for r in same] + [xr])
    # مرور ثانٍ لالتقاط بقية أسطر العمود
    same = [r for r in rows if r[3] < 0.55 and ov_ratio(r[1], r[1] + r[3], colL, colR) > 0.55]
    if same:
        colL = min(colL, min(r[1] for r in same)); colR = max(colR, max(r[1] + r[3] for r in same))
    # سقف العرض: عمود الجريدة لا يتجاوز نصف الصفحة
    if colR - colL > 0.5:
        cx = (xl + xr) / 2
        colL, colR = max(0, cx - 0.24), min(1, cx + 0.24)
    return colL, colR

def block_text(key, i):
    """نصّ أسطر العمود التي تعرضها القصاصة نفسها، مع سطر توثيق (المصدر والتاريخ)."""
    c = con()
    pg = c.execute('SELECT paper,issue,page,greg FROM pages WHERE key=?', [key]).fetchone()
    rows = c.execute('SELECT i,x,ytop,w,h,txt FROM lines WHERE key=?', [key]).fetchall()
    tgt = next((r for r in rows if r[0] == int(i)), None)
    if not pg or not tgt: return None
    colL, colR = column_window(rows, tgt)
    y0 = max(0, tgt[2] - 0.12); y1 = min(1, tgt[2] + tgt[4] + 0.12)
    sel = [r for r in rows
           if (r[0] == tgt[0] or (r[3] < 0.55 and ov_ratio(r[1], r[1] + r[3], colL, colR) > 0.55))
           and y0 <= r[2] + r[4] / 2 <= y1]
    sel.sort(key=lambda r: r[2])
    text = '\n'.join(r[5] for r in sel if r[5].strip())
    paper, issue, page, greg = pg
    ph, g = parse_date_line(greg)
    lo, hi = PAPER_SPAN.get(paper, (1900, 1999))[0:2]
    if g and lo <= g[0] <= hi:
        date = fmt_date(ph, g)
    else:
        v = ISSUE_DATE.get((paper, issue))
        date = v['disp'] if v else '≈ %d م تقديرًا' % int(est_yearf(paper, issue))
    cite = f'{paper}، العدد {issue}، ص{page}' + (f'، {date}' if date else '')
    return {'text': text, 'cite': cite, 'copy': f'{text}\n\n— {cite}'}

def make_crop(key, i):
    out = os.path.join(CACHE, f'{key}_{i}.jpg')
    if os.path.exists(out): return out
    c = con()
    img = c.execute('SELECT img FROM pages WHERE key=?', [key]).fetchone()
    if not img: return None
    rows = c.execute('SELECT i,x,ytop,w,h FROM lines WHERE key=?', [key]).fetchall()
    tgt = next((r for r in rows if r[0] == int(i)), None)
    if not tgt: return None
    _, tx, ty, tw, th = tgt
    colL, colR = column_window(rows, tgt)
    x0 = max(0, colL - 0.012); x1 = min(1, colR + 0.012)
    y0 = max(0, ty - 0.12);    y1 = min(1, ty + th + 0.12)
    ip = page_image_path(img[0])
    if not ip: return None
    im = Image.open(ip).convert('RGB')
    W, H = im.size
    cr = im.crop((int(x0 * W), int(y0 * H), int(x1 * W), int(y1 * H)))
    # قصّ الحواف البيضاء قبل التظليل
    from PIL import ImageOps
    bw = ImageOps.invert(cr.convert('L')).point(lambda p: 255 if p > 45 else 0)
    bb = bw.getbbox()
    offx = offy = 0
    if bb:
        pad = max(8, int(cr.width * 0.012))
        bx0 = max(0, bb[0] - pad); by0 = max(0, bb[1] - pad)
        bx1 = min(cr.width, bb[2] + pad); by1 = min(cr.height, bb[3] + pad)
        cr = cr.crop((bx0, by0, bx1, by1)); offx, offy = bx0, by0
    # تظليل السطر المطابق بالأصفر
    ov = Image.new('RGBA', cr.size, (0, 0, 0, 0)); dr = ImageDraw.Draw(ov)
    rx0 = int((tx - x0 - 0.004) * W) - offx; rx1 = int((tx + tw - x0 + 0.004) * W) - offx
    ry0 = int((ty - y0 - 0.004) * H) - offy; ry1 = int((ty + th - y0 + 0.004) * H) - offy
    dr.rectangle([rx0, ry0, rx1, ry1], fill=(255, 214, 10, 70), outline=(212, 160, 23, 160), width=3)
    cr = Image.alpha_composite(cr.convert('RGBA'), ov).convert('RGB')
    sc = 2.0 if cr.width < 800 else (1.6 if cr.width < 1300 else 1.2)
    cr = cr.resize((int(cr.width * sc), int(cr.height * sc)), Image.LANCZOS)
    cr.save(out, quality=90)
    trim_dir(CACHE, CROP_CACHE_MAX)
    return out

MIME = {'.html': 'text/html; charset=utf-8', '.woff2': 'font/woff2',
        '.jpg': 'image/jpeg', '.png': 'image/png', '.svg': 'image/svg+xml'}

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, ctype, body, cache=None):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        if cache: self.send_header('Cache-Control', f'max-age={cache}')
        self.end_headers()
        self.wfile.write(body)
    def _file(self, path, cache=None):
        ext = os.path.splitext(path)[1]
        with open(path, 'rb') as f:
            self._send(200, MIME.get(ext, 'application/octet-stream'), f.read(), cache)
    def do_GET(self):
        try:
            u = urlparse(self.path); q = parse_qs(u.query)
            g = lambda k, d=None: q.get(k, [d])[0]
            if u.path in ('/', '/index.html'):
                return self._file(os.path.join(SITE, 'index.html'))
            if u.path == '/healthz':
                con().execute('SELECT 1')
                return self._send(200, 'text/plain', b'ok')
            if u.path == '/robots.txt':
                return self._send(200, 'text/plain; charset=utf-8',
                                  b'User-agent: *\nAllow: /\nDisallow: /api/\n', cache=86400)
            if u.path == '/api/search':
                res = search(g('q', ''), g('paper') or None, int(g('limit', '400')))
                return self._send(200, 'application/json; charset=utf-8',
                                  json.dumps(res, ensure_ascii=False).encode())
            if u.path == '/api/text':
                r = block_text(g('key', ''), g('i', '0'))
                if r: return self._send(200, 'application/json; charset=utf-8',
                                        json.dumps(r, ensure_ascii=False).encode())
                return self._send(404, 'text/plain', b'not found')
            if u.path == '/api/crop':
                p = make_crop(g('key', ''), g('i', '0'))
                if p: return self._file(p, cache=604800)
                return self._send(404, 'text/plain', b'not found')
            if u.path == '/api/page':
                img = con().execute('SELECT img FROM pages WHERE key=?', [g('key', '')]).fetchone()
                if img and IMG_BASE:
                    from urllib.parse import quote
                    self.send_response(302)
                    self.send_header('Location', IMG_BASE + '/' + quote(img[0]))
                    self.send_header('Cache-Control', 'max-age=604800')
                    self.end_headers(); return
                if img:
                    p = page_image_path(img[0])
                    if p: return self._file(p, cache=604800)
                return self._send(404, 'text/plain', b'not found')
            if u.path.startswith('/%D8%AE%D8%B7%D9%88%D8%B7/') or u.path.startswith('/خطوط/'):
                from urllib.parse import unquote
                name = os.path.basename(unquote(u.path))
                p = os.path.join(SITE, 'خطوط', name)
                if os.path.exists(p): return self._file(p, cache=2592000)
            return self._send(404, 'text/plain', b'404')
        except BrokenPipeError:
            pass
        except Exception:
            # لا تفاصيل داخلية للزائر — التفاصيل إلى سجل الخادم
            import traceback; traceback.print_exc()
            try: self._send(500, 'text/plain; charset=utf-8', 'خطأ داخلي'.encode())
            except Exception: pass

if __name__ == '__main__':
    srv = ThreadingHTTPServer((BIND, PORT), H)
    print(f'http://{BIND}:{PORT}' + (f' | صور: {IMG_BASE}' if IMG_BASE else ''))
    srv.serve_forever()
