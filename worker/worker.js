/**
 * 期权权利金网页 —— 更新触发器 (Cloudflare Worker, 免费计划即可)
 *
 * 作用: 替网页保管 GitHub Token, 访问者只需输入共享口令即可触发 GitHub Actions,
 *       无需 GitHub 账号, Token 不会出现在浏览器端。
 *
 * 部署: Cloudflare 控制台 → Workers & Pages → Create Worker → 粘贴本文件 → Deploy
 *       然后在 Settings → Variables and Secrets 添加:
 *         GITHUB_TOKEN  (类型 Secret): fine-grained PAT, Actions: Read and write
 *         ACCESS_KEY    (类型 Secret): 自定义共享口令, 如 8 位数字/字母
 */

const GITHUB_API =
  'https://api.github.com/repos/ylw0329/option-premium/actions/workflows/run.yml/dispatches';
const ALLOW_ORIGIN = 'https://ylw0329.github.io';
const COOLDOWN_MS = 5 * 60 * 1000;   // 同一 IP 两次触发最小间隔 5 分钟

const lastHit = new Map();           // IP -> 上次触发时间戳

function json(obj, status, extra = {}) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: {
      'Content-Type': 'application/json; charset=utf-8',
      'Access-Control-Allow-Origin': ALLOW_ORIGIN,
      'Access-Control-Allow-Methods': 'POST, OPTIONS',
      'Access-Control-Allow-Headers': 'Content-Type',
      ...extra,
    },
  });
}

export default {
  async fetch(request, env) {
    if (request.method === 'OPTIONS') {
      return new Response(null, {
        headers: {
          'Access-Control-Allow-Origin': ALLOW_ORIGIN,
          'Access-Control-Allow-Methods': 'POST, OPTIONS',
          'Access-Control-Allow-Headers': 'Content-Type',
        },
      });
    }
    if (request.method !== 'POST') return json({ ok: false, error: '只允许 POST' }, 405);

    let body;
    try { body = await request.json(); }
    catch { return json({ ok: false, error: '请求格式错误' }, 400); }

    // 1) 校验共享口令
    if (!env.ACCESS_KEY || body.key !== env.ACCESS_KEY) {
      return json({ ok: false, error: '口令错误' }, 403);
    }

    // 2) 同 IP 冷却限流(防止连点/滥用)
    const ip = request.headers.get('CF-Connecting-IP') || 'unknown';
    const now = Date.now();
    if (lastHit.has(ip) && now - lastHit.get(ip) < COOLDOWN_MS) {
      const wait = Math.ceil((COOLDOWN_MS - (now - lastHit.get(ip))) / 1000);
      return json({ ok: false, error: `操作太频繁, 请 ${wait} 秒后再试` }, 429);
    }

    // 3) 转发到 GitHub
    if (!env.GITHUB_TOKEN) return json({ ok: false, error: '服务器未配置 GITHUB_TOKEN' }, 500);
    let gh;
    try {
      gh = await fetch(GITHUB_API, {
        method: 'POST',
        headers: {
          'Authorization': 'Bearer ' + env.GITHUB_TOKEN,
          'Accept': 'application/vnd.github+json',
          'User-Agent': 'option-premium-worker',
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ ref: 'main' }),
      });
    } catch (e) {
      return json({ ok: false, error: '连接 GitHub 失败: ' + e.message }, 502);
    }

    if (gh.status === 200 || gh.status === 204) {
      lastHit.set(ip, now);
      // 顺手清理过期记录, 避免内存增长
      if (lastHit.size > 1000) {
        for (const [k, v] of lastHit) if (now - v > COOLDOWN_MS) lastHit.delete(k);
      }
      return json({ ok: true });
    }
    if (gh.status === 403 || gh.status === 401) {
      return json({ ok: false, error: '服务器 Token 无效或权限不足, 请联系管理员更新 GITHUB_TOKEN' }, 502);
    }
    return json({ ok: false, error: 'GitHub 返回 HTTP ' + gh.status }, 502);
  },
};
