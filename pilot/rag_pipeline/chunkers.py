# -*- coding: utf-8 -*-
"""
三种分块策略的实现（Step 3a 的实验组件）

A. 固定长度    CharacterTextSplitter(separator="")        —— 最差基线
B. 递归字符    RecursiveCharacterTextSplitter              —— 业界默认
C. 结构感知    MarkdownHeaderTextSplitter + 二次切分 + 标题注入 —— 我们的方案

统一基线配置：
    chunk_size = 800, overlap = 100
    length_function = len（历史回归）；正式实验可传入明确的 token 计数器
    protect_code = True —— 代码块保护（占位符方案，代码实验另行处理）

所有策略统一输出 List[Chunk]。
"""
import re
from typing import Callable, List, Dict, Any, Optional, Tuple

from langchain_text_splitters import (
    CharacterTextSplitter,
    RecursiveCharacterTextSplitter,
    MarkdownHeaderTextSplitter,
)

# ---------------------------------------------------------------
# 默认参数
# ---------------------------------------------------------------
DEFAULT_CHUNK_SIZE = 800
DEFAULT_OVERLAP = 100
MARKDOWN_HEADERS = [('#', 'h1'), ('##', 'h2'), ('###', 'h3')]

RE_CODE_BLOCK = re.compile(r'```[\s\S]*?```')
LengthFunction = Callable[[str], int]


# ---------------------------------------------------------------
# 代码块保护（占位符方案）
# ---------------------------------------------------------------
def extract_code_blocks(text: str) -> Tuple[str, List[str]]:
    """把所有代码块抽出，原地留下短占位符；返回 (替换后文本, 代码块列表)"""
    blocks: List[str] = []

    def repl(m):
        blocks.append(m.group(0))
        return '\n\n@@CB_%d@@\n\n' % (len(blocks) - 1)

    return RE_CODE_BLOCK.sub(repl, text), blocks


def restore_code_blocks(text: str, blocks: List[str]) -> str:
    """把占位符换回代码块"""
    for i, b in enumerate(blocks):
        text = text.replace('@@CB_%d@@' % i, b)
    return text


class Chunk:
    """统一的分块结果"""

    __slots__ = (
        'content', 'metadata', 'doc_id', 'start', 'end', 'strategy',
        'mapping_status',
    )

    def __init__(self, content: str, metadata: Dict[str, Any], doc_id: str,
                 start: Optional[int] = None, end: Optional[int] = None,
                 strategy: str = '', mapping_status: str = 'unresolved'):
        self.content = content
        self.metadata = metadata
        self.doc_id = doc_id
        self.start = start
        self.end = end
        self.strategy = strategy
        self.mapping_status = mapping_status

    def __repr__(self):
        return 'Chunk(%s, %d字, %s)' % (
            self.doc_id[:20], len(self.content), list(self.metadata.values())[:2])


# ---------------------------------------------------------------
# 定位：把切出的文本片段映射回原文的字符区间
# ---------------------------------------------------------------
def _normalise_with_offsets(value: str) -> Tuple[str, List[int]]:
    """Collapse whitespace while retaining the source offset of each char."""
    chars: List[str] = []
    offsets: List[int] = []
    for index, char in enumerate(value):
        if char.isspace():
            if chars and chars[-1] == ' ':
                continue
            chars.append(' ')
        else:
            chars.append(char)
        offsets.append(index)
    return ''.join(chars), offsets


def _remove_whitespace_with_offsets(value: str) -> Tuple[str, List[int]]:
    """Remove whitespace for conservative mapping when a splitter drops it."""
    chars: List[str] = []
    offsets: List[int] = []
    for index, char in enumerate(value):
        if not char.isspace():
            chars.append(char)
            offsets.append(index)
    return ''.join(chars), offsets


def _find_normalised_span(
    raw_normalised: str,
    raw_offsets: List[int],
    piece: str,
    minimum_start: Optional[int],
) -> Optional[Tuple[int, int]]:
    """Find a piece after the previous piece's start, or return ``None``.

    Splitters trim and sometimes merge whitespace. Matching a whitespace-
    normalised string keeps the returned coordinates in the original source.
    No guessed interval is returned when the text cannot be located.
    """
    key, _ = _normalise_with_offsets(piece.strip())
    if not key:
        return (minimum_start, minimum_start)

    search_from = 0
    candidates: List[Tuple[int, int]] = []
    while True:
        match = raw_normalised.find(key, search_from)
        if match < 0:
            break
        end_index = match + len(key) - 1
        if end_index < len(raw_offsets):
            start = raw_offsets[match]
            end = raw_offsets[end_index] + 1
            if minimum_start is None or start >= minimum_start:
                candidates.append((start, end))
        search_from = match + 1

    if not candidates:
        return None
    # The splitter preserves document order.  For overlapping chunks the
    # next chunk starts after the previous start, not necessarily after its
    # end.  Selecting the first later occurrence handles repeated text while
    # preserving overlap; an identical start is only accepted when no later
    # occurrence exists.
    if minimum_start is None:
        return candidates[0]
    later = [span for span in candidates if span[0] > minimum_start]
    return (later or candidates)[0]


