# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "playwright>=1.40.0",
#     "requests>=2.31.0",
#     "python-dotenv>=1.0.0",
#     "tenacity>=8.2.0",
# ]
# ///
"""Coursera Study Helper: Extracts reading, video, and quiz content to generate study notes.

Strictly read-only: does NOT interact with, select, or submit quiz forms.
Supports:
- Cross-frame (page.frames) & shadow DOM traversal
- KaTeX and MathJax LaTeX extraction
- Question prompts, code blocks, tables, and multiple-choice / checkbox options
- Short-answer and coding assignment instructions
- Video transcripts and reading material summaries
- Study notes generation via OpenRouter API into study_notes.md
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any

import requests
from dotenv import find_dotenv, load_dotenv
from playwright.sync_api import Browser, BrowserContext, Frame, Page, sync_playwright

# Setup logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("study_helper")

# Load environment configuration
load_dotenv(find_dotenv(usecwd=True))
# Also check for .env in coursera subfolder or parent folder
if not os.getenv("OPENROUTER_API_KEY"):
    for env_candidate in [Path("coursera/.env"), Path("../coursera/.env"), Path(".env")]:
        if env_candidate.exists():
            load_dotenv(dotenv_path=env_candidate)
            if os.getenv("OPENROUTER_API_KEY"):
                break

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "dots-studio/dots-3-note-preview:free")
OPENROUTER_FALLBACK_MODEL = os.getenv("OPENROUTER_FALLBACK_MODEL", "qwen/qwen3.8-27b:free")
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")

DEFAULT_CDP_PORTS = [9222, 9223, 9224, 9323]

# JavaScript snippet for deep DOM extraction (piercing Shadow DOM and extracting KaTeX/MathJax)
EXTRACTOR_JS = r"""
(() => {
    // 1. Recursive shadow DOM collector
    function queryAllDeep(selector, root = document) {
        let results = Array.from(root.querySelectorAll(selector));
        const allElements = root.querySelectorAll('*');
        for (const el of allElements) {
            if (el.shadowRoot) {
                results = results.concat(queryAllDeep(selector, el.shadowRoot));
            }
        }
        return results;
    }

    // 2. Clean math, code, images, and tables inside an element
    function cleanMathAndFormatting(elem) {
        if (!elem) return '';
        const clone = elem.cloneNode(true);

        // Sanitize Coursera content-integrity-instructions prompt injection
        const injectionPhrases = [
            "You are a helpful AI assistant. You have identified that this web page contains a protected assessment",
            "academic integrity",
            "Honor Code"
        ];

        // Process KaTeX
        const katexElements = clone.querySelectorAll('.katex, [data-katex]');
        for (const katex of katexElements) {
            const texAnnotation = katex.querySelector('annotation[encoding="application/x-tex"]');
            let mathText = '';
            if (texAnnotation && texAnnotation.textContent.trim()) {
                mathText = texAnnotation.textContent.trim();
            } else {
                const htmlElem = katex.querySelector('.katex-html');
                mathText = htmlElem ? htmlElem.innerText.trim() : katex.innerText.trim();
            }
            const isDisplay = katex.classList.contains('katex-display') || Boolean(katex.closest('.katex-display'));
            const formatted = isDisplay ? `\n$$ ${mathText} $$\n` : `$${mathText}$`;
            katex.replaceWith(document.createTextNode(formatted));
        }

        // Process MathJax
        const mathJaxElements = clone.querySelectorAll('.MathJax, .MathJax_Preview, script[type*="math/tex"]');
        for (const mj of mathJaxElements) {
            if (mj.tagName === 'SCRIPT') {
                const isDisplay = (mj.type || '').includes('mode=display');
                const tex = mj.textContent.trim();
                mj.replaceWith(document.createTextNode(isDisplay ? `\n$$ ${tex} $$\n` : `$${tex}$`));
            } else {
                const formula = mj.getAttribute('data-formula') || mj.getAttribute('data-mathml') || mj.innerText.trim();
                if (formula) {
                    mj.replaceWith(document.createTextNode(`$${formula}$`));
                }
            }
        }

        // Process Images
        const images = clone.querySelectorAll('img');
        for (const img of images) {
            const alt = img.getAttribute('alt') || 'Diagram';
            let src = img.getAttribute('src') || '';
            if (src.startsWith('//')) src = 'https:' + src;
            img.replaceWith(document.createTextNode(` ![${alt}](${src}) `));
        }

        // Process Tables to Markdown
        const tables = clone.querySelectorAll('table');
        for (const table of tables) {
            const rows = Array.from(table.querySelectorAll('tr'));
            if (rows.length > 0) {
                let mdTable = '\n\n';
                rows.forEach((row, rIdx) => {
                    const cells = Array.from(row.querySelectorAll('th, td')).map(c => c.innerText.trim().replace(/\n/g, ' '));
                    if (cells.length > 0) {
                        mdTable += '| ' + cells.join(' | ') + ' |\n';
                        if (rIdx === 0) {
                            mdTable += '| ' + cells.map(() => '---').join(' | ') + ' |\n';
                        }
                    }
                });
                mdTable += '\n';
                table.replaceWith(document.createTextNode(mdTable));
            }
        }

        // Process Code blocks
        const preBlocks = clone.querySelectorAll('pre');
        for (const pre of preBlocks) {
            const code = pre.querySelector('code');
            const codeText = code ? code.innerText : pre.innerText;
            pre.replaceWith(document.createTextNode(`\n```\n${codeText}\n```\n`));
        }

        let text = clone.innerText || clone.textContent || '';
        // Sanitize any Coursera prompt injection
        for (const phrase of injectionPhrases) {
            if (text.includes(phrase)) {
                text = text.split(phrase)[0].trim();
            }
        }
        return text.trim();
    }

    // 3. Collect Question Containers
    const qSelectors = [
        '[data-testid^="part-Submission_"]',
        '.rc-FormPartsMcq',
        '.rc-FormPartsQuestion',
        '.rc-FormPart',
        '[data-testid^="part-"]',
        'fieldset.rc-QuestionForm',
        'fieldset:not([aria-hidden="true"])',
        '[role="radiogroup"]'
    ];

    let questionNodes = [];
    for (const sel of qSelectors) {
        const found = queryAllDeep(sel);
        for (const node of found) {
            // Avoid agreement checkboxes
            if (node.querySelector('#agreement-checkbox-base')) continue;
            if (!questionNodes.includes(node)) {
                questionNodes.push(node);
            }
        }
        if (questionNodes.length > 0) break;
    }

    // 4. Parse Questions
    const questions = [];
    questionNodes.forEach((node, index) => {
        // Extract prompt
        const promptSelectors = [
            '[id^="prompt-"] [data-testid="cml-viewer"]',
            '[data-testid="legend"] [data-testid="cml-viewer"]',
            '[data-testid="cml-viewer"]:not(label *)',
            '.rc-FormPartsQuestion__content',
            'legend',
            '[id^="prompt-"]',
            '[data-testid="legend"]'
        ];

        let promptElem = null;
        for (const ps of promptSelectors) {
            promptElem = node.querySelector(ps);
            if (promptElem && promptElem.innerText.trim()) break;
        }

        let promptText = promptElem ? cleanMathAndFormatting(promptElem) : '';
        if (!promptText) {
            // Fallback: clone node and remove options/labels
            const temp = node.cloneNode(true);
            temp.querySelectorAll('label, [role="radio"], [role="checkbox"], button').forEach(el => el.remove());
            promptText = cleanMathAndFormatting(temp);
        }

        // Extract Images
        const imgElements = node.querySelectorAll('img');
        const images = [];
        for (const img of imgElements) {
            let src = img.getAttribute('src') || '';
            const alt = img.getAttribute('alt') || 'Diagram';
            if (src.startsWith('//')) src = 'https:' + src;
            if (src && !images.some(i => i.src === src)) {
                images.push({ alt, src });
            }
        }

        // Determine Type
        let qType = 'single';
        if (node.querySelector('input[type="checkbox"], [role="checkbox"]')) {
            qType = 'multiselect';
        } else if (node.querySelector('textarea, [contenteditable="true"], input[type="text"]:not([type="radio"]):not([type="checkbox"])')) {
            qType = 'textarea';
        }

        // Extract Options
        const options = [];
        const optionElems = node.querySelectorAll('label, [role="radio"], [role="checkbox"], .rc-Option, .rc-Option__input-container');
        const seenOpts = new Set();

        for (const opt of optionElems) {
            // Skip agreement checkbox
            if (opt.id === 'agreement-checkbox-base' || opt.closest('#agreement-checkbox-base')) continue;
            
            const optClone = opt.cloneNode(true);
            // Remove aria-hidden icons or radio circles
            optClone.querySelectorAll('input, [aria-hidden="true"], svg').forEach(el => el.remove());
            const optText = cleanMathAndFormatting(optClone).replace(/^[A-D]\.\s*/i, '').trim();

            if (optText && !seenOpts.has(optText) && optText.length > 0) {
                seenOpts.add(optText);
                options.push(optText);
            }
        }

        // Extract instructions or placeholders for textarea/coding prompts
        let instructions = '';
        const ta = node.querySelector('textarea, input[type="text"]');
        if (ta) {
            instructions = ta.getAttribute('placeholder') || ta.getAttribute('aria-label') || '';
        }

        if (promptText || options.length > 0) {
            questions.push({
                index: index + 1,
                prompt: promptText,
                type: qType,
                options: options,
                images: images,
                instructions: instructions
            });
        }
    });

    // 5. Check if Reading or Video page if no questions found
    let pageType = questions.length > 0 ? 'quiz' : 'unknown';
    let readingContent = '';
    let videoContent = '';

    if (pageType === 'unknown') {
        const readingElem = document.querySelector('.rc-ReadingItem, [data-testid="reading-item-container"], .rc-ItemBox');
        if (readingElem || document.querySelector('[data-testid="cml-viewer"]')) {
            const content = document.querySelector('.rc-ReadingItem') || document.querySelector('[data-testid="content-container"]') || document.body;
            readingContent = cleanMathAndFormatting(content);
            if (readingContent.length > 100) pageType = 'reading';
        }

        const videoElem = document.querySelector('video, .rc-VideoItem, [data-testid="video-player"]');
        if (videoElem) {
            pageType = 'video';
            const cues = Array.from(document.querySelectorAll('.rc-TranscriptCue, .rc-Transcript span, .vjs-text-track-cue'))
                .map(c => c.innerText.trim())
                .filter(Boolean);
            videoContent = cues.join(' ');
        }
    }

    return {
        title: document.title || '',
        url: window.location.href,
        pageType: pageType,
        questions: questions,
        readingContent: readingContent,
        videoContent: videoContent
    };
})();
"""


def connect_to_browser(cdp_port: int = 9222, cdp_url: str | None = None) -> tuple[Browser | BrowserContext, Page]:
    """Connect to a running Chrome browser via CDP and locate the Coursera tab."""
    p = sync_playwright().start()

    ports_to_try = [cdp_port] if cdp_port else DEFAULT_CDP_PORTS
    browser = None

    if cdp_url:
        logger.info("Attempting connection to custom CDP URL: %s", cdp_url)
        try:
            browser = p.chromium.connect_over_cdp(cdp_url)
        except Exception as e:
            logger.warning("Failed to connect to %s: %s", cdp_url, e)

    if not browser:
        for port in ports_to_try:
            url = f"http://localhost:{port}"
            try:
                logger.info("Checking CDP port %d...", port)
                browser = p.chromium.connect_over_cdp(url)
                logger.info("Successfully connected to Chrome on port %d!", port)
                break
            except Exception:
                continue

    if not browser:
        logger.error(
            "\n[!] Could not connect to Chrome DevTools.\n"
            "Please ensure Google Chrome is running with remote debugging enabled:\n\n"
            '   "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" --remote-debugging-port=9222\n'
            "   (Or launch Chrome with --remote-debugging-port=9222 and open your Coursera quiz tab)\n"
        )
        sys.exit(1)

    # Find the Coursera tab across all contexts
    all_pages: list[Page] = []
    for ctx in browser.contexts:
        all_pages.extend(ctx.pages)

    if not all_pages:
        logger.error("Connected to Chrome, but no open tabs were found.")
        sys.exit(1)

    logger.info("Discovered %d open tab(s) in Chrome.", len(all_pages))

    coursera_pages = [page for page in all_pages if "coursera.org" in (page.url or "").lower()]

    if not coursera_pages:
        logger.warning("No tabs with 'coursera.org' in URL found among open tabs:")
        for idx, page in enumerate(all_pages):
            logger.warning("  [%d] %s - %s", idx + 1, page.title(), page.url)
        logger.info("Using currently active / first tab: %s", all_pages[0].url)
        target_page = all_pages[0]
    else:
        # Prioritize quiz / exam / assignment tabs
        quiz_pages = [p for p in coursera_pages if any(k in p.url.lower() for k in ["exam", "quiz", "assignment", "supplement"])]
        target_page = quiz_pages[0] if quiz_pages else coursera_pages[0]

    logger.info("Target tab selected: '%s' (%s)", target_page.title(), target_page.url)
    return browser, target_page


def extract_content(page: Page) -> dict[str, Any]:
    """Iterate through all frames and shadow roots to extract quiz, reading, or video content."""
    logger.info("Extracting content from target tab across %d frame(s)...", len(page.frames))

    aggregated_questions: list[dict[str, Any]] = []
    page_info: dict[str, Any] = {
        "title": page.title(),
        "url": page.url,
        "pageType": "unknown",
        "questions": [],
        "readingContent": "",
        "videoContent": "",
    }

    # Iterate over all frames (main frame + iframes such as Coursera Labs / LTI)
    for frame_idx, frame in enumerate(page.frames):
        try:
            logger.info("Inspecting frame %d/%d (%s)...", frame_idx + 1, len(page.frames), frame.name or frame.url)
            frame_data = frame.evaluate(EXTRACTOR_JS)
            if not frame_data:
                continue

            frame_questions = frame_data.get("questions", [])
            if frame_questions:
                logger.info("Found %d question(s) in frame %d.", len(frame_questions), frame_idx + 1)
                for q in frame_questions:
                    # Deduplicate based on prompt text
                    if not any(existing["prompt"] == q["prompt"] for existing in aggregated_questions):
                        aggregated_questions.append(q)

            if frame_data.get("pageType") in ("quiz", "reading", "video") and page_info["pageType"] == "unknown":
                page_info["pageType"] = frame_data["pageType"]

            if frame_data.get("readingContent") and not page_info["readingContent"]:
                page_info["readingContent"] = frame_data["readingContent"]

            if frame_data.get("videoContent") and not page_info["videoContent"]:
                page_info["videoContent"] = frame_data["videoContent"]

        except Exception as err:
            logger.debug("Frame %d evaluation note: %s", frame_idx + 1, err)

    if aggregated_questions:
        page_info["pageType"] = "quiz"
        # Re-index questions cleanly
        for i, q in enumerate(aggregated_questions, 1):
            q["index"] = i
        page_info["questions"] = aggregated_questions

    logger.info(
        "Extraction summary: pageType='%s', %d question(s) found.",
        page_info["pageType"],
        len(page_info["questions"]),
    )
    return page_info


def query_openrouter_study_guide(content: dict[str, Any]) -> str:
    """Send extracted quiz or lecture content to OpenRouter for comprehensive concept explanations."""
    if not OPENROUTER_API_KEY:
        logger.warning("OPENROUTER_API_KEY is not set in environment or .env file.")
        return generate_local_study_guide(content)

    logger.info("Generating study notes via OpenRouter (model: %s)...", OPENROUTER_MODEL)

    prompt = build_study_prompt(content)
    url = f"{OPENROUTER_BASE_URL.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://coursera.org",
        "X-Title": "Coursera Study Helper",
    }

    payload = {
        "model": OPENROUTER_MODEL,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are an expert university professor and master tutor. "
                    "Your goal is to provide deep, high-yield, conceptual explanations and structured study notes "
                    "for course quizzes, readings, and lectures. "
                    "For multiple choice questions, provide detailed step-by-step reasoning, explain the underlying theory, "
                    "and analyze EVERY option (why the correct answer is right, and why each distractor is wrong). "
                    "Format clearly in Markdown with headers, math formatting ($ for inline, $$ for block), and bullet points."
                ),
            },
            {"role": "user", "content": prompt},
        ],
    }

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=90)
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise ValueError(f"OpenRouter returned error: {data['error']}")
        return data["choices"][0]["message"]["content"]
    except Exception as e:
        logger.warning("Primary model %s failed: %s. Attempting fallback model %s...", OPENROUTER_MODEL, e, OPENROUTER_FALLBACK_MODEL)
        if OPENROUTER_FALLBACK_MODEL and OPENROUTER_FALLBACK_MODEL != OPENROUTER_MODEL:
            payload["model"] = OPENROUTER_FALLBACK_MODEL
            try:
                resp = requests.post(url, headers=headers, json=payload, timeout=90)
                resp.raise_for_status()
                data = resp.json()
                return data["choices"][0]["message"]["content"]
            except Exception as fb_err:
                logger.error("Fallback model %s also failed: %s", OPENROUTER_FALLBACK_MODEL, fb_err)

    return generate_local_study_guide(content)


def build_study_prompt(content: dict[str, Any]) -> str:
    """Build a detailed prompt instructing OpenRouter to produce study guides and option breakdowns."""
    title = content.get("title", "Coursera Course Item")
    url = content.get("url", "")
    page_type = content.get("pageType", "quiz")

    if page_type == "quiz":
        questions = content.get("questions", [])
        q_text = ""
        for q in questions:
            q_text += f"\n### Question {q['index']}\n"
            q_text += f"**Type:** {q['type']}\n"
            q_text += f"**Prompt:**\n{q['prompt']}\n\n"
            if q.get("instructions"):
                q_text += f"**Instructions/Placeholder:** {q['instructions']}\n"
            if q.get("images"):
                q_text += "**Diagrams/Images:**\n"
                for img in q["images"]:
                    q_text += f"- [{img['alt']}]({img['src']})\n"
            if q.get("options"):
                q_text += "**Options:**\n"
                for idx, opt in enumerate(q["options"]):
                    label = chr(65 + idx) if idx < 26 else str(idx + 1)
                    q_text += f"- ({label}) {opt}\n"
            q_text += "\n---\n"

        return f"""Please generate a comprehensive, in-depth **Study & Revision Guide** for the following Coursera assessment.

