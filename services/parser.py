"""
AST-based parsing service using Tree-sitter.
Provides unified structural extraction for multiple languages.
"""
import re
from typing import Any, Dict, List, Optional

try:
    import tree_sitter
    import tree_sitter_css
    import tree_sitter_go
    import tree_sitter_html
    import tree_sitter_javascript
    import tree_sitter_json
    import tree_sitter_markdown
    import tree_sitter_python
    import tree_sitter_rust
    import tree_sitter_typescript
    import tree_sitter_yaml
    HAS_TREE_SITTER = True

    PY_LANGUAGE = tree_sitter.Language(tree_sitter_python.language())
    JS_LANGUAGE = tree_sitter.Language(tree_sitter_javascript.language())
    TS_LANGUAGE = tree_sitter.Language(tree_sitter_typescript.language_typescript())
    TSX_LANGUAGE = tree_sitter.Language(tree_sitter_typescript.language_tsx())
    HTML_LANGUAGE = tree_sitter.Language(tree_sitter_html.language())
    MD_LANGUAGE = tree_sitter.Language(tree_sitter_markdown.language())
    CSS_LANGUAGE = tree_sitter.Language(tree_sitter_css.language())
    GO_LANGUAGE = tree_sitter.Language(tree_sitter_go.language())
    RUST_LANGUAGE = tree_sitter.Language(tree_sitter_rust.language())
    JSON_LANGUAGE = tree_sitter.Language(tree_sitter_json.language())
    YAML_LANGUAGE = tree_sitter.Language(tree_sitter_yaml.language())

    LANGUAGES = {
        '.py': PY_LANGUAGE,
        '.js': JS_LANGUAGE,
        '.jsx': JS_LANGUAGE,
        '.ts': TS_LANGUAGE,
        '.tsx': TSX_LANGUAGE,
        '.html': HTML_LANGUAGE,
        '.htm': HTML_LANGUAGE,
        '.md': MD_LANGUAGE,
        '.css': CSS_LANGUAGE,
        '.go': GO_LANGUAGE,
        '.rs': RUST_LANGUAGE,
        '.json': JSON_LANGUAGE,
        '.yaml': YAML_LANGUAGE,
        '.yml': YAML_LANGUAGE,
    }
except ImportError:
    HAS_TREE_SITTER = False
    LANGUAGES = {}

# Bump this any time extract_sections_ast / _walk_* logic changes so that
# file_memory records extracted with an older version are force-refreshed.
PARSER_VERSION = "3"  # 3: markdown headings span their section body

def get_parser(ext: str) -> Optional['tree_sitter.Parser']:
    if not HAS_TREE_SITTER or ext not in LANGUAGES:
        return None
    parser = tree_sitter.Parser(LANGUAGES[ext])
    return parser

def extract_sections_ast(content: str, ext: str) -> Optional[List[Dict[str, Any]]]:
    """
    Extract structural sections using Tree-sitter AST.
    Returns None if language is not supported or parsing fails.
    """
    parser = get_parser(ext)
    if not parser:
        return None

    try:
        tree = parser.parse(content.encode('utf-8'))
        lines = content.split('\n')
        sections = []

        if ext == '.py':
            _walk_python(tree.root_node, lines, sections)
        elif ext in ('.js', '.jsx', '.ts', '.tsx'):
            _walk_js_ts(tree.root_node, lines, sections)
        elif ext in ('.html', '.htm'):
            _walk_html(tree.root_node, lines, sections)
        elif ext == '.md':
            _walk_markdown(tree.root_node, lines, sections)
            _extend_markdown_headings(sections, len(lines))
        elif ext == '.css':
            _walk_css(tree.root_node, lines, sections)
        elif ext == '.go':
            _walk_go(tree.root_node, lines, sections)
        elif ext == '.rs':
            _walk_rust(tree.root_node, lines, sections)
        elif ext == '.json':
            _walk_json(tree.root_node, lines, sections)
        elif ext in ('.yaml', '.yml'):
            _walk_yaml(tree.root_node, lines, sections)
        else:
            return None

        return sections
    except Exception:
        return None

def _extract_docstring_python(node, lines: List[str]) -> Optional[str]:
    # In Python, docstring is the first expression statement in a block
    if node.type in ('function_definition', 'class_definition'):
        body = next((child for child in node.children if child.type == 'block'), None)
        if body and body.children and body.children[0].type == 'expression_statement':
            expr = body.children[0]
            if expr.children and expr.children[0].type == 'string':
                doc = lines[expr.start_point[0]].strip()
                if doc.startswith('"""') or doc.startswith("'''"):
                    quote = doc[:3]
                    if doc.endswith(quote) and len(doc) > 6:
                        return doc[3:-3].strip()
                    first = doc[3:].strip()
                    if first: return first
                    if expr.start_point[0] + 1 <= expr.end_point[0]:
                        return lines[expr.start_point[0] + 1].strip()
    return None

def _walk_python(node, lines: List[str], sections: List[Dict[str, Any]], parent_section=None):
    for child in node.children:
        if child.type == 'class_definition':
            name_node = next((c for c in child.children if c.type == 'identifier'), None)
            name = lines[name_node.start_point[0]][name_node.start_point[1]:name_node.end_point[1]] if name_node else 'Unknown'

            sig_start = child.start_point[0]
            # Find the colon
            colon_node = next((c for c in child.children if c.type == ':'), None)
            sig_end = colon_node.end_point[0] if colon_node else sig_start
            signature = '\n'.join(lines[sig_start:sig_end+1]).strip()

            section = {
                "type": "class",
                "name": name,
                "line_start": child.start_point[0] + 1,
                "line_end": child.end_point[0] + 1,
                "signature": signature,
                "children": []
            }
            doc = _extract_docstring_python(child, lines)
            if doc: section["doc"] = doc

            if parent_section:
                parent_section["children"].append(section)
            else:
                sections.append(section)

            body = next((c for c in child.children if c.type == 'block'), None)
            if body:
                _walk_python(body, lines, sections, section)

        elif child.type == 'function_definition':
            name_node = next((c for c in child.children if c.type == 'identifier'), None)
            name = lines[name_node.start_point[0]][name_node.start_point[1]:name_node.end_point[1]] if name_node else 'Unknown'

            sig_start = child.start_point[0]
            colon_node = next((c for c in child.children if c.type == ':'), None)
            sig_end = colon_node.end_point[0] if colon_node else sig_start
            signature = '\n'.join(lines[sig_start:sig_end+1]).strip()

            is_async = any(c.type == 'async' for c in child.children)

            section = {
                "type": "method" if parent_section and parent_section["type"] == "class" else "function",
                "name": name,
                "line_start": child.start_point[0] + 1,
                "line_end": child.end_point[0] + 1,
                "signature": signature
            }
            if is_async: section["async"] = True

            doc = _extract_docstring_python(child, lines)
            if doc: section["doc"] = doc

            if parent_section:
                parent_section["children"].append(section)
            else:
                sections.append(section)

        elif child.type == 'expression_statement':
            # Check for global constants (CAPITAL_NAME = value)
            if not parent_section:
                assign = next((c for c in child.children if c.type == 'assignment'), None)
                if assign:
                    target = next((c for c in assign.children if c.type == 'identifier'), None)
                    if target:
                        name = lines[target.start_point[0]][target.start_point[1]:target.end_point[1]]
                        if name.isupper():
                            sections.append({
                                "type": "constant",
                                "name": name,
                                "line_start": child.start_point[0] + 1,
                                "line_end": child.end_point[0] + 1,
                                "signature": lines[child.start_point[0]].strip()
                            })

        elif child.type == 'comment':
            text = lines[child.start_point[0]][child.start_point[1]:child.end_point[1]]
            if 'TODO' in text or 'FIXME' in text:
                sections.append({
                    "type": "comment",
                    "name": text.lstrip('#').strip(),
                    "line_start": child.start_point[0] + 1,
                    "line_end": child.end_point[0] + 1,
                    "signature": text.strip()
                })

        elif child.type in ('import_statement', 'import_from_statement'):
            section = {
                "type": "import",
                "name": lines[child.start_point[0]].strip(),
                "line_start": child.start_point[0] + 1,
                "line_end": child.end_point[0] + 1,
                "signature": lines[child.start_point[0]].strip()
            }
            if not parent_section:
                sections.append(section)

        elif child.type == 'decorated_definition':
            _walk_python(child, lines, sections, parent_section)

