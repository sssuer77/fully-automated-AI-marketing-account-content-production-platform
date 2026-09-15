// 浏览器侧的文件保存（T4.9 导出 NDJSON）。
//
// 为什么单独一个文件：`saveTextFile` 会碰 `document` / `URL` / `Blob`，而 store 的单测
// 跑在 node 环境（没有 DOM）。把它隔离出来，store 就只需要注入一个"保存函数"，
// 于是"导出"这条链路能在无浏览器的情况下被测到。

/** 从响应头里取文件名（服务端给了 `x-studio-filename` 就用它，否则解析 Content-Disposition）。 */
export function filenameFromHeaders(headers: Headers, fallback: string): string {
  const explicit = headers.get("x-studio-filename");
  if (explicit) return explicit;
  const disposition = headers.get("content-disposition") ?? "";
  const match = /filename="?([^";]+)"?/.exec(disposition);
  return match ? match[1] : fallback;
}

/** 触发一次浏览器下载；非浏览器环境（测试）⇒ 什么都不做，只回传文件名。 */
export function saveTextFile(text: string, filename: string, mime = "application/x-ndjson"): string {
  if (typeof document === "undefined" || typeof URL === "undefined") return filename;
  const blob = new Blob([text], { type: mime });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.rel = "noopener";
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  // 立刻 revoke 会让部分浏览器来不及下载 ⇒ 下一帧再回收。
  setTimeout(() => {
    URL.revokeObjectURL(url);
  }, 0);
  return filename;
}