**Assessment Title:** {title}
**URL:** {url}

{q_text}

For EACH question, please format your analysis as follows:
1. **Core Concept & Theoretical Foundation**: Clear explanation of the topic being tested (principles, definitions, formulas).
2. **Step-by-Step Reasoning & Correct Answer**: Identify the right choice with rigorous logical derivation.
3. **Comprehensive Option Breakdown**: Analyze each answer choice (A, B, C, D, etc.), explaining why it is correct or why it is a distractor/incorrect.
4. **Key Takeaways & Memory Anchor**: High-yield summary bullets to remember for real-world application and exams.
"""
    elif page_type == "reading":
        reading_text = content.get("readingContent", "")[:8000]
        return f"""Please generate a structured, comprehensive **Study Summary & Concept Review** for this reading material.

**Title:** {title}
**URL:** {url}

**Reading Content:**
{reading_text}

Provide:
1. **Executive Summary & Main Takeaways**
2. **Core Concepts Explained (with examples and code snippets where relevant)**
3. **Vocabulary & Key Terminology**
4. **Practical Applications & Reflection Questions**
"""
    elif page_type == "video":
        transcript = content.get("videoContent", "")[:8000]
        return f"""Please generate detailed **Lecture Notes & Study Synthesis** from this video lecture transcript.