def _extract_docstring_js(node, lines: List[str]) -> Optional[str]:
    # Look for previous sibling that is a comment
    prev = node.prev_sibling
    while prev and prev.type == 'comment':
        comment = lines[prev.start_point[0]].strip()
        if comment.startswith('/**'):
            # simple single line extraction
            cleaned = comment.lstrip('/*').rstrip('*/').strip()
            if cleaned and cleaned != '*': return cleaned
            if prev.start_point[0] + 1 <= prev.end_point[0]:
                cleaned = lines[prev.start_point[0] + 1].strip().lstrip('*').strip()
                if cleaned: return cleaned
        prev = prev.prev_sibling
    return None

class _SyntheticParent:
    """Lightweight stand-in node whose only role is to expose a single child.

    Used so an exported declaration node can be re-fed through _walk_js_ts's
    child-iteration loop and recorded as a section (rather than descended into).
    """
    __slots__ = ("children",)

    def __init__(self, child):
        self.children = [child]


def _walk_js_ts(node, lines: List[str], sections: List[Dict[str, Any]], parent_section=None):
    for child in node.children:
        if child.type in ('class_declaration', 'abstract_class_declaration', 'interface_declaration', 'type_alias_declaration', 'enum_declaration'):
            name_node = next((c for c in child.children if c.type in ('identifier', 'type_identifier')), None)
            name = lines[name_node.start_point[0]][name_node.start_point[1]:name_node.end_point[1]] if name_node else 'Unknown'

            # Simple signature heuristic: first line
            signature = lines[child.start_point[0]].strip()

            t = child.type.split('_')[0]
            section = {
                "type": "class" if t == "class" else t,
                "name": name,
                "line_start": child.start_point[0] + 1,
                "line_end": child.end_point[0] + 1,
                "signature": signature,
                "children": []
            }
            doc = _extract_docstring_js(child, lines)
            if doc: section["doc"] = doc

            if parent_section:
                parent_section["children"].append(section)
            else:
                sections.append(section)

            body = next((c for c in child.children if c.type in ('class_body', 'interface_body', 'enum_body', 'object_type')), None)
            if body:
                _walk_js_ts(body, lines, sections, section)

        elif child.type in ('function_declaration', 'method_definition', 'public_field_definition', 'property_definition'):
            name_node = next((c for c in child.children if c.type in ('property_identifier', 'identifier', 'private_property_identifier')), None)
            name = lines[name_node.start_point[0]][name_node.start_point[1]:name_node.end_point[1]] if name_node else 'Unknown'

            signature = lines[child.start_point[0]].strip()

            is_async = any(c.type == 'async' for c in child.children)

            # TS access modifiers can be direct children or inside a 'accessibility_modifier' node
            access = None
            for c in child.children:
                if c.type in ('public', 'private', 'protected'):
                    access = c.type
                    break
                if c.type == 'accessibility_modifier':
                    access = lines[c.start_point[0]][c.start_point[1]:c.end_point[1]]
                    break

            stype = "method" if child.type == 'method_definition' else "function"
            if child.type in ('public_field_definition', 'property_definition'):
                stype = "property"

            section = {
                "type": stype,
                "name": name,
                "line_start": child.start_point[0] + 1,
                "line_end": child.end_point[0] + 1,
                "signature": signature
            }
            if is_async: section["async"] = True
            if access: section["access"] = access

            doc = _extract_docstring_js(child, lines)
            if doc: section["doc"] = doc

            if parent_section:
                parent_section["children"].append(section)
            else:
                sections.append(section)

        elif child.type == 'lexical_declaration' or child.type == 'variable_declaration':
            # Check for constants/variables
            decl = next((c for c in child.children if c.type == 'variable_declarator'), None)
            if decl:
                name_node = next((c for c in decl.children if c.type == 'identifier'), None)
                value_node = next((c for c in decl.children if c.type == 'arrow_function'), None)

                if name_node and value_node:
                    # Arrow function
                    name = lines[name_node.start_point[0]][name_node.start_point[1]:name_node.end_point[1]]
                    is_async = any(c.type == 'async' for c in value_node.children)
                    section = {
                        "type": "function",
                        "name": name,
                        "line_start": child.start_point[0] + 1,
                        "line_end": child.end_point[0] + 1,
                        "signature": lines[child.start_point[0]].strip()
                    }
                    if is_async: section["async"] = True
                    doc = _extract_docstring_js(child, lines)
                    if doc: section["doc"] = doc

                    if parent_section:
                        parent_section["children"].append(section)
                    else:
                        sections.append(section)
                elif name_node and not parent_section:
                    # Global constant/variable
                    name = lines[name_node.start_point[0]][name_node.start_point[1]:name_node.end_point[1]]
                    sections.append({
                        "type": "constant" if "const" in lines[child.start_point[0]] else "variable",
                        "name": name,
                        "line_start": child.start_point[0] + 1,
                        "line_end": child.end_point[0] + 1,
                        "signature": lines[child.start_point[0]].strip()
                    })

        elif child.type == 'comment':
            text = lines[child.start_point[0]][child.start_point[1]:child.end_point[1]]
            if 'TODO' in text or 'FIXME' in text:
                sections.append({
                    "type": "comment",
                    "name": text.lstrip('/ ').strip(),
                    "line_start": child.start_point[0] + 1,
                    "line_end": child.end_point[0] + 1,
                    "signature": text.strip()
                })

        elif child.type == 'import_statement':
            section = {
                "type": "import",
                "name": lines[child.start_point[0]].strip(),
                "line_start": child.start_point[0] + 1,
                "line_end": child.end_point[0] + 1,
                "signature": lines[child.start_point[0]].strip()
            }
            if not parent_section:
                sections.append(section)

        elif child.type == 'export_statement':
            # Process the exported declaration node itself (not its children).
            # Recursing into decl.children would descend INTO the declaration and
            # skip recording the exported class/function/const as a section.
            decl = next((c for c in child.children if c.type != 'export' and c.type != 'default'), None)
            if decl:
                _walk_js_ts(_SyntheticParent(decl), lines, sections, parent_section)

