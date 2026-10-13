# -*- coding: utf-8 -*-
"""
Word 正文解析器（正文优先版 / L3）

设计要点：
1. 按 body 真实顺序遍历段落与表格，不丢失元素顺序
2. 三通道标题识别：Word 样式 > 中文公文编号正则 > 视觉特征
3. 表格 / 图片以占位符标记位置，保留文档真实结构，便于后续替换为真实内容
4. 输出双产物：Markdown（人可读） + 侧车 JSON（机器可用，带 section_path 元数据）

用法：
    python3 pilot/rag_pipeline/word_parser.py
"""
import os
import re
import json
import glob
import zipfile
import subprocess
from collections import Counter

from docx import Document
from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph
from docx.table import Table


# ============================================================
# 标题识别规则
# ============================================================

# 规范做法：Word 段落样式
STYLE_TITLE = re.compile(r'^(?:Title|标题)$', re.I)
STYLE_HEADING = re.compile(r'(?:Heading|标题)\s*(\d+)', re.I)

# 土办法：中文公文编号（覆盖 90% 以上的中文制度/公文）
# 层级约定：文档标题 H1 > 章 H2 > 条/一、 H3 > （一）/1.1 H4
# 元组第 3 项 = 编号前缀长度上限（关键：判断"编号本身"很短，而非"整行"很短，
#   因为公文体'第一条 本办法所称……'的正文可能长达数百字）
CN_PATTERNS = [
    (2, re.compile(r'^第[一二三四五六七八九十百零〇]+[章篇]'), 12),
    (3, re.compile(r'^第[一二三四五六七八九十百零〇]+[条节]'), 12),
    (3, re.compile(r'^[一二三四五六七八九十]+[、\u3001]'), 6),
    (4, re.compile(r'^[（(][一二三四五六七八九十百]+[）)]'), 8),
    (4, re.compile(r'^\d+(?:\.\d+)+\s'), 12),
]

# 目录行特征（含大量省略号/点线），不作为标题
TOC_HINT = re.compile(r'(?:…{2,}|\.{6,}|·{6,})')

MAX_HEADING_LEN = 60          # 标题长度上限
VISUAL_TITLE_MAX_LEN = 40     # 视觉标题长度上限

# Word 样式层级 → 输出层级（留出 H1 给文档标题）
STYLE_LEVEL_MAP = {1: 2, 2: 3, 3: 4, 4: 5, 5: 6}


def detect_heading(text, style_name, para):
    """
    三通道标题识别，返回 (level, source)；不是标题则返回 (None, None)
    优先级：Word 样式 > 中文编号 > 视觉特征
    """
    t = text.strip()

    # ---- 通道 1：Word 段落样式（语义标签，最可靠）----
    if style_name:
        if STYLE_TITLE.match(style_name):
            return 1, 'style:title'
        m = STYLE_HEADING.search(style_name)
        if m:
            raw = int(m.group(1))
            return STYLE_LEVEL_MAP.get(raw, 4), 'style:%s' % style_name

    # ---- 通道 2：中文公文编号（文本层信息，不依赖样式）----
    # 关键：判断"编号前缀"长度，而非整行长度。
    # 公文体 '第一条 本办法所称……' 的正文字数可达数百，但编号本身只有 3~6 字。
    for lvl, pat, max_prefix in CN_PATTERNS:
        m = pat.match(t)
        if not m:
            continue
        if len(m.group(0)) > max_prefix:                  # 编号本身过长 → 不是标题
            continue
        if pat.pattern.startswith(r'^\d') and len(t) > 80:  # 数字编号：整行过长则排除
            continue
        return lvl, 'regex'

    # ---- 通道 3：视觉特征兜底（居中 + 加粗 + 字号大）----
    if para is not None and 0 < len(t) <= VISUAL_TITLE_MAX_LEN:
        try:
            score = 0
            if para.alignment is not None and 'CENTER' in str(para.alignment):
                score += 1
            runs = [r for r in para.runs if r.text.strip()]
            if runs and all(r.bold for r in runs):
                score += 1
            sizes = [r.font.size.pt for r in runs if r.font.size is not None]
            max_size = max(sizes) if sizes else 0
            if max_size >= 15:
                score += 1
            if max_size >= 20:          # 字号特别大（补"未显式加粗"的大标题）
                score += 1
            if score >= 3:
                # 括号包裹的短行（如"（参考文本）"）降级为副标题，避免污染主层级
                if re.match(r'^[（(][^）)]{0,18}[）)]$', t):
                    return 2, 'visual:sub'
                return 1, 'visual'
        except Exception:
            pass

    # ---- 通道 4：Word 自动编号列表项（编号由 Word 生成，文本里没有"一、"）----
    if para is not None and 0 < len(t) <= 20:
        try:
            pPr = para._p.find(qn('w:pPr'))
            if pPr is not None and pPr.find(qn('w:numPr')) is not None:
                runs = [r for r in para.runs if r.text.strip()]
                if runs and all(r.bold for r in runs):
                    return 2, 'list'
        except Exception:
            pass

    return None, None


