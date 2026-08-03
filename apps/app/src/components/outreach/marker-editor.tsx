"use client";

import { useEffect, useRef } from "react";

// Email-safe font stacks. Mirrors backend/app/services/outreach/common.py
// EMAIL_FONTS exactly — keys are what campaign config stores.
export const FONT_STACKS: Record<string, string> = {
    arial: "Arial, Helvetica, sans-serif",
    helvetica: "Helvetica, Arial, sans-serif",
    georgia: "Georgia, 'Times New Roman', serif",
    times: "'Times New Roman', Times, serif",
    verdana: "Verdana, Geneva, sans-serif",
    tahoma: "Tahoma, Geneva, sans-serif",
    trebuchet: "'Trebuchet MS', Helvetica, sans-serif",
    courier: "'Courier New', Courier, monospace",
};

export const FONT_SIZES = [12, 13, 14, 15, 16, 18, 20, 22, 24];

export function cssStackFor(fontFamily: string | null | undefined): string | undefined {
    if (!fontFamily) return undefined;
    return FONT_STACKS[fontFamily];
}

// ── marker <-> HTML serialization ───────────────────────────────────
//
// Mirrors backend/app/services/outreach/common.py's _BOLD_RE / _ITALIC_RE
// / _UNDERLINE_RE / _LINK_RE exactly, so what the editor round-trips
// through matches what the send-time renderer (render_email_html) parses.
// Content must start and end on non-space so "2 * 3 * 4" and stray
// underscores never read as formatting.

// Triple asterisks — what bold+italic nesting collapses to — must be
// handled first, or the bold/italic patterns each reject the leftover
// asterisk.
const TRIPLE_RE = /\*\*\*([^\s*](?:[^\n]*?[^\s*])?)\*\*\*/g;
const BOLD_RE = /\*\*([^\s*](?:[^\n]*?[^\s*])?)\*\*/g;
const ITALIC_RE = /(?<!\*)\*([^\s*](?:[^*\n]*?[^\s*])?)\*(?!\*)/g;
const UNDERLINE_RE = /(?<![\w_])_([^\s_](?:[^_\n]*?[^\s_])?)_(?![\w_])/g;

// Inline links — [label](url), scheme optional, http(s)-shaped domains
// only (mirrors _LINK_RE). Applied before the style passes so a label
// may itself carry them.
const LINK_RE =
    /\[([^\]\n]+)\]\(((?:https?:\/\/)?[a-z0-9][a-z0-9.-]*\.[a-z]{2,}(?:\/[^\s)]*)?)\)/gi;

function escapeHtml(s: string): string {
    return s
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#x27;");
}

/** Plain-text-with-markers -> HTML, for setting the editor's initial
 * innerHTML (only on mount / when `value` changes externally). */
export function markersToHtml(text: string): string {
    const escaped = escapeHtml(text || "").replace(/\u0000/g, "");
    // Hrefs sit behind \u0000-framed placeholders while the style passes
    // run, so a URL's underscores/asterisks can never read as formatting;
    // the label stays inline — it MAY carry style markers. The href keeps
    // the URL exactly as typed (no https:// prepending) so htmlToMarkers
    // round-trips the user's text unchanged; scheme normalization happens
    // in the send-time renderer only.
    const hrefs: string[] = [];
    let html = escaped.replace(LINK_RE, (_m, label: string, url: string) => {
        hrefs.push(url);
        return `<a href="\u0000${hrefs.length - 1}\u0000" style="color:#2563eb;">${label}</a>`;
    });
    html = html.replace(TRIPLE_RE, "<b><i>$1</i></b>");
    html = html.replace(BOLD_RE, "<b>$1</b>");
    html = html.replace(ITALIC_RE, "<i>$1</i>");
    html = html.replace(UNDERLINE_RE, "<u>$1</u>");
    html = html.replace(/\u0000(\d+)\u0000/g, (_m, i) => hrefs[Number(i)] ?? "");
    return html.replace(/\n/g, "<br>");
}