def _walk_html(node, lines: List[str], sections: List[Dict[str, Any]]):
    """Extract headings and elements with IDs from HTML AST."""
    for child in node.children:
        if child.type == 'element':
            start_tag = next((c for c in child.children if c.type == 'start_tag'), None)
            if start_tag:
                tag_name_node = next((c for c in start_tag.children if c.type == 'tag_name'), None)
                tag_name = lines[tag_name_node.start_point[0]][tag_name_node.start_point[1]:tag_name_node.end_point[1]].lower() if tag_name_node else ''

                # Check for headings (h1-h6)
                if tag_name in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6'):
                    # Get text content of heading
                    text_content = ""
                    for subchild in child.children:
                        if subchild.type == 'text':
                            text_content += lines[subchild.start_point[0]][subchild.start_point[1]:subchild.end_point[1]]

                    sections.append({
                        "type": "heading",
                        "name": f"{tag_name}: {text_content.strip()[:60]}",
                        "line_start": child.start_point[0] + 1,
                        "line_end": child.end_point[0] + 1,
                        "signature": lines[child.start_point[0]].strip()
                    })

                # Check for ID attribute
                else:
                    id_attr = None
                    for subchild in start_tag.children:
                        if subchild.type == 'attribute':
                            attr_name_node = next((c for c in subchild.children if c.type == 'attribute_name'), None)
                            if attr_name_node:
                                attr_name = lines[attr_name_node.start_point[0]][attr_name_node.start_point[1]:attr_name_node.end_point[1]]
                                if attr_name == 'id':
                                    val_node = next((c for c in subchild.children if c.type == 'attribute_value'), None)
                                    if val_node:
                                        id_attr = lines[val_node.start_point[0]][val_node.start_point[1]:val_node.end_point[1]].strip('"\'')
                                        break

                    if id_attr:
                        sections.append({
                            "type": "section",
                            "name": f"#{id_attr} ({tag_name})",
                            "line_start": child.start_point[0] + 1,
                            "line_end": child.end_point[0] + 1,
                            "signature": lines[child.start_point[0]].strip()
                        })

            # Recurse into element children
            _walk_html(child, lines, sections)
        else:
            # Recurse into other nodes (like document)
            _walk_html(child, lines, sections)

def _walk_markdown(node, lines: List[str], sections: List[Dict[str, Any]]):
    """Extract headings from Markdown AST."""
    for child in node.children:
        if child.type == 'atx_heading' or child.type == 'setext_heading':
            heading_node = next((c for c in child.children if c.type == 'atx_h1_marker' or c.type == 'atx_h2_marker' or c.type == 'atx_h3_marker' or c.type == 'atx_h4_marker' or c.type == 'atx_h5_marker' or c.type == 'atx_h6_marker'), None)
            level = "h1"
            if heading_node:
                marker = lines[heading_node.start_point[0]][heading_node.start_point[1]:heading_node.end_point[1]]
                level = f"h{len(marker.strip())}"

            # Extract content text
            content_text = ""
            for subchild in child.children:
                if subchild.type in ('inline', 'text'):
                    content_text += lines[subchild.start_point[0]][subchild.start_point[1]:subchild.end_point[1]]

            sections.append({
                "type": "heading",
                "name": f"{level}: {content_text.strip()[:60]}",
                "line_start": child.start_point[0] + 1,
                "line_end": child.end_point[0] + 1,
                "signature": lines[child.start_point[0]].strip()
            })

        # Recurse if needed (atx headings are top level usually, but just in case)
        _walk_markdown(child, lines, sections)


def _extend_markdown_headings(sections: List[Dict[str, Any]], total_lines: int) -> None:
    """Give each heading the body it introduces.

    ``_walk_markdown`` records a heading as its own single line, so a CHANGELOG
    entry became an 18-token chunk holding nothing but its title — short,
    name-boosted, and outranking the source files it described. A heading now
    runs to the line before the next heading of the same or a higher level
    (else end of file), which is also what a reader means by "that section".
    """
    heads = [s for s in sections if s.get("type") == "heading"]

    def _level(sec) -> int:
        name = sec.get("name") or ""
        return int(name[1]) if len(name) > 1 and name[0] == "h" and name[1].isdigit() else 1

    for i, head in enumerate(heads):
        lv = _level(head)
        end = total_lines
        for nxt in heads[i + 1:]:
            if _level(nxt) <= lv:
                end = max(head["line_start"], nxt["line_start"] - 1)
                break
        head["line_end"] = max(int(head.get("line_end") or 0), end)

def _walk_css(node, lines: List[str], sections: List[Dict[str, Any]]):
    """Extract rulesets from CSS AST."""
    for child in node.children:
        if child.type == 'rule_set':
            selector_node = next((c for c in child.children if c.type == 'selectors'), None)
            selector = lines[selector_node.start_point[0]][selector_node.start_point[1]:selector_node.end_point[1]].strip() if selector_node else "Unknown"

            sections.append({
                "type": "section",
                "name": selector[:60],
                "line_start": child.start_point[0] + 1,
                "line_end": child.end_point[0] + 1,
                "signature": selector
            })
        elif child.type == 'media_statement':
            query_node = next((c for c in child.children if c.type == 'media_query'), None)
            query = lines[query_node.start_point[0]][query_node.start_point[1]:query_node.end_point[1]].strip() if query_node else "@media"

            section = {
                "type": "section",
                "name": f"@media {query}",
                "line_start": child.start_point[0] + 1,
                "line_end": child.end_point[0] + 1,
                "signature": query,
                "children": []
            }
            sections.append(section)
            # Find block
            block = next((c for c in child.children if c.type == 'block'), None)
            if block:
                # We reuse walk_css but redirect results to children if we wanted nested
                # For simplicity, we'll just keep them flat but maybe prefixed
                pass

        # Recurse
        _walk_css(child, lines, sections)

