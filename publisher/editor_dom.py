"""Classify editor roots using field labels, never the article's text or size.

Many rich-text editors use the same class for title, author and article body.
The useful placeholder may belong to their first paragraph instead of the root.
"""

EDITOR_PROBE = r'''el => {
    const doc = el.ownerDocument;
    const rect = el.getBoundingClientRect();
    const fields = ['data-placeholder', 'placeholder', 'aria-label', 'data-field', 'name'];
    const hints = [];
    const addHints = node => fields.forEach(name => {
        const value = (node.getAttribute(name) || '').trim();
        if (value) hints.push(value);
    });
    addHints(el);
    const labelled = (el.getAttribute('aria-labelledby') || '').split(/\s+/);
    for (const id of labelled) {
        const label = doc.getElementById(id);
        if (label && !label.isContentEditable) hints.push((label.textContent || '').trim());
    }
    // Ignore nested independent editors. Do not read article text as a label.
    for (const node of el.querySelectorAll('[data-placeholder], [placeholder], [data-field]')) {
        if (!node.hasAttribute('contenteditable') && node.closest('[contenteditable]') === el)
            addHints(node);
    }
    const id = el.id || '';
    const classes = typeof el.className === 'string' ? el.className : '';
    const identity = id + ' ' + classes;
    const otherHints = hints.some(s => /标题|作者|摘要|搜索|图注/.test(s) ||
        /^(title|author|digest|description|search|caption)$/i.test(s));
    const bodyHints = hints.some(s => /正文/.test(s) || /^(body|content)$/i.test(s));
    const metadataSelector = '#title, #author, #js_description, [role="toolbar"], [role="search"]';
    let metadataIdentity = false;
    for (let node = el; node && node !== doc.body; node = node.parentElement) {
        const value = (node.id || '') + ' ' + (typeof node.className === 'string' ? node.className : '');
        if (/(^|[\s_-])(title|author|digest|description|search|toolbar|caption)([\s_-]|$)/i.test(value))
            metadataIdentity = true;
    }
    let nested = false;
    for (let parent = el.parentElement; parent; parent = parent.parentElement) {
        if (parent.isContentEditable) { nested = true; break; }
        if (parent.getAttribute('contenteditable') === 'false') break;
    }
    const editable = el.isContentEditable;
    const readonly = el.getAttribute('aria-readonly') === 'true' || el.getAttribute('aria-disabled') === 'true';
    const excluded = !editable ? 'not_editable' : readonly ? 'readonly' : nested ? 'nested' :
        (otherHints || metadataIdentity || el.closest(metadataSelector)) ? 'non_body_field' : '';
    const explicitBody = bodyHints || /^(ueditor_\d+|js_content|body|editor-body)$/.test(id) ||
        /(^|\s)(edui-body-container|editor-body)(\s|$)/i.test(classes);
    const richEditor = /(^|\s)(ProseMirror|edui-body-container|ql-editor)(\s|$)/i.test(classes);
    return {tag:el.tagName.toLowerCase(), id:id.slice(0,80), classes:classes.slice(0,120),
        // Only report recognized purposes; exclude free text/values/URLs.
        purpose:otherHints ? 'other_field' : bodyHints ? 'body' : '',
        editable, excluded, explicit_body:explicitBody, rich_editor:richEditor,
        width:Math.round(rect.width), height:Math.round(rect.height)};
}'''


def choose_body(candidates):
    """Return a uniquely identified body index, or None. Size is diagnostic only."""
    eligible = [(index, item) for index, item in enumerate(candidates) if not item['excluded']]
    explicit = [(index, item) for index, item in eligible if item['explicit_body']]
    if len(explicit) == 1:
        return explicit[0][0]
    if explicit:
        return None
    if len(eligible) == 1:
        # All known metadata/tool fields were removed above. A single
        # remaining editable root is therefore safer than waiting forever for
        # a class name that a WeChat revision may omit.
        return eligible[0][0]
    return None


def candidate_summary(candidates):
    return '；'.join(
        f"{item['scope']}:{item['tag']}#{item['id'] or '-'}"
        f"[{item['classes'] or '-'}]({item['purpose'] or '用途未知'}"
        f"/{item['excluded'] or '候选'})"
        for item in candidates[:6]
    )