**Video Title:** {title}
**URL:** {url}

**Transcript / Closed Captions:**
{transcript}

Provide:
1. **Lecture Overview & Key Concepts**
2. **Detailed Topic Breakdown**
3. **Technical Nuances & Highlights**
4. **Summary Review Checklist**
"""
    else:
        return f"Please create high-yield study notes for this Coursera material:\n\nTitle: {title}\nURL: {url}"


def generate_local_study_guide(content: dict[str, Any]) -> str:
    """Generate a clean Markdown document locally if OpenRouter is unreachable or key is missing."""
    title = content.get("title", "Coursera Study Material")
    url = content.get("url", "")
    questions = content.get("questions", [])

    lines = [
        f"# Study Notes: {title}",
        f"\n**Source URL:** {url}",
        f"**Extracted On:** {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "\n> [!NOTE]\n> Extracted in read-only inspection mode without modifying page state.\n",
    ]

    if questions:
        lines.append(f"## Quiz Questions & Concepts ({len(questions)} items)\n")
        for q in questions:
            lines.append(f"### Question {q['index']}")
            lines.append(f"**Type:** {q['type']}")
            lines.append(f"\n{q['prompt']}\n")
            if q.get("options"):
                lines.append("**Options:**")
                for idx, opt in enumerate(q["options"]):
                    label = chr(65 + idx) if idx < 26 else str(idx + 1)
                    lines.append(f"- **({label})** {opt}")
            if q.get("images"):
                lines.append("\n**Attached Images / Diagrams:**")
                for img in q["images"]:
                    lines.append(f"- ![{img['alt']}]({img['src']})")
            lines.append("\n---\n")
    elif content.get("readingContent"):
        lines.append("## Reading Material Summary\n")
        lines.append(content["readingContent"])
    elif content.get("videoContent"):
        lines.append("## Video Lecture Transcript\n")
        lines.append(content["videoContent"])

    return "\n".join(lines)


def save_study_notes(markdown_content: str, output_path: str = "study_notes.md") -> Path:
    """Save generated study notes into Markdown file."""
    path = Path(output_path)
    path.write_text(markdown_content, encoding="utf-8")
    logger.info("Successfully wrote study notes to %s (%d bytes).", path.resolve(), len(markdown_content))
    return path.resolve()


def main() -> None:
    parser = argparse.ArgumentParser(description="Coursera Study Helper - Extract quiz, video, and reading concepts.")
    parser.add_argument("--cdp-port", type=int, default=9222, help="Chrome DevTools remote debugging port (default: 9222)")
    parser.add_argument("--cdp-url", type=str, default=None, help="Custom CDP WebSocket or HTTP URL")
    parser.add_argument("--output", type=str, default="study_notes.md", help="Output Markdown file path")
    args = parser.parse_args()

    logger.info("Starting Coursera Study Helper (Read-Only Mode)...")

    browser, page = connect_to_browser(cdp_port=args.cdp_port, cdp_url=args.cdp_url)

    try:
        content = extract_content(page)
        notes = query_openrouter_study_guide(content)
        out_file = save_study_notes(notes, output_path=args.output)
        print(f"\n=== STUDY NOTES SAVED TO: {out_file} ===")
        print("\n--- PREVIEW ---\n")
        print(notes[:1500] + ("\n... [truncated for display]" if len(notes) > 1500 else ""))
    finally:
        pass  # Keep user's browser open and untouched


if __name__ == "__main__":
    main()