def _find_whitespace_ignored_span(
    raw: str,
    piece: str,
    minimum_start: Optional[int],
) -> Optional[Tuple[int, int]]:
    """Map a piece only when its non-whitespace characters match the source."""
    raw_compact, raw_offsets = _remove_whitespace_with_offsets(raw)
    key, _ = _remove_whitespace_with_offsets(piece.strip())
    if not key:
        return None

    search_from = 0
    candidates: List[Tuple[int, int]] = []
    while True:
        match = raw_compact.find(key, search_from)
        if match < 0:
            break
        end_index = match + len(key) - 1
        if end_index < len(raw_offsets):
            start = raw_offsets[match]
            end = raw_offsets[end_index] + 1
            if minimum_start is None or start >= minimum_start:
                candidates.append((start, end))
        search_from = match + 1

    if not candidates:
        return None
    if minimum_start is None:
        return candidates[0]
    later = [span for span in candidates if span[0] > minimum_start]
    return (later or candidates)[0]


def locate_spans_detailed(
    raw: str, pieces: List[str]
) -> List[Tuple[Optional[Tuple[int, int]], str]]:
    """Map splitter output to exact source intervals.

    First collapse whitespace runs. If that fails, retry after removing
    whitespace only; the non-whitespace characters must still match in order.
    Failed matches remain unresolved. No cursor-based coordinates are made.
    """
    raw_normalised, raw_offsets = _normalise_with_offsets(raw)
    spans: List[Tuple[Optional[Tuple[int, int]], str]] = []
    previous_start: Optional[int] = None
    for piece in pieces:
        span = _find_normalised_span(
            raw_normalised, raw_offsets, piece, previous_start
        )
        mapping_method = "collapsed_whitespace"
        if span is None:
            span = _find_whitespace_ignored_span(raw, piece, previous_start)
            mapping_method = "ignored_whitespace"
        spans.append((span, mapping_method))
        if span is not None and span[0] is not None and span[1] is not None and span[1] > span[0]:
            previous_start = span[0]
    return spans


def locate_spans(raw: str, pieces: List[str]) -> List[Optional[Tuple[int, int]]]:
    """Compatibility wrapper returning only source intervals."""
    return [span for span, _ in locate_spans_detailed(raw, pieces)]


# ---------------------------------------------------------------
# A. 固定长度
# ---------------------------------------------------------------
def split_fixed(text: str, doc_id: str, chunk_size: int = DEFAULT_CHUNK_SIZE,
                overlap: int = DEFAULT_OVERLAP, protect: bool = True,
                length_function: LengthFunction = len,
                **kw) -> List[Chunk]:
    """纯按字符数切（最差基线）。separator="" 表示不找语义边界，硬切。"""
    src, blocks = extract_code_blocks(text) if protect else (text, [])
    measured_length = (
        (lambda value: length_function(restore_code_blocks(value, blocks)))
        if blocks else length_function
    )
    sp = CharacterTextSplitter(separator='', chunk_size=chunk_size,
                               chunk_overlap=overlap,
                               length_function=measured_length)
    pieces = sp.split_text(src)
    if blocks:
        pieces = [restore_code_blocks(p, blocks) for p in pieces]
    spans = locate_spans(text, pieces)
    return [
        Chunk(
            p, {}, doc_id,
            *(span if span is not None else (None, None)),
            'A_fixed',
            'exact_source_span' if span is not None else 'unresolved',
        )
        for p, span in zip(pieces, spans)
    ]


# ---------------------------------------------------------------
# B. 递归字符
# ---------------------------------------------------------------
def split_recursive(text: str, doc_id: str, chunk_size: int = DEFAULT_CHUNK_SIZE,
                    overlap: int = DEFAULT_OVERLAP, protect: bool = True,
                    length_function: LengthFunction = len,
                    **kw) -> List[Chunk]:
    """按分隔符层级递归切（业界默认）。"""
    src, blocks = extract_code_blocks(text) if protect else (text, [])
    measured_length = (
        (lambda value: length_function(restore_code_blocks(value, blocks)))
        if blocks else length_function
    )
    sp = RecursiveCharacterTextSplitter(chunk_size=chunk_size,
                                        chunk_overlap=overlap,
                                        length_function=measured_length)
    pieces = sp.split_text(src)
    if blocks:
        pieces = [restore_code_blocks(p, blocks) for p in pieces]
    spans = locate_spans(text, pieces)
    return [
        Chunk(
            p, {}, doc_id,
            *(span if span is not None else (None, None)),
            'B_recursive',
            'exact_source_span' if span is not None else 'unresolved',
        )
        for p, span in zip(pieces, spans)
    ]


