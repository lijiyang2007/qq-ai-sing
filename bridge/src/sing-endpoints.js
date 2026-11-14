// /api/socialV2/sing 和 /api/socialV2/sing-status 两条路由的完整实现。
//
// 这两条是「模型点歌」的唯一入口。QQ 仿真会话的工具面被锁死了（没有 pwsh / read /
// 文件系统），而翻唱流水线恰恰要跑本地进程。所以这里只暴露**提交一首歌**这一个动作：
//   · 参数收窄 —— 只有歌名和模式，没有路径、没有命令、没有参数拼接；
//   · target **只取当前会话**（令牌绑定），模型无法把歌发到别的会话；
//   · 解释器/脚本路径来自 config，模型看不到也改不了；
//   · 限频与并发闸门在 cover-job.js 里按 jobs 目录统计（跨重启有效）。
//
// 另外还防了一类很隐蔽的错：模型可能拿「管理员让别人唱的」那条消息去调 qq_sing。
// 那条消息应该走 qq_relay_send（唱给**别人**），不是 qq_sing（唱给**当前会话**）。
// 下面会检测这种情况并明确拒绝，附上正确指引 —— 否则模型会「唱给管理员听」，
// 然后管理员再手动转发，整个代唱就白做了。

const SING_PATH = '/api/socialV2/sing';
const SING_STATUS_PATHS = ['/api/socialV2/sing-status', '/api/socialV2/sing/list'];

/**
 * @param {object} deps
 * @returns {{ paths: string[], handle: (req,res,url) => Promise<boolean> }}
 */
