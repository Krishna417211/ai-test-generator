import Editor from "@monaco-editor/react";

const LANG_BY_EXT: Record<string, string> = {
  ts: "typescript",
  tsx: "typescript",
  js: "javascript",
  jsx: "javascript",
  py: "python",
  java: "java",
  yml: "yaml",
  yaml: "yaml",
  md: "markdown",
  json: "json",
};

function languageFor(filename: string): string {
  const ext = filename.split(".").pop()?.toLowerCase() ?? "";
  return LANG_BY_EXT[ext] ?? "plaintext";
}

interface Props {
  filename: string;
  content: string;
  height?: number;
}

/**
 * Read-only Monaco editor for previewing a generated test file with real
 * syntax highlighting. Monaco is loaded on demand by @monaco-editor/react.
 */
export default function CodeViewer({ filename, content, height = 384 }: Props) {
  return (
    <Editor
      height={height}
      language={languageFor(filename)}
      value={content}
      theme="vs-dark"
      loading={<div className="p-4 text-xs text-grey-500">Loading editor…</div>}
      options={{
        readOnly: true,
        domReadOnly: true,
        minimap: { enabled: false },
        fontSize: 12,
        lineHeight: 18,
        scrollBeyondLastLine: false,
        lineNumbers: "on",
        renderLineHighlight: "none",
        wordWrap: "on",
        automaticLayout: true,
        scrollbar: { alwaysConsumeMouseWheel: false },
        padding: { top: 8, bottom: 8 },
      }}
    />
  );
}