# ---------------------------------------------------------------
# C. 结构感知（两遍法 + 标题注入）
# ---------------------------------------------------------------
def split_structure_aware(text: str, doc_id: str,
                          chunk_size: int = DEFAULT_CHUNK_SIZE,
                          overlap: int = DEFAULT_OVERLAP,
                          headers=None,
                          inject: bool = True,
                          strip_headers: bool = True,
                          protect: bool = True,
                          length_function: LengthFunction = len,
                          **kw) -> List[Chunk]:
    """
    第一遍：按 Markdown 标题切，拿层级 metadata
    第二遍：对超大块用 RecursiveCharacterTextSplitter 二次切（小块不动，metadata 自动继承）
    后处理：还原代码块 → 把标题路径注入 content 开头（因为 metadata 不参与向量化）。
    注入前缀会从二次切分预算中扣除，避免最终 embedding 文本超过 chunk_size。
    """
    headers = headers or MARKDOWN_HEADERS
    src, blocks = extract_code_blocks(text) if protect else (text, [])
    measured_length = (
        (lambda value: length_function(restore_code_blocks(value, blocks)))
        if blocks else length_function
    )

    h_sp = MarkdownHeaderTextSplitter(headers_to_split_on=headers,
                                      strip_headers=strip_headers)
    docs = h_sp.split_text(src)

    out, raws = [], []
    for d in docs:
        meta = dict(d.metadata or {})
        path = ' > '.join(str(v) for v in meta.values() if v)
        prefix = ('"%s": ' % path) if inject and path else ''
        body_budget = chunk_size - length_function(prefix)
        if body_budget <= 0:
            raise ValueError(
                'Structure-aware heading metadata is longer than the requested '
                f'chunk_size ({chunk_size} in the selected length unit): {path!r}'
            )
        if overlap >= body_budget:
            raise ValueError(
                'overlap must be smaller than the body budget after heading '
                f'injection ({overlap} >= {body_budget}) for {path!r}'
            )
        s_sp = RecursiveCharacterTextSplitter(
            chunk_size=body_budget,
            chunk_overlap=overlap,
            length_function=measured_length,
        )
        sub_docs = s_sp.split_documents([d])
        for sub_doc in sub_docs:
            content = sub_doc.page_content
            # Keep the metadata from the splitter output, then inject the same
            # prefix that was budgeted above into the final embedding text.
            meta = dict(sub_doc.metadata or {})
            path = ' > '.join(str(v) for v in meta.values() if v)
            prefix = ('"%s": ' % path) if inject and path else ''
            if prefix:
                content = prefix + content
            raws.append(sub_doc.page_content)  # body only; prefix is synthetic
            out.append(Chunk(content, meta, doc_id, None, None, 'C_structure'))

    # Code-block placeholders are restored after splitting.  This keeps the
    # source mapping body-only and makes the measured final content include
    # the restored block and injected metadata.
    if blocks:
        for c, raw in zip(out, raws):
            restored = restore_code_blocks(raw, blocks)
            prefix = c.content[:-len(raw)] if raw and c.content.endswith(raw) else ''
            c.content = prefix + restored
        raws = [restore_code_blocks(raw, blocks) for raw in raws]

    spans = locate_spans_detailed(text, raws)
    for c, (span, mapping_method) in zip(out, spans):
        if span is None:
            c.start, c.end = None, None
            c.mapping_status = 'unresolved'
        else:
            c.start, c.end = span
            if inject and c.metadata:
                suffix = '_ignoring_whitespace' if mapping_method == 'ignored_whitespace' else ''
                c.mapping_status = f'exact_body{suffix}_excluding_injected_metadata'
            else:
                c.mapping_status = ('exact_source_ignoring_whitespace' if mapping_method == 'ignored_whitespace'
                                    else 'exact_source_span')
    return out


STRATEGIES = {
    'A_fixed': split_fixed,
    'B_recursive': split_recursive,
    'C_structure': split_structure_aware,
}


# ---------------------------------------------------------------
# 质量统计工具
# ---------------------------------------------------------------
def fence_balance(chunks: List[Chunk]) -> Dict[str, Any]:
    """统计代码块切断情况：chunk 里 ``` 数量为奇数 → 代码块被切断"""
    with_code, broken = 0, 0
    for c in chunks:
        n = c.content.count('```')
        if n:
            with_code += 1
            if n % 2 == 1:
                broken += 1
    return {'with_code': with_code, 'broken': broken,
            'broken_rate': round(broken / with_code, 3) if with_code else 0.0}


def size_stats(chunks: List[Chunk]) -> Dict[str, Any]:
    if not chunks:
        return {}
    sizes = sorted(len(c.content) for c in chunks)
    n = len(sizes)
    return {
        'n': n,
        'avg': int(sum(sizes) / n),
        'p50': sizes[n // 2],
        'p90': sizes[int(n * .9)],
        'max': sizes[-1],
        'lt100': sum(1 for s in sizes if s < 100),
        'gt2000': sum(1 for s in sizes if s > 2000),
        'lt100_rate': round(sum(1 for s in sizes if s < 100) / n, 3),
        'gt2000_rate': round(sum(1 for s in sizes if s > 2000) / n, 3),
    }