/** Wrap `text` in `marker` on both sides, but push any leading/trailing
 * whitespace OUTSIDE the markers so the backend regexes (which require
 * non-space at the marker's inner edges) still match. Returns `text`
 * unchanged if it's empty or all-whitespace — an empty tag shouldn't
 * turn into a stray "****". */
function wrapMarker(text: string, marker: string): string {
    const match = text.match(/^(\s*)([\s\S]*?)(\s*)$/);
    if (!match) return text;
    const [, lead, core, trail] = match;
    if (!core) return text;
    return `${lead}${marker}${core}${marker}${trail}`;
}

function hasUnderline(style: CSSStyleDeclaration | undefined): boolean {
    if (!style) return false;
    const deco = style.textDecorationLine || style.textDecoration || "";
    return deco.includes("underline");
}

function isBoldStyle(style: CSSStyleDeclaration | undefined): boolean {
    if (!style) return false;
    const w = style.fontWeight;
    if (!w) return false;
    if (w === "bold" || w === "bolder") return true;
    const n = Number(w);
    return !Number.isNaN(n) && n >= 600;
}

function isItalicStyle(style: CSSStyleDeclaration | undefined): boolean {
    return style?.fontStyle === "italic";
}

/** Recursive, transparent-by-default walk: unknown elements just
 * recurse into their children. B/STRONG (or inline font-weight:bold),
 * I/EM (or font-style:italic), and U (or text-decoration:underline)
 * wrap their rendered text in the matching marker; A serializes back to
 * [label](href). BR -> newline. */
function walkNode(node: Node): string {
    if (node.nodeType === Node.TEXT_NODE) {
        return (node.textContent || "").replace(/\u00A0/g, " ");
    }
    if (node.nodeType !== Node.ELEMENT_NODE) return "";
    const el = node as HTMLElement;
    const tag = el.tagName;
    if (tag === "BR") return "\n";

    const inner = Array.from(el.childNodes).map(walkNode).join("");

    const bold = tag === "B" || tag === "STRONG" || isBoldStyle(el.style);
    const italic = tag === "I" || tag === "EM" || isItalicStyle(el.style);
    const underline = tag === "U" || hasUnderline(el.style);

    let out = inner;
    if (tag === "A") {
        // getAttribute (not .href) keeps the URL verbatim; .href would
        // resolve a scheme-less value against the app's own origin.
        const href = el.getAttribute("href");
        if (href && out) out = `[${out}](${href})`;
    }
    if (underline) out = wrapMarker(out, "_");
    if (italic) out = wrapMarker(out, "*");
    if (bold) out = wrapMarker(out, "**");
    return out;
}

/** HTML -> plain-text-with-markers. Block elements (DIV/P — what
 * browsers insert on Enter) each become their own line; BR is an
 * explicit line break; everything else recurses transparently. */
export function htmlToMarkers(root: HTMLElement): string {
    const lines: string[] = [];
    let current = "";
    const flush = () => {
        lines.push(current);
        current = "";
    };
    for (const child of Array.from(root.childNodes)) {
        const el = child.nodeType === Node.ELEMENT_NODE ? (child as HTMLElement) : null;
        if (el && el.tagName === "BR") {
            flush();
            continue;
        }
        if (el && (el.tagName === "DIV" || el.tagName === "P")) {
            if (current) flush();
            current = walkNode(el);
            flush();
            continue;
        }
        current += walkNode(child);
    }
    if (current || lines.length === 0) flush();
    return lines.join("\n");
}

export interface MarkerEditorProps {
    value: string;
    onChange: (markers: string) => void;
    fontFamily: string | null;
    fontSizePx: number | null;
    placeholder?: string;
    minHeightClass?: string;
}

/** WYSIWYG editor whose value is the backend's plain-text-with-markers
 * format (**bold**, *italic*, _underline_). The font controls
 * themselves live OUTSIDE this component — the parent renders them
 * next to the toolbar and passes the resolved family/size down so the
 * editor preview matches what the send-time template will render. */
