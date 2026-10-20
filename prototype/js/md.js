/** 极简 Markdown 渲染器：只做原型需要的那些语法。
 *  先转义 HTML，再生成标签 —— 绝不能把原文当 HTML 插进页面。
 */
const MTMarkdown = (() => {
  function esc(s) {
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function inline(s) {
    let t = esc(s);
    // 行内代码优先，避免里面的星号被当成强调
    const codes = [];
    t = t.replace(/`([^`]+)`/g, (m, code) => {
      codes.push(code);
      return '\u0001' + (codes.length - 1) + '\u0001';
    });
    t = t.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    t = t.replace(/(^|[^*])\*([^*]+)\*/g, '$1<em>$2</em>');
    t = t.replace(/~~([^~]+)~~/g, '<del>$1</del>');
    t = t.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g,
      '<a href="$2" target="_blank" rel="noreferrer">$1</a>');
    return t.replace(/\u0001(\d+)\u0001/g, (m, i) => '<code>' + codes[+i] + '</code>');
  }

  function render(src) {
    const lines = String(src || '').replace(/\r\n/g, '\n').split('\n');
    const out = [];
    let list = null;      // 'ul' | 'ol'
    let inCode = false;
    let codeBuf = [];
    let quoteBuf = [];

    const closeList = () => {
      if (list) { out.push('</' + list + '>'); list = null; }
    };
    const flushQuote = () => {
      if (quoteBuf.length) {
        out.push('<blockquote>' + inline(quoteBuf.join(' ')) + '</blockquote>');
        quoteBuf = [];
      }
    };

    for (const raw of lines) {
      const line = raw.replace(/\t/g, '    ');

      if (/^```/.test(line.trim())) {
        if (inCode) {
          out.push('<pre class="md-code"><code>' + esc(codeBuf.join('\n')) + '</code></pre>');
          codeBuf = [];
          inCode = false;
        } else {
          closeList(); flushQuote();
          inCode = true;
        }
        continue;
      }
      if (inCode) { codeBuf.push(raw); continue; }

      if (!line.trim()) { closeList(); flushQuote(); continue; }

      const q = line.match(/^\s*>\s?(.*)$/);
      if (q) { closeList(); quoteBuf.push(q[1]); continue; }
      flushQuote();

      const h = line.match(/^(#{1,6})\s+(.*)$/);
      if (h) {
        closeList();
        const level = h[1].length;
        out.push('<h' + level + '>' + inline(h[2]) + '</h' + level + '>');
        continue;
      }
      if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) {
        closeList(); out.push('<hr>'); continue;
      }
      const ul = line.match(/^\s*[-*+]\s+(.*)$/);
      const ol = line.match(/^\s*\d+[.)]\s+(.*)$/);
      if (ul || ol) {
        const want = ul ? 'ul' : 'ol';
        if (list !== want) { closeList(); out.push('<' + want + '>'); list = want; }
        out.push('<li>' + inline((ul || ol)[1]) + '</li>');
        continue;
      }
      closeList();
      out.push('<p>' + inline(line) + '</p>');
    }
    if (inCode && codeBuf.length) {
      out.push('<pre class="md-code"><code>' + esc(codeBuf.join('\n')) + '</code></pre>');
    }
    closeList(); flushQuote();
    return out.join('\n');
  }

  return { render, inline, esc };
})();

window.MTMarkdown = MTMarkdown;
