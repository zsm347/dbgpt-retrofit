# -*- coding: utf-8 -*-
"""
Step 1.5：Markdown 语料清洗 + 语言纯度统计

清洗项（全部【跳过代码块】执行）：
    1. MDX import 语句            import X from "..."
    2. HTML/JSX 标签              <div> <img src=.../> </Tabs>
    3. JSX 表达式                 {'/img/x.png'} {`code`}
    4. Docusaurus 容器标记         :::tip :::warning :::（保留内容）
    5. HTML 注释                  <!-- ... -->
    6. frontmatter                --- ... ---（内容与正文 # 标题重复）

统计项：
    · 整篇中文占比分布
    · 纯英文段落（未翻译内容）数量与占比

输出：
    pilot/data/rag_corpus/project_docs/cleaned/       清洗后的语料（保持目录结构）
    pilot/data/rag_corpus/project_docs/_clean_report.md
    pilot/data/rag_corpus/project_docs/_clean_stats.json
"""
import os
import re
import json
from collections import Counter, defaultdict

BASE = '/Users/zsm/Documents/DB-GPT/pilot/data/rag_corpus/project_docs'
ROOT = ('/Users/zsm/Documents/DB-GPT/docs/i18n/zh-CN/'
        'docusaurus-plugin-content-docs/current')
OUT_DIR = os.path.join(BASE, 'cleaned')

# ---------------------------------------------------------------
# 清洗
# ---------------------------------------------------------------
RE_IMPORT = re.compile(r'^\s*import\s+.*?(?:from\s+[\'"][^\'"]*[\'"]|[\'"][^\'"]*[\'"])\s*;?\s*$', re.M)
RE_COMMENT = re.compile(r'<!--[\s\S]*?-->')
RE_CONTAINER = re.compile(r'^\s*:::+\s*[a-zA-Z-]*\s*$', re.M)
RE_TAG = re.compile(r'</?[A-Za-z][A-Za-z0-9]*(?:\s[^<>]*)?/?>')
RE_JSX_EXPR = re.compile(r'\{[^{}\n]{0,120}?\}')

COUNTER = Counter()


def clean_segment(seg, stat):
    """清洗非代码段"""
    n0 = len(seg)

    seg, k = RE_IMPORT.subn('', seg)
    stat['import_lines'] += k

    seg, k = RE_COMMENT.subn('', seg)
    stat['html_comments'] += k

    seg, k = RE_CONTAINER.subn('', seg)
    stat['container_marks'] += k

    # JSX 表达式（如 {'/img/x.png'} {`dbgpt start`}）
    seg, k = RE_JSX_EXPR.subn('', seg)
    stat['jsx_expr'] += k

    seg, k = RE_TAG.subn('', seg)
    stat['html_tags'] += k

    # 收拾残留：连续的空白行压缩
    seg = re.sub(r'[ \t]+\n', '\n', seg)
    seg = re.sub(r'\n{4,}', '\n\n\n', seg)

    stat['chars_removed'] += max(0, n0 - len(seg))
    return seg


def clean(text, stat):
    """按代码块切分，只在非代码段清洗"""
    parts = re.split(r'(```[\s\S]*?```)', text)
    out = []
    for i, p in enumerate(parts):
        if i % 2 == 1:                      # 代码块 → 原样保留
            out.append(p)
            stat['code_blocks_kept'] += 1
        else:
            out.append(clean_segment(p, stat))
    return ''.join(out)


def strip_frontmatter(raw):
    m = re.match(r'^---\s*\n(.*?)\n---\s*\n', raw, re.S)
    if not m:
        return raw, {}
    fm = {}
    for k in ('id', 'title'):
        km = re.search(r'^%s:\s*(.+)$' % k, m.group(1), re.M)
        if km:
            fm[k] = km.group(1).strip().strip('"\'')
    return raw[m.end():], fm


# ---------------------------------------------------------------
# 语言统计
# ---------------------------------------------------------------
RE_CN = re.compile(r'[\u4e00-\u9fff]')
RE_EN = re.compile(r'[A-Za-z]')


def lang_profile(text):
    cn = len(RE_CN.findall(text))
    en = len(RE_EN.findall(text))
    ratio = cn / (cn + en) if (cn + en) else 1.0

    # 段落级：找出"纯英文段落"（中文<5% 且较长）
    en_paras, total_paras = 0, 0
    for para in re.split(r'\n\s*\n', text):
        p = para.strip()
        if len(p) < 60 or p.startswith('```') or p.startswith('|'):
            continue
        total_paras += 1
        c = len(RE_CN.findall(p))
        e = len(RE_EN.findall(p))
        if e > 30 and (c / (c + e) if (c + e) else 0) < 0.05:
            en_paras += 1
    return {'cn_ratio': round(ratio, 3), 'pure_en_paras': en_paras,
            'total_paras': total_paras}