def _walk_go(node, lines: List[str], sections: List[Dict[str, Any]]):
    """Extract functions, types, and methods from Go AST."""
    for child in node.children:
        if child.type in ('function_declaration', 'method_declaration'):
            name_node = next((c for c in child.children if c.type == 'identifier' or c.type == 'field_identifier'), None)
            name = lines[name_node.start_point[0]][name_node.start_point[1]:name_node.end_point[1]] if name_node else 'Unknown'

            signature = lines[child.start_point[0]].strip()

            sections.append({
                "type": "function" if child.type == 'function_declaration' else "method",
                "name": name,
                "line_start": child.start_point[0] + 1,
                "line_end": child.end_point[0] + 1,
                "signature": signature
            })
        elif child.type == 'type_declaration':
            # Drill into type specs
            for spec in child.children:
                if spec.type == 'type_spec':
                    name_node = next((c for c in spec.children if c.type == 'type_identifier'), None)
                    name = lines[name_node.start_point[0]][name_node.start_point[1]:name_node.end_point[1]] if name_node else 'Unknown'
                    sections.append({
                        "type": "type",
                        "name": name,
                        "line_start": child.start_point[0] + 1,
                        "line_end": child.end_point[0] + 1,
                        "signature": lines[child.start_point[0]].strip()
                    })
        _walk_go(child, lines, sections)

def _walk_rust(node, lines: List[str], sections: List[Dict[str, Any]]):
    """Extract functions, structs, enums, and impls from Rust AST."""
    for child in node.children:
        if child.type in ('function_item', 'struct_item', 'enum_item', 'trait_item', 'impl_item'):
            name_node = next((c for c in child.children if c.type in ('identifier', 'type_identifier')), None)
            name = lines[name_node.start_point[0]][name_node.start_point[1]:name_node.end_point[1]] if name_node else 'Unknown'

            if child.type == 'impl_item':
                # For impl, the name is the type being implemented
                type_node = next((c for c in child.children if c.type == 'type_identifier'), None)
                if type_node:
                    name = f"impl {lines[type_node.start_point[0]][type_node.start_point[1]:type_node.end_point[1]]}"

            stype = child.type.replace('_item', '')
            sections.append({
                "type": stype,
                "name": name,
                "line_start": child.start_point[0] + 1,
                "line_end": child.end_point[0] + 1,
                "signature": lines[child.start_point[0]].strip()
            })
        _walk_rust(child, lines, sections)

def _walk_json(node, lines: List[str], sections: List[Dict[str, Any]]):
    """Extract top-level keys from JSON AST."""
    for child in node.children:
        if child.type == 'object':
            for pair in child.children:
                if pair.type == 'pair':
                    key_node = next((c for c in pair.children if c.type == 'string'), None)
                    if key_node:
                        key = lines[key_node.start_point[0]][key_node.start_point[1]:key_node.end_point[1]].strip('"\'')
                        sections.append({
                            "type": "property",
                            "name": key,
                            "line_start": pair.start_point[0] + 1,
                            "line_end": pair.end_point[0] + 1,
                            "signature": key
                        })
        # Typically only top level for mapping
        break

def _walk_yaml(node, lines: List[str], sections: List[Dict[str, Any]]):
    """Extract top-level keys from YAML AST."""
    for doc in node.children:
        if doc.type == 'document':
            block = next((c for c in doc.children if c.type == 'block_node'), None)
            if block:
                mapping = next((c for c in block.children if c.type == 'block_mapping'), None)
                if mapping:
                    for pair in mapping.children:
                        if pair.type == 'block_mapping_pair':
                            key_node = next((c for c in pair.children if c.type == 'flow_node' or c.type == 'block_node'), None)
                            if key_node:
                                key = lines[key_node.start_point[0]][key_node.start_point[1]:key_node.end_point[1]].strip()
                                sections.append({
                                    "type": "property",
                                    "name": key,
                                    "line_start": pair.start_point[0] + 1,
                                    "line_end": pair.end_point[0] + 1,
                                    "signature": key
                                })
        # Typically only top level for mapping
        break

def check_syntax_ast(content: str, ext: str) -> List[Dict[str, Any]]:
    """
    Detect syntax errors using Tree-sitter.
    Returns a list of errors with line numbers and descriptions.
    """
    parser = get_parser(ext)
    if not parser:
        return []

    try:
        tree = parser.parse(content.encode("utf-8"))
        errors = []

        def _find_errors(node):
            if node.type == "ERROR" or node.is_error:
                errors.append({
                    "line": node.start_point[0] + 1,
                    "column": node.start_point[1],
                    "type": "syntax_error",
                    "text": f"Syntax error at line {node.start_point[0] + 1}, column {node.start_point[1]}"
                })

            # node.is_missing is available in some versions
            try:
                if hasattr(node, "is_missing") and node.is_missing:
                    errors.append({
                        "line": node.start_point[0] + 1,
                        "column": node.start_point[1],
                        "type": "missing_token",
                        "text": f"Missing expected token at line {node.start_point[0] + 1}"
                    })
            except Exception: pass

            for child in node.children:
                _find_errors(child)

        _find_errors(tree.root_node)
        return errors
    except Exception as e:
        return [{"line": 1, "column": 0, "type": "parser_error", "text": str(e)}]


# ── Native syntax checking (no tree-sitter, no AI) ──────────────────
import os as _os
import shutil as _shutil
import subprocess as _subprocess
import sys as _sys
import tempfile as _tempfile
import threading as _threading


def _kill_proc_tree(proc: _subprocess.Popen) -> None:
    """Kill a process and all its children (Windows-safe)."""
    try:
        if _sys.platform == "win32":
            # taskkill /T kills the entire process tree — critical on Windows
            # where Rscript spawns child R processes that outlive the parent.
            _subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True, timeout=5,
                creationflags=_subprocess.CREATE_NO_WINDOW,
            )
        else:
            proc.kill()
        proc.wait(timeout=3)
    except Exception:
        pass


def _err(line: int = 1, col: int = 0, text: str = "") -> Dict[str, Any]:
    return {"line": max(1, int(line)), "column": max(0, int(col)), "text": str(text)}


def _clean_text(text: str) -> str:
    return str(text or "").replace("\x00", "").strip()