export function createSingRoutes(deps) {
  const {
    getConfig, sendJson, readBody, log = () => {},
    agentTokenOk, v2ToolEnabled, v2SessionAllowed, modeAllowed,
    shouldBlockSilentReply, getCurrentMode, isPaused,
    coverJobs, songIntents, submitTrackedSong, relayGuidance,
    parseBilibiliSongRequest, parseProSongRequest, getSocialV2State
  } = deps;

  for (const [name, value] of Object.entries({
    getConfig, sendJson, readBody, agentTokenOk, v2ToolEnabled, v2SessionAllowed,
    modeAllowed, shouldBlockSilentReply, getCurrentMode, isPaused,
    coverJobs, songIntents, submitTrackedSong, relayGuidance,
    parseBilibiliSongRequest, parseProSongRequest, getSocialV2State
  })) {
    if (value === undefined || value === null) {
      throw new TypeError(
        `createSingRoutes: 缺少必需的 deps.${name}。` +
        ' 完整清单见 docs/BRIDGE_WIRING.md「deps 清单」。'
      );
    }
  }

  const cfgOf = () => getConfig();

  // ── 提交 ──────────────────────────────────────────────────────────────
  async function handleSing(req, res) {
    const cfg = cfgOf();
    const body = await readBody();
    const token = String(body.token ?? req.headers['x-agent-token'] ?? '').trim();
    const key = String(body.key ?? '').trim();
    const mode = getCurrentMode();

    if (mode !== 'reserved2') { sendJson({ ok: false, error: '该接口仅 reserved2 模式可用' }, 403); return; }
    if (!key) { sendJson({ ok: false, error: 'key 不能为空（格式 group:群号 或 private:QQ号）' }, 400); return; }
    if (!token) { sendJson({ ok: false, error: 'reserved2 模式点歌必须携带 agent token' }, 403); return; }
    if (!agentTokenOk(key, token)) { sendJson({ ok: false, error: 'agent token 无效' }, 403); return; }
    if (!v2ToolEnabled('sing')) { sendJson({ ok: false, error: '工具未启用：qq_sing' }, 403); return; }
    if (!v2SessionAllowed(key)) { sendJson({ ok: false, error: '目标不在当前模式允许范围内' }, 403); return; }
    if (shouldBlockSilentReply(key)) { sendJson({ ok: false, error: '静默模式已开启，当前不允许点歌' }, 403); return; }

    const keyMatch = /^(group|private):(\d+)$/.exec(key);
    if (!keyMatch) { sendJson({ ok: false, error: 'key 格式应为 group:群号 或 private:QQ号' }, 400); return; }
    const kind = keyMatch[1];
    const id = Number(keyMatch[2]);
    if (!Number.isFinite(id) || id <= 0 || !modeAllowed(key, kind, id, cfg, mode)) {
      sendJson({ ok: false, error: '目标不在当前模式允许范围内' }, 403);
      return;
    }

    try {
      const rawSong = String(body.song ?? '').trim();
      let action = String(body.action ?? 'submit');
      let value = rawSong;
      // 兼容旧客户端：把动作前缀写在歌名里。
      for (const [prefix, act] of [['等歌手：', 'wait_artist'], ['补充歌手：', 'choose_artist'],
        ['更换点歌：', 'replace'], ['取消点歌', 'cancel']]) {
        if (rawSong.startsWith(prefix)) { action = act; value = rawSong.slice(prefix.length).trim(); break; }
      }

      const recent = getSocialV2State(key).recentMessages || [];
      const message = body.requestMessageId
        ? recent.find((m) => !m.isSelf && String(m.messageId) === String(body.requestMessageId))
        : [...recent].reverse().find((m) => !m.isSelf && m.userId);
      const requesterId = kind === 'private' ? String(id) : String(message?.userId ?? '');

      // 关键防错：这条消息其实是「管理员让别人唱的」，该走代发，不该走这里。
      const relayRequests = relayGuidance(key, message ? [message] : []);
      if (relayRequests.length && ['submit', 'wait_artist'].includes(action)) {
        sendJson({
          ok: false,
          error: '这条消息是管理员代唱给别人，应使用qq_relay_send，不是qq_sing；不要让管理员改口重发。先qq_relay_targets核对收件人，再对每个目标分别提交。',
          code: 'USE_RELAY_FOR_RECIPIENTS',
          relaySongRequests: relayRequests
        }, 409);
        return;
      }

      if (message?.proSongHandled && ['submit', 'wait_artist'].includes(action)) {
        throw Object.assign(new Error('这条pro点歌已由桥接处理，请查询进度，不要去掉pro再次提交'), { statusCode: 409 });
      }
      if (message?.videoSongHandled && ['submit', 'wait_artist'].includes(action)) {
        throw Object.assign(new Error('这条B站点歌已由桥接处理，请查进度，不要重复提交'), { statusCode: 409 });
      }

      // B 站来源：必须来自本会话真实的那条消息，不能由模型自己编一个链接。
      const videoRequest = parseBilibiliSongRequest(value);
      if (videoRequest) {
        const actual = parseBilibiliSongRequest(message?.plain || message?.text || '');
        if (!actual || actual.url !== videoRequest.url) {
          throw Object.assign(new Error('视频点歌必须来自本会话明确要求唱该视频的消息'), { statusCode: 403 });
        }
        const result = await submitTrackedSong({
          song: 'B站视频 ' + videoRequest.url, videoUrl: videoRequest.url,
          mode: body.mode || 'full', target: key
        });
        sendJson({ ok: true, ...result, sourceKind: 'bilibili', sourceNote: '使用指定视频音轨，原始音质可能有损；不是无损来源。' });
        return;
      }

      // pro 模式：同样必须来自真实消息，模型不能自行启用。
      const proIntent = parseProSongRequest(value);
      if (proIntent) {
        if (message?.proSongHandled) {
          throw Object.assign(new Error('这条pro点歌已由桥接处理，请查询进度，不要重复提交'), { statusCode: 409 });
        }
        const actual = parseProSongRequest(message?.plain || message?.text || '');
        if (!actual || actual.query !== proIntent.query) {
          throw Object.assign(new Error('pro模式必须来自当前会话真实的pro点歌消息，不能由AI自行启用'), { statusCode: 403 });
        }
      }

      // 非 submit 的交互动作（等歌手 / 补充 / 更换 / 取消）。
      if (action !== 'submit') {
        if (!requesterId) throw Object.assign(new Error('无法确认原点歌者，请先读取对应消息'), { statusCode: 400 });
        let request;
        if (action === 'wait_artist') request = await songIntents.autoSelect({ key, title: value, mode: body.mode || 'full', requesterId });
        else if (action === 'choose_artist') request = await songIntents.provideArtist({ key, artist: value, requesterId });
        else if (action === 'replace') request = await songIntents.replace({ key, title: value, mode: body.mode || 'full', requesterId });
        else if (action === 'cancel') request = songIntents.cancel({ key, requesterId });
        else throw Object.assign(new Error('不支持的点歌动作'), { statusCode: 400 });
        sendJson({
          ok: true, status: request.status, job: request.job || null,
          waitingForArtist: false, deadline: request.deadline,
          note: '桥接按官方明确标注原版的策略自动选歌，不再询问或等待版本；job非空才表示已提交制作。',
          message: request.message
        });
        return;
      }

      // 已有待补歌手的请求时不要再开新任务。
      const pending = /^(group|private):[1-9]\d{4,19}$/.test(key) ? songIntents.get(key) : null;
      if (pending && ['pending', 'asking', 'resolving'].includes(pending.status)) {
        throw Object.assign(new Error('已有待补歌手请求，请用补充歌手、取消或更换点歌动作，避免两次提交'), { statusCode: 409 });
      }

      const proRequest = parseProSongRequest(rawSong);
      if (proRequest) {
        const actual = parseProSongRequest(message?.plain || message?.text || '');
        if (!actual || actual.query !== proRequest.query) {
          throw Object.assign(new Error('pro模式必须来自当前会话真实的pro点歌消息，不能由AI自行启用'), { statusCode: 403 });
        }
      }

      const selected = typeof coverJobs.resolveSong === 'function'
        ? await coverJobs.resolveSong(proRequest ? { proQuery: proRequest.query } : { autoOriginalQuery: rawSong })
        : null;
      const result = await submitTrackedSong({
        song: selected?.song || body.song,
        mode: body.mode,
        target: key,
        ...(selected?.trackId ? { trackId: selected.trackId } : {}),
        ...(proRequest && selected?.allowVariant ? { allowVariant: true } : {})
      });
      sendJson({ ok: true, ...result });
    } catch (error) {
      log('[sing] submit failed', error?.message ?? error);
      sendJson({ ok: false, error: error?.message ?? String(error) }, error?.statusCode ?? 500);
    }
  }

  // ── 查状态 ────────────────────────────────────────────────────────────
  async function handleStatus(req, res) {
    const body = await readBody();
    const token = String(body.token ?? req.headers['x-agent-token'] ?? '').trim();
    const key = String(body.key ?? '').trim();
    const jobId = String(body.job ?? body.jobId ?? '').trim();
    const mode = getCurrentMode();

    if (mode !== 'reserved2') { sendJson({ ok: false, error: '该接口仅 reserved2 模式可用' }, 403); return; }
    if (!key) { sendJson({ ok: false, error: 'key 不能为空' }, 400); return; }
    if (!token) { sendJson({ ok: false, error: '必须携带 agent token' }, 403); return; }
    if (!agentTokenOk(key, token)) { sendJson({ ok: false, error: 'agent token 无效' }, 403); return; }
    if (!v2ToolEnabled('sing')) { sendJson({ ok: false, error: '工具未启用：qq_sing' }, 403); return; }
    if (!v2SessionAllowed(key)) { sendJson({ ok: false, error: '目标不在当前模式允许范围内' }, 403); return; }

    try {
      // target 传 key：coverJobs.status 会校验「这个任务是不是这个会话点的」，防跨会话窥探。
      const result = coverJobs.status({ jobId, target: key });
      sendJson({ ok: true, ...result });
    } catch (error) {
      sendJson({ ok: false, error: error?.message ?? String(error) }, error?.statusCode ?? 500);
    }
  }

  return {
    paths: [SING_PATH, ...SING_STATUS_PATHS],
    async handle(req, res, url) {
      if (req.method === 'POST' && url.pathname === SING_PATH) {
        await handleSing(req, res);
        return true;
      }
      if (req.method === 'POST' && SING_STATUS_PATHS.includes(url.pathname)) {
        await handleStatus(req, res);
        return true;
      }
      return false;
    }
  };
}
