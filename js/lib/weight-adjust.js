/**
 * Weight Adjustment
 *
 * Handles Ctrl+Up/Down to adjust weights in SD prompt syntax.
 * Supports: <lora:name:weight>, (text:weight), and plain tags.
 */

import { CONFIG } from "./config.js";

/**
 * Find LoRA/lyco/hypernet tag at cursor position.
 * Format: <lora:name:weight> or <lora:name>
 *
 * @param {string} doc - Document text
 * @param {number} pos - Cursor position
 * @returns {Object|null} Tag info or null
 */
function findLoraTag(doc, pos) {
    // Search backwards for <
    let start = -1;
    for (let i = pos - 1; i >= 0; i--) {
        if (doc[i] === "<") {
            start = i;
            break;
        }
        if (doc[i] === ">" || doc[i] === "\n") break;
    }
    if (start === -1) return null;

    // Search forwards for >
    let end = -1;
    for (let i = pos; i < doc.length; i++) {
        if (doc[i] === ">") {
            end = i;
            break;
        }
        if (doc[i] === "<" || doc[i] === "\n") break;
    }
    if (end === -1) return null;

    const content = doc.slice(start, end + 1);
    const match = content.match(/^<(lora|lyco|hypernet):([^:>]+)(?::(-?[\d.]+))?>$/i);

    if (match) {
        return {
            start,
            end,
            type: match[1],
            name: match[2],
            weight: match[3] ? parseFloat(match[3]) : CONFIG.defaultWeight,
        };
    }
    return null;
}

/**
 * Find weighted parentheses at cursor position.
 * Format: (text:weight) or (text)
 *
 * @param {string} doc - Document text
 * @param {number} pos - Cursor position
 * @returns {Object|null} Parentheses info or null
 */
function findWeightedParens(doc, pos) {
    let openParen = -1;
    let depth = 0;

    // Search backwards for opening paren
    for (let i = pos - 1; i >= 0; i--) {
        const char = doc[i];
        const prevChar = i > 0 ? doc[i - 1] : "";

        if (char === ")" && prevChar !== "\\") {
            depth++;
        } else if (char === "(" && prevChar !== "\\") {
            if (depth === 0) {
                openParen = i;
                break;
            }
            depth--;
        }
    }

    if (openParen === -1) return null;

    // Search forwards for closing paren
    depth = 0;
    for (let i = pos; i < doc.length; i++) {
        const char = doc[i];
        const prevChar = i > 0 ? doc[i - 1] : "";

        if (char === "(" && prevChar !== "\\") {
            depth++;
        } else if (char === ")" && prevChar !== "\\") {
            if (depth === 0) {
                return { open: openParen, close: i };
            }
            depth--;
        }
    }
    return null;
}

/**
 * Find plain tag (word) at cursor position.
 * Handles ::Label:: prefix syntax - only wraps the content after the label.
 *
 * @param {string} doc - Document text
 * @param {number} pos - Cursor position
 * @returns {Object|null} Tag info or null
 */
function findPlainTag(doc, pos) {
    const isTagChar = (i) => {
        const c = doc[i];
        if (c === "\n" || c === "," || c === "<" || c === ">" || c === "|" || c === "{" || c === "}") return false;
        if ((c === "(" || c === ")") && (i === 0 || doc[i - 1] !== "\\")) return false;
        return true;
    };

    let wordStart = pos;
    let wordEnd = pos;

    while (wordStart > 0 && isTagChar(wordStart - 1)) {
        wordStart--;
    }
    while (wordEnd < doc.length && isTagChar(wordEnd)) {
        wordEnd++;
    }

    // Trim whitespace from edges
    while (wordStart < wordEnd && /\s/.test(doc[wordStart])) wordStart++;
    while (wordEnd > wordStart && /\s/.test(doc[wordEnd - 1])) wordEnd--;

    if (wordStart < wordEnd) {
        const fullText = doc.slice(wordStart, wordEnd);

        // Check for ::Label:: prefix - only weight the content after the label
        const labelMatch = fullText.match(/^(::([^:]+)::)\s*/);
        if (labelMatch) {
            const labelPrefix = labelMatch[1];
            const labelLen = labelMatch[0].length; // includes trailing whitespace
            const contentStart = wordStart + labelLen;
            const contentText = fullText.slice(labelLen);

            // If there's content after the label, return that for weighting
            if (contentText.trim()) {
                return {
                    start: contentStart,
                    end: wordEnd,
                    text: contentText.trim(),
                    labelPrefix: labelPrefix,
                    labelStart: wordStart
                };
            }
        }

        return { start: wordStart, end: wordEnd, text: fullText };
    }
    return null;
}

/**
 * Round weight to one decimal place.
 *
 * @param {number} weight - Weight value
 * @returns {number} Rounded weight
 */
function roundWeight(weight) {
    return Math.round(weight * 10) / 10;
}

/**
 * Adjust weight at cursor position.
 *
 * @param {EditorView} view - CodeMirror view
 * @param {number} direction - 1 for increase, -1 for decrease
 * @returns {boolean} True if weight was adjusted
 */
export function adjustWeight(view, direction) {
    const delta = direction * CONFIG.weightStep;
    const doc = view.state.doc.toString();
    const pos = view.state.selection.main.head;

    // Try LoRA tag first
    const lora = findLoraTag(doc, pos);
    if (lora) {
        const newWeight = roundWeight(lora.weight + delta);
        const replacement = `<${lora.type}:${lora.name}:${newWeight}>`;
        view.dispatch({
            changes: { from: lora.start, to: lora.end + 1, insert: replacement },
            selection: { anchor: Math.min(pos, lora.start + replacement.length - 1) },
        });
        return true;
    }

    // Try weighted parentheses
    const parens = findWeightedParens(doc, pos);
    if (parens) {
        const fullMatch = doc.slice(parens.open, parens.close + 1);
        const weightMatch = fullMatch.match(/^\((.+):(-?[\d.]+)\)$/);
        const plainTagMatch = !weightMatch && fullMatch.match(/^\((.+)\)$/);

        if (weightMatch) {
            const word = weightMatch[1];
            const oldWeight = parseFloat(weightMatch[2]);
            const newWeight = roundWeight(oldWeight + delta);
            const oldLen = parens.close + 1 - parens.open;
            const replacement = newWeight === CONFIG.defaultWeight ? word : `(${word}:${newWeight})`;
            const newPos = pos + (replacement.length - oldLen);

            view.dispatch({
                changes: { from: parens.open, to: parens.close + 1, insert: replacement },
                selection: { anchor: Math.max(parens.open, newPos) },
            });
            return true;
        }

        if (plainTagMatch) {
            const word = plainTagMatch[1];
            const newWeight = roundWeight(CONFIG.defaultWeight + delta);
            const oldLen = parens.close + 1 - parens.open;
            const replacement = newWeight === CONFIG.defaultWeight ? word : `(${word}:${newWeight})`;
            const newPos = pos + (replacement.length - oldLen);

            view.dispatch({
                changes: { from: parens.open, to: parens.close + 1, insert: replacement },
                selection: { anchor: Math.max(parens.open, newPos) },
            });
            return true;
        }
    }

    // Try plain tag (wrap in parens with weight)
    const tag = findPlainTag(doc, pos);
    if (tag) {
        const newWeight = roundWeight(CONFIG.defaultWeight + delta);
        const replacement = `(${tag.text}:${newWeight})`;

        view.dispatch({
            changes: { from: tag.start, to: tag.end, insert: replacement },
            selection: { anchor: pos + 1 },
        });
        return true;
    }

    return false;
}