def _checker_name(ext: str) -> str:
    return {
        '.py': 'python_ast',
        '.json': 'json',
        '.yaml': 'pyyaml',
        '.yml': 'pyyaml',
        '.xml': 'elementtree',
        '.svg': 'elementtree',
        '.toml': 'tomllib',
        '.html': 'lxml',
        '.htm': 'lxml',
        '.css': 'tinycss2',
        '.js': 'node --check',
        '.jsx': 'tsc',
        '.ts': 'tsc',
        '.tsx': 'tsc',
        '.java': 'javac',
        '.go': 'gofmt',
        '.rs': 'rustc',
        '.r': 'Rscript',
        '.php': 'php -l',
        '.rb': 'ruby -c',
        '.pl': 'perl -c',
        '.pm': 'perl -c',
        '.lua': 'luac -p',
        '.sh': 'bash -n',
        '.bash': 'bash -n',
    }.get((ext or "").lower(), (ext or "unknown").lstrip(".") or "unknown")


def _result(status: str, checker: str, errors: List[Dict[str, Any]] | None = None, detail: str = "") -> Dict[str, Any]:
    return {
        "status": status,
        "checker": checker,
        "errors": errors or [],
        "detail": _clean_text(detail),
    }


def _subproc_check(cmd: List[str], content: str, suffix: str, checker: str, timeout: int = 8) -> Dict[str, Any]:
    """Write content to a temp file and run a checker subprocess."""
    # Resolve the checker through PATH before Popen: npm-global tools on
    # Windows are .cmd batch shims (tsc.cmd, eslint.cmd) that CreateProcess
    # will not find by bare name — shutil.which returns the full shim path,
    # which Popen can execute. Bare-name Popen made every npm checker report
    # "not installed" on Windows even when it was on PATH.
    exe = _shutil.which(cmd[0])
    if exe is None:
        return _result("checker_unavailable", checker,
                       detail=f"{cmd[0]} is not installed or not on PATH.")
    fd, tmp = _tempfile.mkstemp(suffix=suffix)
    try:
        with _os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(content)

        kwargs = {}
        if _sys.platform == "win32":
            kwargs["creationflags"] = _subprocess.CREATE_NO_WINDOW

        # Popen + _kill_proc_tree on timeout: subprocess.run's timeout only kills
        # the direct child, leaving any grandchild processes (e.g. Rscript -> R)
        # running. encoding/errors avoid UnicodeDecodeError on non-UTF-8 output.
        proc = _subprocess.Popen(
            [exe] + cmd[1:] + [tmp], stdin=_subprocess.DEVNULL,
            stdout=_subprocess.PIPE, stderr=_subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", **kwargs,
        )
        try:
            out, err = proc.communicate(timeout=timeout)
        except _subprocess.TimeoutExpired:
            _kill_proc_tree(proc)
            return _result("checker_timeout", checker, detail=f"{cmd[0]} exceeded {timeout}s.")
        return {
            "status": "ok",
            "checker": checker,
            "returncode": proc.returncode,
            "stdout": (out or "")[:65536],
            "stderr": (err or "")[:65536],
        }
    except FileNotFoundError:
        return _result("checker_unavailable", checker, detail=f"{cmd[0]} is not installed or not on PATH.")
    except PermissionError as e:
        return _result("checker_failed", checker, detail=str(e) or f"{cmd[0]} access denied.")
    except OSError as e:
        return _result("checker_failed", checker, detail=str(e) or f"{cmd[0]} failed to start.")
    except _subprocess.TimeoutExpired:
        return _result("checker_timeout", checker, detail=f"{cmd[0]} exceeded {timeout}s.")
    finally:
        try:
            _os.unlink(tmp)
        except OSError:
            pass


def _native_python(content: str) -> Dict[str, Any]:
    import ast
    try:
        ast.parse(content)
        return _result("clean", "python_ast")
    except SyntaxError as e:
        return _result("syntax_error", "python_ast", [_err(e.lineno or 1, (e.offset or 1) - 1, e.msg)])


def _native_json(content: str) -> Dict[str, Any]:
    import json
    try:
        json.loads(content)
        return _result("clean", "json")
    except json.JSONDecodeError as e:
        return _result("syntax_error", "json", [_err(e.lineno, e.colno, e.msg)], detail=e.msg)


def _native_yaml(content: str) -> Dict[str, Any]:
    try:
        import yaml
        # Consume all documents to catch errors in multi-document YAML files.
        list(yaml.safe_load_all(content))
        return _result("clean", "pyyaml")
    except ImportError:
        return _result("checker_unavailable", "pyyaml", detail="PyYAML is not installed.")
    except Exception as e:
        mark = getattr(e, 'problem_mark', None)
        line = (mark.line + 1) if mark else 1
        col = mark.column if mark else 0
        return _result("syntax_error", "pyyaml", [_err(line, col, str(e))], detail=str(e))


def _native_xml(content: str) -> Dict[str, Any]:
    import xml.etree.ElementTree as ET
    try:
        ET.fromstring(content)
        return _result("clean", "elementtree")
    except ET.ParseError as e:
        pos = getattr(e, 'position', None)
        line, col = pos if pos else (1, 0)
        return _result("syntax_error", "elementtree", [_err(line, col, str(e))], detail=str(e))


def _native_toml(content: str) -> Dict[str, Any]:
    try:
        try:
            import tomllib  # Python 3.11+
        except ImportError:
            import tomli as tomllib  # type: ignore
        tomllib.loads(content)
        return _result("clean", "tomllib")
    except ImportError:
        return _result("checker_unavailable", "tomllib", detail="tomllib/tomli is not installed.")
    except Exception as e:
        m = re.search(r'line (\d+)', str(e))
        return _result("syntax_error", "tomllib", [_err(int(m.group(1)) if m else 1, 0, str(e))], detail=str(e))


def _native_html(content: str) -> Dict[str, Any]:
    # lxml false-positive filters for valid HTML5 constructs:
    # - HTML_UNKNOWN_TAG: HTML5 semantic elements (nav, main, header, footer, etc.)
    # - ERR_TAG_NAME_MISMATCH "script embeds close tag": JS code containing </script>
    # - ERR_NAME_REQUIRED "htmlParseEntityRef: no name": unescaped & in code/text
    _SKIP_TYPES = {"HTML_UNKNOWN_TAG", "ERR_NAME_REQUIRED"}
    _SKIP_MSG_FRAGMENTS = {"script embeds close tag"}

    def _is_false_positive(e) -> bool:
        if e.type_name in _SKIP_TYPES:
            return True
        msg = e.message.lower()
        return any(frag in msg for frag in _SKIP_MSG_FRAGMENTS)

    try:
        from lxml import etree  # type: ignore
        parser = etree.HTMLParser(recover=True)
        etree.fromstring(content.encode('utf-8', errors='replace'), parser)
        real_errors = [e for e in parser.error_log if not _is_false_positive(e)]
        errors = [_err(e.line, e.column, e.message) for e in real_errors]
        if errors:
            return _result("syntax_error", "lxml", errors, detail=errors[0]["text"])
        return _result("clean", "lxml")
    except ImportError:
        return _result("checker_unavailable", "lxml", detail="lxml is not installed.")
    except Exception as e:
        return _result("checker_failed", "lxml", detail=str(e))