def main():
    manifest = json.load(open(os.path.join(BASE, '_manifest.json'), encoding='utf-8'))
    docs = manifest['docs']
    print('待清洗: %d 份\n' % len(docs))

    stat = Counter()
    report = []
    for i, d in enumerate(docs, 1):
        src = os.path.join(ROOT, d['rel_path'])
        try:
            raw = open(src, encoding='utf-8', errors='ignore').read()
        except Exception as e:
            print('  跳过 %s (%s)' % (d['rel_path'], e))
            continue

        body, fm = strip_frontmatter(raw)
        cleaned = clean(body, stat)

        lp = lang_profile(cleaned)
        rel = d['rel_path']
        report.append({
            'rel_path': rel, 'title': fm.get('title') or d['title'],
            'chars_before': d['chars'],
            'chars_after': len(re.sub(r'\s', '', cleaned)),
            **lp,
        })

        dst = os.path.join(OUT_DIR, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, 'w', encoding='utf-8') as fh:
            fh.write(cleaned)

        if i % 40 == 0:
            print('  已处理 %d/%d' % (i, len(docs)))

    # ---------------- 汇总 ----------------
    cb = sum(r['chars_before'] for r in report)
    ca = sum(r['chars_after'] for r in report)
    print('\n' + '=' * 92)
    print('清洗结果')
    print('=' * 92)
    print('  字数: %s → %s  (减少 %.1f%%)' % (
        format(cb, ','), format(ca, ','), 100.0 * (cb - ca) / cb))
    print()
    print('  清除项统计:')
    print('    HTML/JSX 标签   : %d 处' % stat['html_tags'])
    print('    JSX 表达式      : %d 处' % stat['jsx_expr'])
    print('    MDX import 语句 : %d 行' % stat['import_lines'])
    print('    Docusaurus 容器 : %d 处' % stat['container_marks'])
    print('    HTML 注释       : %d 处' % stat['html_comments'])
    print('    保留的代码块    : %d 个' % stat['code_blocks_kept'])

    # 语言纯度
    print('\n' + '=' * 92)
    print('语言纯度（中文占比）')
    print('=' * 92)
    buckets = [(0, .2), (.2, .4), (.4, .6), (.6, .8), (.8, 1.01)]
    for lo, hi in buckets:
        n = sum(1 for r in report if lo <= r['cn_ratio'] < hi)
        print('  中文占比 %3d%%~%3d%% : %3d 份 %s' % (
            int(lo * 100), int(hi * 100), n,
            '#' * min(int(n / 3) + (1 if n else 0), 55)))
    low = [r for r in report if r['cn_ratio'] < 0.4]
    print('\n  ⚠️ 中文占比 <40%% 的文档: %d 份' % len(low))
    for r in sorted(low, key=lambda x: x['cn_ratio'])[:12]:
        print('     %-56s 中文占比 %.0f%%  纯英段落 %d/%d' % (
            r['rel_path'][:54], r['cn_ratio'] * 100,
            r['pure_en_paras'], r['total_paras']))

    tot_en_paras = sum(r['pure_en_paras'] for r in report)
    tot_paras = sum(r['total_paras'] for r in report)
    print('\n  全文纯英文段落: %d / %d (%.1f%%)' % (
        tot_en_paras, tot_paras, 100.0 * tot_en_paras / max(tot_paras, 1)))

    # ---------------- 落盘 ----------------
    json.dump({'clean_stat': dict(stat), 'report': report},
              open(os.path.join(BASE, '_clean_stats.json'), 'w', encoding='utf-8'),
              ensure_ascii=False, indent=2)

    with open(os.path.join(BASE, '_clean_report.md'), 'w', encoding='utf-8') as fh:
        W = fh.write
        W('# 语料清洗报告\n\n')
        W('## 清洗项统计\n\n| 项目 | 数量 |\n|---|---|\n')
        W('| HTML/JSX 标签 | %d 处 |\n' % stat['html_tags'])
        W('| JSX 表达式 | %d 处 |\n' % stat['jsx_expr'])
        W('| MDX import 语句 | %d 行 |\n' % stat['import_lines'])
        W('| Docusaurus 容器标记 | %d 处 |\n' % stat['container_marks'])
        W('| HTML 注释 | %d 处 |\n' % stat['html_comments'])
        W('| **保留的代码块** | %d 个 |\n' % stat['code_blocks_kept'])
        W('\n## 体量变化\n\n')
        W('- 清洗前: %s 字\n- 清洗后: **%s 字**\n- 减少: %.1f%%\n' % (
            format(cb, ','), format(ca, ','), 100.0 * (cb - ca) / cb))
        W('\n## 语言纯度\n\n| 中文占比区间 | 文档数 |\n|---|---|\n')
        for lo, hi in buckets:
            n = sum(1 for r in report if lo <= r['cn_ratio'] < hi)
            W('| %d%%~%d%% | %d |\n' % (int(lo * 100), int(hi * 100), n))
        W('\n**全文纯英文段落: %d / %d (%.1f%%)**\n\n' % (
            tot_en_paras, tot_paras, 100.0 * tot_en_paras / max(tot_paras, 1)))
        W('\n## 逐篇明细\n\n| 路径 | 清洗前 | 清洗后 | 中文占比 | 纯英段落 |\n|---|---|---|---|---|\n')
        for r in sorted(report, key=lambda x: x['cn_ratio']):
            W('| %s | %d | %d | %.0f%% | %d/%d |\n' % (
                r['rel_path'], r['chars_before'], r['chars_after'],
                r['cn_ratio'] * 100, r['pure_en_paras'], r['total_paras']))

    print('\n输出:')
    print('  清洗后语料: %s/' % OUT_DIR)
    print('  清洗报告  : %s/_clean_report.md' % BASE)
    print('  统计明细  : %s/_clean_stats.json' % BASE)


if __name__ == '__main__':
    main()
