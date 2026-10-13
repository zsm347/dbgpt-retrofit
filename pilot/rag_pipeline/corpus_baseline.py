# -*- coding: utf-8 -*-
"""
Step 1：Markdown 语料基线筛选

目的：从项目自带的文档里，按【基础可用性】筛出实验语料集。
     只做可用性过滤（空文档 / 极短文档 / 纯索引页），
     保留其余全部自然分布 —— 不为了"实验好看"而刻意挑文档。

输出：
    pilot/data/rag_corpus/project_docs/_manifest.json   语料清单（机器用）
    pilot/data/rag_corpus/project_docs/_report.md       筛选报告（人可读）

用法：
    python3 pilot/rag_pipeline/corpus_baseline.py
"""
import os
import re
import json
import glob
from collections import Counter

SRC = ('/Users/zsm/Documents/DB-GPT/docs/i18n/zh-CN/'
       'docusaurus-plugin-content-docs/current')
OUT_DIR = '/Users/zsm/Documents/DB-GPT/pilot/data/rag_corpus/project_docs'

MIN_CHARS = 500          # 少于 500 字的文档无法构成有意义的分块实验
EST_CHUNK = 512          # 估算 chunk 数用的基准 chunk_size


def analyze(path, root):
    raw = open(path, encoding='utf-8', errors='ignore').read()

    # 拆 frontmatter
    fm = {}
    body = raw
    m = re.match(r'^---\s*\n(.*?)\n---\s*\n', raw, re.S)
    if m:
        block = m.group(1)
        body = raw[m.end():]
        for k in ('id', 'title', 'sidebar_label'):
            km = re.search(r'^%s:\s*(.+)$' % k, block, re.M)
            if km:
                fm[k] = km.group(1).strip().strip('"\'')

    chars = len(re.sub(r'\s', '', body))
    levels = {lv: len(re.findall(r'^%s [^#]' % ('#' * lv), body, re.M))
              for lv in (1, 2, 3, 4)}
    max_depth = max([lv for lv in levels if levels[lv] > 0] or [0])
    return {
        'path': path,
        'rel_path': os.path.relpath(path, root),
        'top_dir': os.path.relpath(path, root).split('/')[0],
        'title': fm.get('title') or os.path.basename(path),
        'chars': chars,
        'headings': sum(levels.values()),
        'h1': levels[1], 'h2': levels[2], 'h3': levels[3], 'h4': levels[4],
        'max_depth': max_depth,
        'table_lines': len(re.findall(r'^\|.*\|\s*$', body, re.M)),
        'code_blocks': body.count('```') // 2,
        'list_items': len(re.findall(r'^\s*(?:[-*]|\d+\.) ', body, re.M)),
        'est_chunks': max(1, -(-chars // EST_CHUNK)),
    }


def main():
    os.makedirs(OUT_DIR, exist_ok=True)

    files = sorted(glob.glob(SRC + '/**/*.md', recursive=True))
    print('扫描 %d 份 .md 文档...\n' % len(files))

    kept, removed = [], []
    for f in files:
        a = analyze(f, SRC)
        if a['chars'] < MIN_CHARS:
            reason = '空文档' if a['chars'] < 50 else '过短(<%d字)' % MIN_CHARS
            removed.append({'rel_path': a['rel_path'], 'chars': a['chars'],
                            'reason': reason})
        else:
            kept.append(a)

    kept.sort(key=lambda r: -r['chars'])

    # ---------- 汇总 ----------
    tot_chars = sum(r['chars'] for r in kept)
    tot_chunks = sum(r['est_chunks'] for r in kept)
    depths = Counter(r['max_depth'] for r in kept)

    print('=' * 88)
    print('筛选结果')
    print('=' * 88)
    print('  扫描总数 : %d 份' % len(files))
    print('  剔除     : %d 份（%s）' % (
        len(removed), ', '.join('%s×%d' % (k, v) for k, v in
                                Counter(r['reason'] for r in removed).items())))
    print('  保留     : %d 份' % len(kept))
    print()
    print('  【语料体量】')
    print('    总字数      : %s 字' % format(tot_chars, ','))
    print('    预估 chunk 数: %s 个（按 %d 字/chunk 估算）' % (
        format(tot_chunks, ','), EST_CHUNK))
    print('    单篇中位字数: %d' % sorted(r['chars'] for r in kept)[len(kept) // 2])
    print()
    print('  【结构分布】')
    for d in range(1, 5):
        print('    最深 %d 级标题 : %d 份' % (d, depths.get(d, 0)))
    print()
    print('  【内容特征】')
    print('    含表格   : %d 份' % sum(1 for r in kept if r['table_lines'] > 0))
    print('    含代码块 : %d 份' % sum(1 for r in kept if r['code_blocks'] > 0))
    print('    含列表   : %d 份' % sum(1 for r in kept if r['list_items'] > 0))
    print()
    print('  【按目录】')
    g = {}
    for r in kept:
        g.setdefault(r['top_dir'], []).append(r)
    for k in sorted(g, key=lambda k: -sum(x['chars'] for x in g[k]))[:12]:
        v = g[k]
        print('    %-20s %3d 份  %8s 字' % (k, len(v), format(sum(x['chars'] for x in v), ',')))

    # ---------- 落盘 ----------
    manifest = {
        'source': SRC,
        'filter_rule': {'min_chars': MIN_CHARS},
        'scanned': len(files),
        'kept': len(kept),
        'removed_count': len(removed),
        'totals': {'chars': tot_chars, 'est_chunks': tot_chunks},
        'docs': kept,
        'removed': removed,
    }
    p_json = os.path.join(OUT_DIR, '_manifest.json')
    with open(p_json, 'w', encoding='utf-8') as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)

    p_md = os.path.join(OUT_DIR, '_report.md')
    with open(p_md, 'w', encoding='utf-8') as fh:
        W = fh.write
        W('# Markdown 语料基线报告\n\n')
        W('> 来源：`%s`\n\n' % SRC)
        W('## 筛选规则\n\n')
        W('- 剔除：空文档、少于 %d 字的文档\n' % MIN_CHARS)
        W('- 保留：其余全部（保持自然分布）\n\n')
        W('## 汇总\n\n')
        W('| 指标 | 值 |\n|---|---|\n')
        W('| 扫描总数 | %d 份 |\n' % len(files))
        W('| 保留 | **%d 份** |\n' % len(kept))
        W('| 剔除 | %d 份 |\n' % len(removed))
        W('| 总字数 | **%s** |\n' % format(tot_chars, ','))
        W('| 预估 chunk 数（@%d字） | **%s** |\n' % (EST_CHUNK, format(tot_chunks, ',')))
        W('\n## 语料清单（按字数降序）\n\n')
        W('| # | 标题 | 字数 | 标题数 | 层深 | 表格 | 代码 | 预估chunk | 路径 |\n')
        W('|---|---|---|---|---|---|---|---|---|\n')
        for i, r in enumerate(kept, 1):
            W('| %d | %s | %d | %d | %d | %d | %d | %d | %s |\n' % (
                i, r['title'], r['chars'], r['headings'], r['max_depth'],
                r['table_lines'], r['code_blocks'], r['est_chunks'], r['rel_path']))
        W('\n## 被剔除的文档（%d 份）\n\n' % len(removed))
        W('| 路径 | 字数 | 原因 |\n|---|---|---|\n')
        for r in removed:
            W('| %s | %d | %s |\n' % (r['rel_path'], r['chars'], r['reason']))

    print('\n' + '=' * 88)
    print('输出:')
    print('  清单: %s' % p_json)
    print('  报告: %s' % p_md)


if __name__ == '__main__':
    main()