def _native_css(content: str) -> Dict[str, Any]:
    try:
        import tinycss2  # type: ignore
        rules = tinycss2.parse_stylesheet(content)
        errors = [
            _err(getattr(r, 'source_line', 1), 0, repr(r))
            for r in rules if getattr(r, 'type', '') == 'error'
        ]
        if errors:
            return _result("syntax_error", "tinycss2", errors, detail=errors[0]["text"])
        return _result("clean", "tinycss2")
    except ImportError:
        return _result("checker_unavailable", "tinycss2", detail="tinycss2 is not installed.")
    except Exception as e:
        return _result("checker_failed", "tinycss2", detail=str(e))


# JSX intent markers: a closing tag, a self-closing tag, or an element
# opened directly in a return/arrow position. C3's own UI (cli/ui,
# cli/hub_ui) deliberately ships JSX in .js files served through Babel, so
# a plain `node --check` verdict on such a file is a claim about the wrong
# grammar — not evidence of a defect.
_JSX_MARKER = re.compile(
    r"</[A-Za-z]|/>|return\s*\(\s*<|=>\s*\(?\s*<|<[A-Z][\w.]*[\s>/]")


def _native_js(content: str) -> Dict[str, Any]:
    proc = _subproc_check(["node", "--check"], content, ".js", "node --check")
    if proc["status"] != "ok":
        return proc
    if proc["returncode"] == 0:
        return _result("clean", "node --check")
    errors = []
    lines = _clean_text(proc["stderr"] + "\n" + proc["stdout"]).splitlines()
    for i, line in enumerate(lines):
        m = re.match(r'^.*:(\d+)$', line.strip())
        if m and i + 1 < len(lines):
            errors.append(_err(int(m.group(1)), 0, lines[i + 1].strip()))
    if errors:
        if _JSX_MARKER.search(content):
            # Re-judge under the grammar the file is actually written in.
            jsx = _native_jsx(content)
            if jsx["status"] == "clean":
                jsx["detail"] = _clean_text(
                    "JSX in a .js file (C3's UI idiom): node --check cannot "
                    "parse it; validated with the tsc JSX checker instead. "
                    + jsx.get("detail", ""))
                return jsx
            if jsx["status"] == "syntax_error":
                return jsx  # real defects, with tsc's better positions
            return _result(
                "unsupported", "node --check + tsc",
                detail="File contains JSX; node --check cannot parse it and "
                       "the tsc JSX checker was unavailable to validate it.")
        return _result("syntax_error", "node --check", errors, detail=errors[0]["text"])
    detail = lines[0] if lines else f"node --check returned {proc['returncode']}."
    return _result("checker_failed", "node --check", detail=detail)


def _native_ts(content: str) -> Dict[str, Any]:
    proc = _subproc_check(
        ["tsc", "--noEmit", "--target", "ES2020", "--isolatedModules", "--skipLibCheck"],
        content, ".ts", "tsc", timeout=15,
    )
    if proc["status"] == "checker_unavailable":
        fallback = _native_js(content)
        if fallback["status"] in {"clean", "syntax_error"}:
            fallback["detail"] = _clean_text(
                "tsc unavailable; fell back to node --check syntax validation. " + fallback.get("detail", "")
            )
        return fallback
    if proc["status"] != "ok":
        return proc
    if proc["returncode"] == 0:
        return _result("clean", "tsc")
    errors = []
    for line in (proc["stderr"] + proc["stdout"]).splitlines():
        m = re.match(r'^.*\((\d+),(\d+)\):\s*error\s+\w+:\s*(.+)$', line)
        if m:
            errors.append(_err(int(m.group(1)), int(m.group(2)) - 1, m.group(3)))
    if errors:
        return _result("syntax_error", "tsc", errors, detail=errors[0]["text"])
    return _result("checker_failed", "tsc", detail=f"tsc returned {proc['returncode']}.")


def _native_jsx(content: str) -> Dict[str, Any]:
    proc = _subproc_check(
        ["tsc", "--noEmit", "--jsx", "react", "--allowJs", "--isolatedModules",
         "--skipLibCheck", "--target", "ES2020"],
        content, ".jsx", "tsc", timeout=15,
    )
    if proc["status"] == "checker_unavailable":
        return _result("unsupported", "tsc",
                        detail="tsc not found; node --check cannot validate JSX syntax.")
    if proc["status"] != "ok":
        return proc
    if proc["returncode"] == 0:
        return _result("clean", "tsc")
    errors = []
    for line in (proc["stderr"] + proc["stdout"]).splitlines():
        m = re.match(r'^.*\((\d+),(\d+)\):\s*error\s+\w+:\s*(.+)$', line)
        if m:
            errors.append(_err(int(m.group(1)), int(m.group(2)) - 1, m.group(3)))
    if errors:
        return _result("syntax_error", "tsc", errors, detail=errors[0]["text"])
    return _result("checker_failed", "tsc", detail=f"tsc returned {proc['returncode']}.")


def _native_tsx(content: str) -> Dict[str, Any]:
    proc = _subproc_check(
        ["tsc", "--noEmit", "--jsx", "react", "--isolatedModules",
         "--skipLibCheck", "--target", "ES2020"],
        content, ".tsx", "tsc", timeout=15,
    )
    if proc["status"] == "checker_unavailable":
        fallback = _native_js(content)
        if fallback["status"] in {"clean", "syntax_error"}:
            fallback["detail"] = _clean_text(
                "tsc unavailable; fell back to node --check (JSX not fully validated). "
                + fallback.get("detail", "")
            )
        return fallback
    if proc["status"] != "ok":
        return proc
    if proc["returncode"] == 0:
        return _result("clean", "tsc")
    errors = []
    for line in (proc["stderr"] + proc["stdout"]).splitlines():
        m = re.match(r'^.*\((\d+),(\d+)\):\s*error\s+\w+:\s*(.+)$', line)
        if m:
            errors.append(_err(int(m.group(1)), int(m.group(2)) - 1, m.group(3)))
    if errors:
        return _result("syntax_error", "tsc", errors, detail=errors[0]["text"])
    return _result("checker_failed", "tsc", detail=f"tsc returned {proc['returncode']}.")