def split_head_body(text):
    """
    中文公文常见写法：'第一条 为进一步规范……' —— 条号与正文同行。
    拆分规则按体裁区分：
      · '第X章 / 第X篇'：后面是"章名"（很短，如"总则"）→ 保持整行
      · '第X条 / 第X节'：后面是"条文正文"（可能很长）→ 一律拆分
    """
    m = re.match(r'^(第[一二三四五六七八九十百零〇]+[章节条篇])[\s\u3000\u00a0]*(\S.*)$',
                 text, re.S)
    if not m:
        return text, ''
    head, rest = m.group(1), m.group(2).strip()
    if head.endswith(('章', '篇')):
        return ('%s %s' % (head, rest)).strip(), ''
    return head, rest


# ============================================================
# 元素遍历
# ============================================================

def iter_blocks(doc):
    """按 body 真实顺序产出 ('p', Paragraph) / ('tbl', Table)"""
    for child in doc.element.body.iterchildren():
        if child.tag == qn('w:p'):
            yield 'p', Paragraph(child, doc)
        elif child.tag == qn('w:tbl'):
            yield 'tbl', Table(child, doc)


def para_has_image(p):
    xml = p._p.xml
    return ('<w:drawing' in xml) or ('<w:pict' in xml)


def doc_slug(name):
    s = os.path.splitext(name)[0]
    s = re.sub(r'[\\/:*?"<>|\s]+', '_', s)
    return s[:80]


# ============================================================
# 主解析
# ============================================================

def parse_docx(path, media_dir=None):
    doc = Document(path)
    fname = os.path.basename(path)

    md_lines = []
    elements = []
    heading_stack = []          # [(level, text)]
    stats = Counter()
    n_table = n_image = 0

    def section_path():
        return [h[1] for h in heading_stack]

    for kind, block in iter_blocks(doc):
        # ---------- 表格 ----------
        if kind == 'tbl':
            n_table += 1
            rows = len(block.rows)
            cols = len(block.columns)
            tchars = sum(len(c.text) for r in block.rows for c in r.cells)
            elements.append({
                'type': 'table', 'index': n_table,
                'rows': rows, 'cols': cols, 'chars': tchars,
                'section_path': section_path(),
            })
            md_lines.append('')
            md_lines.append('<!-- TABLE#%d: %d行×%d列, %d字 -->' % (n_table, rows, cols, tchars))
            md_lines.append('')
            stats['table'] += 1
            stats['table_chars'] += tchars
            continue

        # ---------- 段落 ----------
        p = block
        text = (p.text or '').strip()
        has_img = para_has_image(p)

        if has_img:
            n_image += 1
            elements.append({
                'type': 'image', 'index': n_image,
                'anchor': 'inline', 'text_nearby': text[:60],
                'section_path': section_path(),
            })
            md_lines.append('<!-- IMAGE#%d -->' % n_image)
            stats['image'] += 1

        if not text:
            continue

        style_name = p.style.name if p.style is not None else None

        # 目录行：不作为标题，避免污染层级
        if TOC_HINT.search(text):
            elements.append({'type': 'toc', 'text': text, 'section_path': section_path()})
            md_lines.append(text)
            stats['toc'] += 1
            continue

        lvl, src = detect_heading(text, style_name, p)

        if lvl:
            head, rest = split_head_body(text)
            while heading_stack and heading_stack[-1][0] >= lvl:
                heading_stack.pop()
            heading_stack.append((lvl, head))

            elements.append({
                'type': 'heading', 'level': lvl, 'text': head,
                'source': src, 'section_path': section_path(),
            })
            md_lines.append('')
            md_lines.append('%s %s' % ('#' * min(lvl, 6), head))
            md_lines.append('')
            stats['heading'] += 1
            stats['heading_src:%s' % src.split(':')[0]] += 1

            if rest:
                elements.append({'type': 'paragraph', 'text': rest, 'section_path': section_path()})
                md_lines.append(rest)
                stats['para'] += 1
                stats['para_chars'] += len(rest)
        else:
            elements.append({'type': 'paragraph', 'text': text, 'section_path': section_path()})
            md_lines.append(text)
            stats['para'] += 1
            stats['para_chars'] += len(text)

    # ---------- 媒体文件（含浮动图，media 比 inline 统计更全）----------
    media = []
    try:
        with zipfile.ZipFile(path) as z:
            media = [n for n in z.namelist() if n.startswith('word/media/')]
    except Exception:
        pass
    stats['media_files'] = len(media)

    md = '\n'.join(md_lines)
    md = re.sub(r'\n{3,}', '\n\n', md).strip() + '\n'

    result = {
        'doc_id': doc_slug(fname),
        'source_file': fname,
        'stats': {
            'chars_parsed': stats['para_chars'],
            'headings': stats['heading'],
            'paragraphs': stats['para'],
            'tables': stats['table'],
            'table_chars': stats['table_chars'],
            'images_inline': n_image,
            'media_files': stats['media_files'],
            'toc_lines': stats['toc'],
            'heading_source': {
                'style': stats['heading_src:style'],
                'regex': stats['heading_src:regex'],
                'visual': stats['heading_src:visual'],
            },
        },
        'flags': {
            # 只提正文时内容近乎为空的文档 → 需靠表格通道补齐
            'table_heavy': stats['para_chars'] < 200 and stats['table_chars'] > 200,
            'has_no_heading': stats['heading'] == 0,
        },
        'elements': elements,
        'media_files': media,
    }
    return result, md


