// A small, safe Markdown renderer for chat answers: headings, bullet / numbered lists, tables, fenced code,
// **bold**, *italic* and `code`. It builds React elements only (never raw HTML), so model output cannot inject markup.
import type { ReactNode } from "react";

const INLINE = /(`[^`\n]+`)|(\*\*[^*\n]+\*\*)|(\*[^*\s][^*\n]*\*)/g;

function inline(text: string, keyBase: string): ReactNode[] {
  const out: ReactNode[] = [];
  let last = 0;
  let n = 0;
  for (const m of text.matchAll(INLINE)) {
    const at = m.index ?? 0;
    if (at > last) out.push(text.slice(last, at));
    const tok = m[0];
    const key = `${keyBase}-${n++}`;
    if (m[1]) {
      out.push(
        <code key={key} className="rounded bg-ink-800 px-1 py-px font-mono text-[12px] text-ink-100">
          {tok.slice(1, -1)}
        </code>,
      );
    } else if (m[2]) {
      out.push(<strong key={key}>{tok.slice(2, -2)}</strong>);
    } else {
      out.push(<em key={key}>{tok.slice(1, -1)}</em>);
    }
    last = at + tok.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

const BULLET = /^\s*[-*•]\s+(.*)$/;
const NUMBERED = /^\s*\d+[.)]\s+(.*)$/;
const HEADING = /^(#{1,4})\s+(.*)$/;
const TABLE_SEP = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;

function cells(line: string): string[] {
  const parts = line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|");
  return parts.map((c) => c.trim());
}

export function Markdown({ text }: { text: string }) {
  const lines = text.replace(/\r\n?/g, "\n").split("\n");
  const blocks: ReactNode[] = [];
  let i = 0;
  let k = 0;

  while (i < lines.length) {
    const line = lines[i];
    const key = `b${k++}`;

    if (line.trim() === "") {
      i++;
      continue;
    }

    if (line.trim().startsWith("```")) {
      const body: string[] = [];
      i++;
      while (i < lines.length && !lines[i].trim().startsWith("```")) body.push(lines[i++]);
      i++; // closing fence
      blocks.push(
        <pre key={key} className="my-2 overflow-x-auto rounded-md border border-ink-700 bg-ink-800 p-2.5 font-mono text-[12px] leading-relaxed text-ink-100">
          {body.join("\n")}
        </pre>,
      );
      continue;
    }

    const heading = HEADING.exec(line);
    if (heading) {
      blocks.push(
        <div key={key} className="mb-1 mt-3 text-[13px] font-semibold text-ink-100">
          {inline(heading[2], key)}
        </div>,
      );
      i++;
      continue;
    }

    if (line.includes("|") && i + 1 < lines.length && TABLE_SEP.test(lines[i + 1])) {
      const head = cells(line);
      const rows: string[][] = [];
      i += 2;
      while (i < lines.length && lines[i].includes("|") && lines[i].trim() !== "") rows.push(cells(lines[i++]));
      blocks.push(
        <div key={key} className="my-2 overflow-x-auto rounded-md border border-ink-700">
          <table className="w-full border-collapse text-left text-[12px]">
            <thead className="bg-ink-800 text-ink-300">
              <tr>
                {head.map((h, c) => (
                  <th key={c} className="px-2.5 py-1.5 font-semibold">
                    {inline(h, `${key}h${c}`)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((r, ri) => (
                <tr key={ri} className="border-t border-ink-700">
                  {head.map((_, c) => (
                    <td key={c} className="px-2.5 py-1.5 align-top">
                      {inline(r[c] ?? "", `${key}r${ri}c${c}`)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>,
      );
      continue;
    }

    const bullet = BULLET.test(line);
    if (bullet || NUMBERED.test(line)) {
      const re = bullet ? BULLET : NUMBERED;
      const items: string[] = [];
      while (i < lines.length && re.test(lines[i])) items.push(re.exec(lines[i++])![1]);
      const Tag = bullet ? "ul" : "ol";
      blocks.push(
        <Tag key={key} className={`my-1.5 space-y-0.5 pl-5 ${bullet ? "list-disc" : "list-decimal"}`}>
          {items.map((it, ii) => (
            <li key={ii}>{inline(it, `${key}i${ii}`)}</li>
          ))}
        </Tag>,
      );
      continue;
    }

    const para: string[] = [];
    while (
      i < lines.length &&
      lines[i].trim() !== "" &&
      !lines[i].trim().startsWith("```") &&
      !HEADING.test(lines[i]) &&
      !BULLET.test(lines[i]) &&
      !NUMBERED.test(lines[i])
    ) {
      para.push(lines[i++]);
    }
    blocks.push(
      <p key={key} className="my-1.5 whitespace-pre-wrap">
        {inline(para.join("\n"), key)}
      </p>,
    );
  }

  return <div className="text-[13px] leading-relaxed text-ink-100">{blocks}</div>;
}