def _native_java(content: str) -> Dict[str, Any]:
    proc = _subproc_check(
        ["javac", "-proc:none", "-source", "11", "-encoding", "UTF-8"],
        content, ".java", "javac", timeout=15,
    )
    if proc["status"] != "ok":
        return proc
    if proc["returncode"] == 0:
        return _result("clean", "javac")
    # Filter to syntax-only errors — javac also reports type/import errors which
    # are not syntax issues (e.g. "cannot find symbol", "package does not exist").
    _JAVAC_SEMANTIC_PATTERNS = {
        "should be declared in", "cannot find symbol", "package does not exist",
        "cannot access", "incompatible types", "is not abstract",
        "has private access", "is already defined", "unreported exception",
        "non-static method", "non-static variable",
    }
    errors = []
    for line in (proc["stderr"] + proc["stdout"]).splitlines():
        m = re.match(r'^.*:(\d+):\s*error:\s*(.+)$', line)
        if m and not any(p in m.group(2) for p in _JAVAC_SEMANTIC_PATTERNS):
            errors.append(_err(int(m.group(1)), 0, m.group(2)))
    if errors:
        return _result("syntax_error", "javac", errors, detail=errors[0]["text"])
    if proc["returncode"] != 0:
        # All errors were semantic (imports, types) — syntax is likely fine.
        return _result("clean", "javac", detail="Syntax OK; semantic errors (imports/types) were ignored.")
    return _result("clean", "javac")


def _native_go(content: str) -> Dict[str, Any]:
    proc = _subproc_check(["gofmt", "-e"], content, ".go", "gofmt")
    if proc["status"] != "ok":
        return proc
    if proc["returncode"] == 0:
        return _result("clean", "gofmt")
    errors = []
    for line in (proc["stderr"] + proc["stdout"]).splitlines():
        m = re.match(r'^.*:(\d+):(\d+):\s*(.+)$', line)
        if m:
            errors.append(_err(int(m.group(1)), int(m.group(2)) - 1, m.group(3)))
    if errors:
        return _result("syntax_error", "gofmt", errors, detail=errors[0]["text"])
    return _result("checker_failed", "gofmt", detail=f"gofmt returned {proc['returncode']}.")


def _native_rust(content: str) -> Dict[str, Any]:
    with _tempfile.TemporaryDirectory() as tmpdir:
        rs_path = _os.path.join(tmpdir, "check.rs")
        with open(rs_path, 'w', encoding='utf-8') as f:
            f.write(content)
        try:
            kwargs = {}
            if _sys.platform == "win32":
                kwargs["creationflags"] = _subprocess.CREATE_NO_WINDOW
            r = _subprocess.run(
                ["rustc", "--edition", "2021", "--emit=metadata", "--out-dir", tmpdir, rs_path],
                capture_output=True, text=True, timeout=15,
                **kwargs
            )
        except FileNotFoundError:
            return _result("checker_unavailable", "rustc", detail="rustc is not installed or not on PATH.")
        except PermissionError as e:
            return _result("checker_failed", "rustc", detail=str(e) or "rustc access denied.")
        except _subprocess.TimeoutExpired:
            return _result("checker_timeout", "rustc", detail="rustc exceeded 15s.")
        except OSError as e:
            return _result("checker_failed", "rustc", detail=str(e) or "rustc failed to start.")
    if r.returncode == 0:
        return _result("clean", "rustc")
    errors, lines = [], (r.stderr + r.stdout).splitlines()
    for i, line in enumerate(lines):
        m = re.match(r'^error(?:\[E\d+\])?: (.+)$', line)
        if m:
            for j in range(i + 1, min(i + 5, len(lines))):
                loc = re.match(r'^\s*--> [^:]+:(\d+):(\d+)', lines[j])
                if loc:
                    errors.append(_err(int(loc.group(1)), int(loc.group(2)) - 1, m.group(1)))
                    break
    if errors:
        return _result("syntax_error", "rustc", errors, detail=errors[0]["text"])
    detail = lines[0] if lines else f"rustc returned {r.returncode}."
    return _result("checker_failed", "rustc", detail=detail)


def _native_r(content: str) -> Dict[str, Any]:
    # Write to a temp file so parse(file=...) gets the full content reliably.
    # parse(stdin()) silently truncates large files and misses errors in
    # complex multi-line constructs (e.g. knitr::knit_child() string args).
    _R_TIMEOUT = 20  # Rscript cold-start on Windows is 3-8s; 20s is generous.
    with _tempfile.NamedTemporaryFile(
        mode='w', suffix='.R', delete=False, encoding='utf-8'
    ) as tmp:
        tmp.write(content)
        tmp_path = tmp.name
    proc = None
    try:
        kwargs = {}
        if _sys.platform == "win32":
            kwargs["creationflags"] = _subprocess.CREATE_NO_WINDOW
        # Use forward slashes — R on Windows accepts them and avoids backslash escaping issues.
        # Escape single quotes for safe embedding in R string literal.
        r_path = tmp_path.replace('\\', '/').replace("'", "\\'")
        proc = _subprocess.Popen(
            ["Rscript", "--vanilla", "-e",
             f"tryCatch({{parse(file='{r_path}');cat('OK\\n')}},error=function(e){{cat('ERROR:',conditionMessage(e),'\\n')}})"],
            stdin=_subprocess.DEVNULL,
            stdout=_subprocess.PIPE, stderr=_subprocess.PIPE, text=True,
            **kwargs
        )
        try:
            stdout, stderr = proc.communicate(timeout=_R_TIMEOUT)
        except _subprocess.TimeoutExpired:
            _kill_proc_tree(proc)
            return _result("checker_timeout", "Rscript", detail=f"Rscript exceeded {_R_TIMEOUT}s.")
        output = (stdout or "") + (stderr or "")
        output = output[:65536]
        if "ERROR:" in output:
            text = re.sub(r'^ERROR:\s*', '', output.strip())
            # R reports "file:line:col: message" — extract line and column.
            m = re.search(r':(\d+):(\d+):', text)
            if m:
                line, col = int(m.group(1)), int(m.group(2))
            else:
                lm = re.search(r'line (\d+)', text)
                line, col = (int(lm.group(1)) if lm else 1), 0
            return _result("syntax_error", "Rscript", [_err(line, col, text)], detail=text)
        if proc.returncode != 0 and "OK" not in output:
            detail = (output.strip() or f"Rscript exited with code {proc.returncode}")
            return _result("checker_failed", "Rscript", detail=detail)
        return _result("clean", "Rscript")
    except FileNotFoundError:
        return _result("checker_unavailable", "Rscript", detail="Rscript is not installed or not on PATH.")
    except PermissionError as e:
        return _result("checker_failed", "Rscript", detail=str(e) or "Rscript access denied.")
    except OSError as e:
        return _result("checker_failed", "Rscript", detail=str(e) or "Rscript failed to start.")
    finally:
        if proc and proc.poll() is None:
            _kill_proc_tree(proc)
        try:
            _os.unlink(tmp_path)
        except OSError:
            pass