# ============================================================
# 批量入口
# ============================================================

SRC_DIR = '/Users/zsm/Documents/DB-GPT/pilot/data/rag_corpus/institution_docs'
OUT_DIR = '/Users/zsm/Documents/DB-GPT/pilot/parsed/word'
TMP_DIR = '/Users/zsm/Documents/DB-GPT/pilot/parsed/_converted'


def collect_files():
    """
    收集待解析文件。
    老 .doc（OLE 格式）python-docx 不支持，先用 macOS 自带 textutil 转成 docx。
    """
    files = sorted(glob.glob(os.path.join(SRC_DIR, '*.docx')))

    docs = sorted(glob.glob(os.path.join(SRC_DIR, '*.doc')))
    if docs:
        os.makedirs(TMP_DIR, exist_ok=True)
    for old in docs:
        base = os.path.basename(old)
        if base.startswith('~$'):
            continue
        dst = os.path.join(TMP_DIR, os.path.splitext(base)[0] + '.docx')
        try:
            subprocess.run(['textutil', '-convert', 'docx', '-output', dst, old],
                           check=True, capture_output=True)
            files.append(dst)
            print('[格式转换] %s (.doc → .docx)' % base[:60])
        except Exception as e:
            print('[转换失败] %s: %s' % (base[:60], e))
    return files


def main():
    os.makedirs(OUT_DIR, exist_ok=True)

    files = collect_files()
    report = []

    print('%-58s %7s %5s %5s %5s %5s %5s %s' % (
        '文档', '正文字数', '标题', '段落', '表格', '表字数', '图', '标题来源'))
    print('-' * 130)

    for f in files:
        name = os.path.basename(f)
        if name.startswith('~$'):
            continue
        try:
            result, md = parse_docx(f)
        except Exception as e:
            print('%-58s 失败: %s' % (name[:56], e))
            report.append({'file': name, 'error': str(e)})
            continue

        slug = result['doc_id']
        with open(os.path.join(OUT_DIR, slug + '.md'), 'w', encoding='utf-8') as fh:
            fh.write(md)
        with open(os.path.join(OUT_DIR, slug + '.json'), 'w', encoding='utf-8') as fh:
            json.dump(result, fh, ensure_ascii=False, indent=2)

        s = result['stats']
        hs = s['heading_source']
        src_str = 'style=%d regex=%d visual=%d' % (hs['style'], hs['regex'], hs['visual'])
        flag = ''
        if result['flags']['table_heavy']:
            flag += ' [TABLE-HEAVY]'
        if result['flags']['has_no_heading']:
            flag += ' [NO-HEADING]'

        print('%-58s %7d %5d %5d %5d %5d %5d %s%s' % (
            name[:56], s['chars_parsed'], s['headings'], s['paragraphs'],
            s['tables'], s['table_chars'], s['images_inline'], src_str, flag))

        report.append({'file': name, 'doc_id': slug, **s, 'flags': result['flags']})

    # 汇总报告
    rp = os.path.join(OUT_DIR, '_parse_report.md')
    with open(rp, 'w', encoding='utf-8') as fh:
        fh.write('# Word 解析质量报告\n\n')
        fh.write('| 文档 | 正文字数 | 标题 | 段落 | 表格 | 表格字数 | 图 | 标题来源 | 标记 |\n')
        fh.write('|---|---|---|---|---|---|---|---|---|\n')
        for r in report:
            if 'error' in r:
                fh.write('| %s | - | - | - | - | - | - | 解析失败: %s | |\n' % (r['file'], r['error']))
                continue
            hs = r['heading_source']
            flags = []
            if r['flags']['table_heavy']:
                flags.append('表格为主')
            if r['flags']['has_no_heading']:
                flags.append('无标题')
            fh.write('| %s | %d | %d | %d | %d | %d | %d | style=%d/regex=%d/visual=%d | %s |\n' % (
                r['file'], r['chars_parsed'], r['headings'], r['paragraphs'],
                r['tables'], r['table_chars'], r['images_inline'],
                hs['style'], hs['regex'], hs['visual'], ','.join(flags) or '-'))
    print('\n输出目录: %s' % OUT_DIR)
    print('质量报告: %s' % rp)


if __name__ == '__main__':
    main()