export function MarkerEditor({
    value,
    onChange,
    fontFamily,
    fontSizePx,
    placeholder,
    minHeightClass = "min-h-[200px]",
}: MarkerEditorProps) {
    const editorRef = useRef<HTMLDivElement | null>(null);
    // Tracks the last markers string *we* emitted via onChange, so the
    // sync effect below only resets innerHTML when `value` changes for
    // a reason OTHER than our own onInput — resetting on every
    // keystroke would blow away the caret position.
    const lastEmitted = useRef<string | null>(null);

    useEffect(() => {
        const el = editorRef.current;
        if (!el) return;
        if (value === lastEmitted.current) return;
        el.innerHTML = markersToHtml(value);
        lastEmitted.current = value;
    }, [value]);

    function emitChange() {
        const el = editorRef.current;
        if (!el) return;
        const markers = htmlToMarkers(el);
        lastEmitted.current = markers;
        onChange(markers);
    }

    function applyCommand(cmd: "bold" | "italic" | "underline") {
        document.execCommand(cmd);
        emitChange();
    }

    function handleToolbarMouseDown(
        e: React.MouseEvent<HTMLButtonElement>,
        cmd: "bold" | "italic" | "underline",
    ) {
        // Keep the current selection alive — a regular click would
        // blur the editor and lose it before execCommand runs.
        e.preventDefault();
        applyCommand(cmd);
    }

    function handleKeyDown(e: React.KeyboardEvent<HTMLDivElement>) {
        const mod = e.metaKey || e.ctrlKey;
        if (!mod) return;
        const key = e.key.toLowerCase();
        if (key === "b") {
            e.preventDefault();
            applyCommand("bold");
        } else if (key === "i") {
            e.preventDefault();
            applyCommand("italic");
        } else if (key === "u") {
            e.preventDefault();
            applyCommand("underline");
        }
    }

    function handlePaste(e: React.ClipboardEvent<HTMLDivElement>) {
        e.preventDefault();
        const text = e.clipboardData.getData("text/plain");
        document.execCommand("insertText", false, text);
        emitChange();
    }

    return (
        <div className="rounded-md border border-border bg-bg-secondary">
            <div className="flex items-center gap-1 border-b border-border px-2 py-1">
                <ToolbarButton
                    label="Bold"
                    onMouseDown={(e) => handleToolbarMouseDown(e, "bold")}
                >
                    <span className="font-bold">B</span>
                </ToolbarButton>
                <ToolbarButton
                    label="Italic"
                    onMouseDown={(e) => handleToolbarMouseDown(e, "italic")}
                >
                    <span className="italic">I</span>
                </ToolbarButton>
                <ToolbarButton
                    label="Underline"
                    onMouseDown={(e) => handleToolbarMouseDown(e, "underline")}
                >
                    <span className="underline">U</span>
                </ToolbarButton>
            </div>
            <div className="relative">
                {!value ? (
                    <span className="pointer-events-none absolute left-3 top-2 text-sm text-text-subtle">
                        {placeholder}
                    </span>
                ) : null}
                <div
                    ref={editorRef}
                    contentEditable
                    suppressContentEditableWarning
                    onInput={emitChange}
                    onKeyDown={handleKeyDown}
                    onPaste={handlePaste}
                    style={{
                        fontFamily: cssStackFor(fontFamily),
                        fontSize: fontSizePx ? `${fontSizePx}px` : undefined,
                    }}
                    className={`${minHeightClass} px-3 py-2 text-sm outline-none`}
                />
            </div>
        </div>
    );
}

function ToolbarButton({
    label,
    onMouseDown,
    children,
}: {
    label: string;
    onMouseDown: (e: React.MouseEvent<HTMLButtonElement>) => void;
    children: React.ReactNode;
}) {
    return (
        <button
            type="button"
            title={label}
            aria-label={label}
            onMouseDown={onMouseDown}
            className="flex h-7 w-7 items-center justify-center rounded text-xs text-text-muted hover:bg-bg-card-hover hover:text-text"
        >
            {children}
        </button>
    );
}