def _native_php(content: str) -> Dict[str, Any]:
    proc = _subproc_check(["php", "-l"], content, ".php", "php -l")
    if proc["status"] != "ok":
        return proc
    if proc["returncode"] == 0:
        return _result("clean", "php -l")
    output = _clean_text(proc["stderr"] + "\n" + proc["stdout"])
    for line in output.splitlines():
        m = re.match(r'^.*error:.*in\s+\S+\s+on line\s+(\d+)', line, re.IGNORECASE)
        if m:
            return _result("syntax_error", "php -l", [_err(int(m.group(1)), 0, line.strip())], detail=line.strip())
    return _result("syntax_error", "php -l", [_err(1, 0, output)], detail=output)


def _native_ruby(content: str) -> Dict[str, Any]:
    proc = _subproc_check(["ruby", "-c"], content, ".rb", "ruby -c")
    if proc["status"] != "ok":
        return proc
    if proc["returncode"] == 0:
        return _result("clean", "ruby -c")
    output = _clean_text(proc["stderr"] + "\n" + proc["stdout"])
    for line in output.splitlines():
        m = re.match(r'^.*:(\d+):\s*(.+)$', line)
        if m:
            return _result("syntax_error", "ruby -c", [_err(int(m.group(1)), 0, m.group(2))], detail=m.group(2))
    return _result("syntax_error", "ruby -c", [_err(1, 0, output)], detail=output)


def _native_perl(content: str) -> Dict[str, Any]:
    # Note: perl -c runs BEGIN blocks — use with caution on untrusted code.
    proc = _subproc_check(["perl", "-c"], content, ".pl", "perl -c")
    if proc["status"] != "ok":
        return proc
    if proc["returncode"] == 0:
        return _result("clean", "perl -c")
    output = _clean_text(proc["stderr"] + "\n" + proc["stdout"])
    for line in output.splitlines():
        m = re.match(r'^.*at\s+\S+\s+line\s+(\d+)', line)
        if m:
            return _result("syntax_error", "perl -c", [_err(int(m.group(1)), 0, line.strip())], detail=line.strip())
    return _result("syntax_error", "perl -c", [_err(1, 0, output)], detail=output)


def _native_lua(content: str) -> Dict[str, Any]:
    proc = _subproc_check(["luac", "-p"], content, ".lua", "luac -p")
    if proc["status"] != "ok":
        return proc
    if proc["returncode"] == 0:
        return _result("clean", "luac -p")
    output = _clean_text(proc["stderr"] + "\n" + proc["stdout"])
    for line in output.splitlines():
        m = re.match(r'^.*:(\d+):\s*(.+)$', line)
        if m:
            return _result("syntax_error", "luac -p", [_err(int(m.group(1)), 0, m.group(2))], detail=m.group(2))
    return _result("syntax_error", "luac -p", [_err(1, 0, output)], detail=output)


def _native_shell(content: str) -> Dict[str, Any]:
    proc = _subproc_check(["bash", "-n"], content, ".sh", "bash -n")
    if proc["status"] != "ok":
        return proc
    if proc["returncode"] == 0:
        return _result("clean", "bash -n")
    errors = []
    output = _clean_text(proc["stderr"] + "\n" + proc["stdout"])
    for line in output.splitlines():
        m = re.match(r'^.*: line (\d+): (.+)$', line)
        if m:
            errors.append(_err(int(m.group(1)), 0, m.group(2)))
    if errors:
        return _result("syntax_error", "bash -n", errors, detail=errors[0]["text"])
    detail = output or f"bash -n returned {proc['returncode']}."
    return _result("checker_failed", "bash -n", detail=detail)


_NATIVE_DISPATCH = {
    '.py':   _native_python,
    '.json': _native_json,
    '.yaml': _native_yaml,  '.yml':  _native_yaml,
    '.xml':  _native_xml,   '.svg':  _native_xml,
    '.toml': _native_toml,
    '.html': _native_html,  '.htm':  _native_html,
    '.css':  _native_css,
    '.js':   _native_js,    '.jsx':  _native_jsx,
    '.ts':   _native_ts,    '.tsx':  _native_tsx,
    '.java': _native_java,
    '.go':   _native_go,
    '.rs':   _native_rust,
    '.r':    _native_r,
    '.php':  _native_php,
    '.rb':   _native_ruby,
    '.pl':   _native_perl,  '.pm':   _native_perl,
    '.lua':  _native_lua,
    '.sh':   _native_shell, '.bash': _native_shell,
}


def check_syntax_native(content: str, ext: str) -> Dict[str, Any]:
    """Syntax-check using native parsers/compilers and return a structured result."""
    normalized_ext = (ext or "").lower()
    fn = _NATIVE_DISPATCH.get(normalized_ext)
    if not fn:
        return _result(
            "unsupported",
            _checker_name(normalized_ext),
            detail=f"No native syntax checker is registered for '{ext or '[no extension]'}'.",
        )
    try:
        result = fn(content)
        if not isinstance(result, dict) or "status" not in result:
            return _result("checker_failed", _checker_name(normalized_ext), detail="Checker returned an invalid result.")
        return result
    except Exception as e:
        return _result("checker_failed", _checker_name(normalized_ext), detail=f"Checker error: {e}")


def check_syntax_native_with_timeout(content: str, ext: str, timeout_seconds: int = 35) -> Dict[str, Any]:
    """Run native syntax validation with a hard timeout using a daemon thread.

    Uses threading instead of multiprocessing to avoid the Windows spawn
    deadlock (re-importing tree_sitter bindings in a fresh process blocked
    the MCP server's stdio thread indefinitely).  Individual subprocess-based
    checkers already carry their own timeouts; this wrapper provides a final
    safety net for pure-Python checkers that could theoretically loop.
    """
    normalized_ext = (ext or "").lower()
    if normalized_ext not in _NATIVE_DISPATCH:
        return check_syntax_native(content, normalized_ext)

    timeout_seconds = max(1, int(timeout_seconds or 12))

    result_holder: list = [None]

    def _run() -> None:
        try:
            result_holder[0] = check_syntax_native(content, normalized_ext)
        except Exception as e:
            result_holder[0] = _result(
                "checker_failed",
                _checker_name(normalized_ext),
                detail=f"Validation worker crashed: {e}",
            )

    t = _threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout_seconds)

    if t.is_alive():
        return _result(
            "checker_timeout",
            _checker_name(normalized_ext),
            detail=f"Validation exceeded {timeout_seconds}s and was terminated.",
        )

    if not isinstance(result_holder[0], dict):
        return _result("checker_failed", _checker_name(normalized_ext), detail="Validation worker returned no result.")
    return result_holder[0]
